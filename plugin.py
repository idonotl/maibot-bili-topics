import asyncio, json, random, re, time
from pathlib import Path
import httpx
from collections import deque

from maibot_sdk import API, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .config import BiliConfig
from .store import Store
from .source import fetch
from .danmaku import HEADERS, deep_read, probe, render_probe
from . import frames as fr
from . import transcript as asr
from .search import Search, resolve_bvid



def asr_duration(seconds) -> str:
    total = int(seconds or 0)
    return "%d:%02d" % (total // 60, total % 60) if total < 3600 else "%d:%02d:%02d" % (
        total // 3600, total % 3600 // 60, total % 60)


async def fetch_view_safe(client, bvid):
    from .danmaku import fetch_view
    return await fetch_view(client, bvid)



def parse_duration(value) -> int:
    """把 "7:21" / "1:02:03" 解析成秒；无法解析按 0 处理。"""
    text = str(value or "").strip()
    if not text:
        return 0
    parts = text.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return 0


class BiliTopics(MaiBotPlugin):
    config_model = BiliConfig

    def __init__(self):
        super().__init__()
        self.store = None
        self._cookies = {}
        self.worker = None
        self._deep_lock = asyncio.Lock()
        self._tool_lock = asyncio.Lock()
        self._prewarm = None
        self._browse = None
        self._search = Search()
        self._probe_cache = {}
        self._calls = deque()
        self._whisper = None
        self._whisper_failed = ""
        self._whisper_lock = asyncio.Lock()
        self._asr_tasks = {}
        self._asr_sem = asyncio.Semaphore(1)
        self._last_asr_cleanup = 0.0
        self._tasks = set()
        self._last_manual_browse = 0.0
        self._browse_session = {}
        self._scene_tasks = {}
        self._scene_sem = asyncio.Semaphore(1)
        self._watch_queue: "asyncio.Queue" = asyncio.Queue(maxsize=32)
        self._watch_worker = None

    # ---------- 生命周期 ----------
    async def on_load(self):
        if not self.config.plugin.enabled:
            return
        self.store = Store(self.ctx.paths.data_dir / 'topics.sqlite3')
        self._cookies = self._load_cookies()
        await asyncio.to_thread(self.store.start)
        removed = await asyncio.to_thread(asr.remove_leftovers, self._temp_dir())
        if removed:
            self._get_logger().info("B站转写：启动时清理残留音频 %d 个", removed)
        self.worker = asyncio.create_task(self.run())
        self._watch_worker = asyncio.create_task(self._watch_loop())
        self._browse = asyncio.create_task(self._browse_loop())
        if self.config.plugin.deep_read:
            self._prewarm = asyncio.create_task(self._prewarm_later())

    async def on_unload(self):
        for task in (self.worker, self._prewarm, self._browse, self._watch_worker):
            if task:
                task.cancel()
        await asyncio.gather(*[t for t in (self.worker, self._prewarm, self._browse, self._watch_worker) if t],
                             return_exceptions=True)
        self.worker = None
        self._prewarm = None
        self._browse = None
        self.store = None
        self._probe_cache.clear()
        pending=set(self._tasks)|set(self._scene_tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        self._tasks.clear()
        self._scene_tasks.clear()

    async def on_config_update(self, scope, config_data, version):
        if scope in ('', 'self'):
            await self.on_unload()
            await self.on_load()

    # ---------- 采集 ----------
    async def run(self):
        while True:
            now = time.time()
            cfg = self.config.plugin
            try:
                reserved = await asyncio.to_thread(self.store.reserve, now, cfg.interval_minutes, cfg.daily_max)
                if reserved:
                    try:
                        topics = await fetch(cfg.interests, now)
                        result = '成功，找到%d条' % len(topics)
                    except Exception as exc:
                        topics = []
                        result = '采集失败：' + type(exc).__name__ + ' ' + str(exc)[:120]
                    await asyncio.to_thread(self.store.finish_browse, now, result, topics)
                    self._get_logger().info('B站话题采集: %s', result)
                    if cfg.deep_read and cfg.prewarm_count > 0:
                        self._prewarm = asyncio.create_task(self.prewarm(cfg.prewarm_count))
            except asyncio.CancelledError:
                raise
            except Exception:
                self._get_logger().exception('B站话题采集异常')
            try:
                if time.time() - self._last_asr_cleanup > 6 * 3600:
                    self._last_asr_cleanup = time.time()
                    await asyncio.to_thread(asr.cleanup_temp, self._temp_dir())
                    await asyncio.to_thread(self._cleanup_frames, 3600)
                    if self.store:
                        days = self.config.plugin.transcript_keep_days
                        await asyncio.to_thread(self.store.cleanup_transcripts, days)
            except Exception:
                self._get_logger().debug("B站转写：定期清理失败", exc_info=True)
            await asyncio.sleep(600)

    # ---------- 深读预热 ----------
    async def _prewarm_later(self):
        await asyncio.sleep(8)
        await self.prewarm(max(1, self.config.plugin.prewarm_count))

    async def prewarm(self, limit):
        if not self.store:
            return
        try:
            pending = await asyncio.to_thread(self.store.pending_deep_read, time.time(), limit)
        except Exception:
            self._get_logger().exception('B站预热读取队列失败')
            return
        for topic in pending:
            await self._maybe_deep_read(topic, ignore_cooldown=True)
            await asyncio.sleep(5)

    async def _maybe_deep_read(self, topic, ignore_cooldown=False):
        cfg = self.config.plugin
        if not ignore_cooldown and not await asyncio.to_thread(self.store.cooldown_ok, time.time(), cfg.deep_read_cooldown_minutes):
            return topic
        async with self._deep_lock:
            started = time.time()
            try:
                video, summary, rendered, count = await asyncio.wait_for(
                    deep_read(topic['bvid'], cfg, self.ctx.llm.generate), timeout=150)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = '%s: %s' % (type(exc).__name__, str(exc)[:160])
                if str(exc).startswith(('video too long', 'no danmaku')):
                    self._get_logger().info('B站深读跳过 %s: %s', topic['bvid'], str(exc)[:80])
                    return topic
                await asyncio.to_thread(self.store.log_deep_read, time.time(), topic['bvid'], False, detail)
                self._get_logger().warning('B站深读失败 %s: %s', topic['bvid'], detail[:140])
                return topic
            await asyncio.to_thread(self.store.save_summary, topic['bvid'], video, summary, rendered, count, time.time())
            await asyncio.to_thread(self.store.log_deep_read, time.time(), topic['bvid'], True, '%d条弹幕 %.1fs' % (count, time.time() - started))
            if (cfg.transcript_enabled and self._asr_tasks.get(topic['bvid']) is None
                    and float(video.get('duration') or 0) <= cfg.transcript_max_minutes * 60):
                self._asr_tasks[topic['bvid']] = asyncio.create_task(self._transcribe_job(topic['bvid'], video))
                self._transcribe_tasks_cleanup()
                self._get_logger().info('B站转写：深读后为 %s 排入转写', topic['bvid'])
            self._get_logger().info('B站深读完成 %s: %s', topic['bvid'], summary.get('topic', '')[:60])
            topic.update({'summary': rendered, 'keypoints': summary.get('keypoints') or [],
                          'evidence_level': 'danmaku', 'duration': video.get('duration') or 0,
                          'evidence': '基于该视频公开弹幕与简介的摘要'})
            return topic

    # ---------- 供其他插件 / 主动搭话 ----------
    @API('pick_topic', description='为授权会话选取一条公开话题（已预热的话题带弹幕摘要）', version='1', public=True)
    async def pick_topic(self, target: str = ''):
        cfg = self.config.plugin
        allowed = {*(f'private:{x}' for x in cfg.private_user_ids), *(f'group:{x}' for x in cfg.group_ids)}
        if not self.store or target not in allowed:
            return {'ok': False, 'error': 'target_not_allowed_or_not_ready'}
        topic = await asyncio.to_thread(self.store.pick, target, time.time())
        if topic is None:
            return {'ok': True, 'topic': None}
        if cfg.deep_read and not topic.get('summary'):
            try:
                asyncio.create_task(self._maybe_deep_read(topic))
            except Exception:
                pass
        if self.store:
            row = await asyncio.to_thread(self.store.get_transcript, topic.get('bvid', ''))
            if row and asr.usable(row.get('text', '')):
                topic['transcript'] = row['text'][:500]
                topic['transcript_chars'] = row.get('chars') or len(row['text'])
        return {'ok': True, 'topic': topic}


    @API('learning_material', description='给表达学习插件提供素材：已缓存的转写、画面描述与元信息', version='1', public=True)
    async def learning_material(self, bvid: str = ''):
        """返回已缓存的素材；没有缓存时告诉调用方先去听/看。"""
        code = (bvid or '').strip()
        if not code or not self.store:
            return {'ok': False, 'reason': 'no_store'}
        material = {'bvid': code, 'title': '', 'description': '', 'duration': 0.0,
                    'transcript': '', 'scenes': ''}
        try:
            timeout = httpx.Timeout(15.0, connect=8.0)
            async with httpx.AsyncClient(headers=HEADERS, cookies=self._cookies or None, timeout=timeout,
                                         follow_redirects=True, trust_env=False) as client:
                video = await fetch_view_safe(client, code)
            material['title'] = video.get('title') or ''
            material['description'] = video.get('desc') or ''
            material['duration'] = float(video.get('duration') or 0)
        except Exception:
            self._get_logger().debug('learning_material: 读取视频信息失败 %s', code)
        row = await asyncio.to_thread(self.store.get_transcript, code)
        if row and asr.usable(row.get('text', '')):
            material['transcript'] = row['text']
        scene = await asyncio.to_thread(self.store.get_scene, code)
        if scene and asr.usable(scene['text']):
            material['scenes'] = scene['text']
        if scene:
            raw_hints = scene['danmaku'] if 'danmaku' in scene.keys() else ''
            if raw_hints:
                try:
                    hint_map = json.loads(raw_hints)
                    material['danmaku'] = ["；".join(words) for words in list(hint_map.values())[:6]]
                except (TypeError, ValueError):
                    material['danmaku'] = []
        if not material['transcript'] and not material['scenes']:
            return {'ok': False, 'reason': 'no_material'}
        return {'ok': True, 'material': material}

    @API('status', description='查看公开话题采集状态', version='1')
    async def status(self):
        if not self.store:
            return {'ready': False, 'last_result': '未启用', 'count': 0, 'topics': []}
        return {'ready': True, **(await asyncio.to_thread(self.store.status))}

    # ---------- 工具：给模型按需调用 ----------
    def _allow(self):
        limit = max(1, int(self.config.plugin.tool_rate_per_minute))
        now = time.time()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= limit:
            return False
        self._calls.append(now)
        return True

    async def _cached_probe(self, bvid, sample):
        cached = self._probe_cache.get(bvid)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        video_row = None
        if self.store:
            video_row = await asyncio.to_thread(self.store.get_transcript, bvid)
        has_transcript = bool(video_row and asr.usable(video_row.get("text", "")))
        result = await probe(bvid, sample_size=10 if has_transcript else sample,
                             danmaku_limit=600,
                             max_duration=self.config.plugin.tool_max_duration)
        text = render_probe(result)
        if has_transcript:
            text = ("（这条视频的语音转写已经有内容了；要讲「视频讲了什么」请调用 bili_transcript，"
                    "下面只是观众弹幕反应，不能代替内容。）\n" + text)
        self._probe_cache[bvid] = (time.time(), text)
        if len(self._probe_cache) > 50:
            oldest = min(self._probe_cache, key=lambda k: self._probe_cache[k][0])
            self._probe_cache.pop(oldest, None)
        return text

    @Tool(
        "bili_video_probe",
        description=(
            "读取某个 B站视频的公开信息与弹幕样本，用来判断这个视频讲了什么、观众在聊什么。"
            "当用户发来 B站链接、BV号、b23.tv 短链，或让你「去看一下这个视频」时调用。"
            "只读公开数据：不登录、不下载视频、不看画面和声音，返回里会标明这一点。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url_or_bvid",
                param_type=ToolParamType.STRING,
                description="B站视频链接、b23.tv 短链或 BV 号。",
                required=True,
            ),
        ],
    )
    async def bili_video_probe(self, url_or_bvid: str = "", **kwargs):
        async with self._tool_lock:
            if not self._allow():
                return "调用太频繁了，等一分钟再试。"
            try:
                bvid = await resolve_bvid(url_or_bvid)
                if not bvid:
                    return "没能从这个内容里识别出 BV 号，确认一下是 B站 的视频链接吗？"
                text = await self._cached_probe(bvid, self.config.plugin.tool_sample)
                state = await self._prewarm_transcript(bvid)
                if state == "running":
                    text += ("\n\n（语音正在后台转写，约 1 分钟后调用 bili_transcript 就能拿到「视频里讲了什么」。"
                             "现在先别调它，先正常回话。）")
                elif state == "cached":
                    text += "\n\n（这条视频的语音已经转写好了，需要具体内容可以直接调用 bili_transcript。）"
                return text
            except Exception as exc:
                return "读取这个视频失败了：%s %s" % (type(exc).__name__, str(exc)[:120])

    @Tool(
        "bili_search",
        description=(
            "用关键词搜索 B站 公开视频，返回标题、UP主、播放量和链接，并可选顺带读取第一条的弹幕样本。"
            "当用户给出关键词并让你「去 B站 看看」「搜一下这个」时调用。只读公开搜索结果，不登录。"
        ),
        parameters=[
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="搜索关键词，尽量用最少的核心词，例如「LPL 世界赛」「星穹铁道」。",
                required=True,
            ),
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description="返回几条结果，1-8，默认 5。",
                required=False,
            ),
            ToolParameterInfo(
                name="read_top",
                param_type=ToolParamType.BOOLEAN,
                description="是否顺带读取第一条视频的弹幕样本，默认 true。",
                required=False,
            ),
        ],
    )
    async def bili_search(self, keyword: str = "", limit: int = 5, read_top: bool = True, **kwargs):
        async with self._tool_lock:
            if not self._allow():
                return "调用太频繁了，等一分钟再试。"
            try:
                items = await self._search.search(keyword, limit)
            except Exception as exc:
                return "B站 搜索失败：%s %s" % (type(exc).__name__, str(exc)[:120])
            if not items:
                return "没搜到相关视频，可以换个更常见的关键词。"
            lines = ["关键词「%s」的 B站 公开搜索结果：" % " ".join(str(keyword).split())]
            for i, item in enumerate(items, 1):
                lines.append("%d. %s｜UP：%s｜时长 %s｜播放 %s｜%s" % (
                    i, item["title"], item["author"] or "未知", item["duration"] or "?",
                    item["play"], item["url"]))
            if read_top:
                try:
                    text = await self._cached_probe(items[0]["bvid"], min(30, self.config.plugin.tool_sample))
                    lines += ["", "第一条的视频内容速览：", text]
                except Exception as exc:
                    lines.append("（第一条的弹幕读取失败：%s）" % type(exc).__name__)
            return "\n".join(lines)

    @Tool(
        "bili_trending",
        description=(
            "列出本地已经积攒的 B站 热门话题（含弹幕摘要），用来回答「今天B站有什么热闹/有什么热点」。"
            "这些是后台定时采集的公开热门视频，不是实时搜索。"
        ),
        parameters=[
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description="返回几条，1-8，默认 5。",
                required=False,
            ),
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="可选，按关键词过滤积攒的话题，例如「游戏」「数码」。",
                required=False,
            ),
        ],
    )
    async def bili_trending(self, limit: int = 5, keyword: str = "", **kwargs):
        if not self.store:
            return "话题池还没准备好。"
        try:
            rows = await asyncio.to_thread(self.store.recent, time.time(), 3, limit, keyword)
        except Exception as exc:
            return "读取话题池失败：%s" % type(exc).__name__
        if not rows:
            return "最近三天没有攒到相关话题，等下一轮采集再看。"
        lines = ["最近积攒的 B站 话题（%d 条）：" % len(rows)]
        for i, row in enumerate(rows, 1):
            first = (row.get("summary") or row.get("description") or "").split("\n")[0][:100]
            lines.append("%d. %s｜%s｜%s" % (i, row.get("title", "")[:60], first, row.get("url", "")))
        lines.append("这些是最近攒下的热门；想细聊哪条我可以再挖。")
        return "\n".join(lines)


    @Tool(
        "bili_my_topics",
        description=(
            "把最近攒下的 B站 话题拿出来讲给对方听。当对方问“今天刷B站有什么有趣的吗/有什么新鲜事”时调用。"
            "这些话题讲完就会被删掉，所以同一批内容不会重复出现；如果返回空，就是这段时间没攒到新的。"
        ),
        parameters=[
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description="讲几条，1-4，默认 3。",
                required=False,
            ),
        ],
    )
    async def bili_my_topics(self, limit: int = 3, **kwargs):
        if not self.store:
            return "话题池还没准备好。"
        rows = await asyncio.to_thread(self.store.consume_topics, limit)
        if not rows:
            return ("（这段时间没有攒到新话题，连列表都是空的。可以撒娇或可爱地承认自己最近都在摸鱼，"
                    "没刷到新鲜的东西，不要编造视频内容。）")
        lines = ["我最近刷到的："]
        for index, row in enumerate(rows, 1):
            summary = (row.get("summary") or "").replace("\n", " ")[:160]
            keypoints = row.get("keypoints_json") or []
            if isinstance(keypoints, str):
                try:
                    keypoints = json.loads(keypoints)
                except json.JSONDecodeError:
                    keypoints = []
            lines.append("%d. %s｜%s" % (index, (row.get("title") or "")[:50], summary))
            if keypoints:
                lines.append("   我觉得有意思的点：%s" % "；".join(str(k)[:30] for k in keypoints[:3]))
        lines.append("（聊的时候用自己的话讲，不要念标题，不要贴链接；这几条已经从池子里删掉了。）")
        return "\n".join(lines)

    @Tool(
        "bili_browse",
        description=(
            "去 B站 刷一会儿。mode=session 用于对方说“你可以去刷会儿B站/去玩会儿手机”这类自由时间："
            "会一口气收藏几条，但先不剧透，等对方问再讲。"
            "mode=one 用于对方发链接或让你“马上找一条/现在放一个”，会立刻挑一条并给出标题和简介。"
        ),
        parameters=[
            ToolParameterInfo(
                name="mode",
                param_type=ToolParamType.STRING,
                description="session=自由刷（收藏几条先不说）；one=立刻找一条来分享。默认 session。",
                required=False,
            ),
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="可选方向，如“数码”“LPL”；不填就随机。",
                required=False,
            ),
        ],
    )
    async def bili_browse(self, mode: str = "session", keyword: str = "", **kwargs):
        cfg = self.config.plugin
        if not self.store or not cfg.browse_enabled:
            return "现在不方便刷。"
        now = time.time()
        if now - getattr(self, "_last_manual_browse", 0.0) < 120:
            return "刚刷过一小会儿啦，过几分钟再刷嘛。"
        words = [w for w in ([keyword] if keyword else (cfg.browse_keywords or cfg.interests)) if w]
        if not words:
            return "一时想不到刷什么。"
        picked = random.choice(words)
        want = 1 if str(mode).lower() == "one" else 4
        try:
            items = await self._search.search(picked, 8)
        except Exception as exc:
            return "刷的时候网络有点卡：%s" % type(exc).__name__
        limit = cfg.browse_max_minutes * 60
        usable = [i for i in items if parse_duration(i.get("duration")) <= limit]
        if not usable:
            return "刷了一圈没看到合适的，等下再逛逛。"
        random.shuffle(usable)
        picked_items = []
        for choice in usable[: max(want, 1) * 2]:
            if len(picked_items) >= want:
                break
            try:
                async with httpx.AsyncClient(headers=HEADERS, cookies=self._cookies or None,
                                             timeout=httpx.Timeout(12.0, connect=6.0),
                                             follow_redirects=True, trust_env=False) as client:
                    video = await fetch_view_safe(client, choice["bvid"])
            except Exception:
                continue
            if float(video.get("duration") or 0) > limit:
                continue
            picked_items.append({
                "bvid": video["bvid"], "title": video["title"], "desc": video["desc"],
                "author": video["author"], "duration": video.get("duration") or 0,
                "url": "https://www.bilibili.com/video/" + video["bvid"],
                "published": video.get("published") or 0,
            })
        if not picked_items:
            return "点进去几个都没加载出来，等下再看。"
        self._last_manual_browse = now
        if str(mode).lower() == "one":
            video = picked_items[0]
            topic = {"bvid": video["bvid"], "title": video["title"], "description": video["desc"],
                     "category": "", "author": video["author"], "url": video["url"],
                     "published": video["published"], "fetched": now}
            await asyncio.to_thread(self.store.finish_browse, now,
                                    "按指令浏览（one）：%s → %s" % (picked, video["title"][:24]), [topic])
            self._track(asyncio.create_task(self._maybe_deep_read(
                {"bvid": video["bvid"], "title": video["title"], "description": video["desc"],
                 "published": video["published"]}, ignore_cooldown=True)))
            self._enqueue_watch(video, "user")
            self._get_logger().info("B站按指令浏览（one）：%s → %s", picked, video["title"][:40])
            return ("刷到一个：《%s》｜UP：%s｜时长 %s\n简介：%s\n"
                    "（可以直接讲给对方听；要细节我可以再听语音。）" % (
                        video["title"][:60], video["author"] or "未知",
                        asr_duration(video["duration"]), (video["desc"] or "")[:120]))

        self._browse_session = {"started": now, "keyword": picked,
                                "items": picked_items, "shared": 0}
        for item in picked_items:
            self._enqueue_watch(item, "browse")
        await asyncio.to_thread(self.store.finish_browse, now,
                                "自由刷（session）：%s 收藏 %d 条" % (picked, len(picked_items)), [])
        self._get_logger().info("B站自由刷：%s 收藏 %d 条", picked, len(picked_items))
        return ("已经刷了一会儿，收藏了 %d 条有意思的（先不剧透）。"
                "这是浏览结果；回应语气与先后由当前对话决定，不需要重复答应去刷；先不讲具体视频内容，"
                "等对方问你刷到什么、或说“分享一下”时，再调用 bili_share_session 讲。"
                % len(picked_items))

    @Tool(
        "bili_share_session",
        description=(
            "把刚才自由刷时收藏的视频讲出来。只在对方明确问“刷到什么了/分享一下/给我讲讲”时调用。"
            "讲完这些就当作分享过了，不会重复。"
        ),
        parameters=[
            ToolParameterInfo(
                name="count",
                param_type=ToolParamType.INTEGER,
                description="讲几条，1-3，默认 2。",
                required=False,
            ),
        ],
    )
    async def bili_share_session(self, count: int = 2, **kwargs):
        session = self._browse_session or {}
        items = session.get("items") or []
        if not items:
            return "（这次没有收藏到东西；可以说刚才刷了会儿但没看到特别想聊的，不要编。）"
        take = max(1, min(int(count or 2), 3))
        picked = items[:take]
        self._browse_session["items"] = items[take:]
        self._browse_session["shared"] = session.get("shared", 0) + len(picked)
        lines = ["刚才刷到的："]
        for index, video in enumerate(picked, 1):
            lines.append("%d. 《%s》｜UP：%s｜时长 %s" % (
                index, video["title"][:50], video["author"] or "未知",
                asr_duration(video["duration"])))
            if video.get("desc"):
                lines.append("   简介：%s" % video["desc"][:80])
            transcript = None
            scene = None
            if self.store:
                transcript = await asyncio.to_thread(self.store.get_transcript, video["bvid"])
                scene = await asyncio.to_thread(self.store.get_scene, video["bvid"])
            if transcript and asr.usable(transcript.get("text", "")):
                lines.append("   我听到的：%s" % transcript["text"][:180].replace("\n", " "))
            if scene and asr.usable(scene["text"]):
                lines.append("   我看到的：%s" % scene["text"][:200].replace("\n", " "))
            if scene and "danmaku" in scene.keys() and scene["danmaku"]:
                try:
                    hint_map = json.loads(scene["danmaku"])
                    words = ["、".join(v) for v in list(hint_map.values())[:3]]
                    if words:
                        lines.append("   观众反应（帮我理解，不必逐条说）：%s" % "｜".join(words))
                except (TypeError, ValueError):
                    pass
        lines.append("（用自己的话讲，不要念标题、不要贴链接；弹幕只当理解素材，"
                     "不要固定说“弹幕都在刷什么”，觉得贴切可以顺口带一句；还没深看完的就按简介聊。）")
        return "\n".join(lines)

    @Tool(
        "bili_browse_status",
        description=(
            "查看自己刚才刷B站的状态：是否在刷、刷了多久、收藏了几条、有没有分享过。"
            "当对方问“你刚刚在干嘛/你刚才是不是在刷B站”时调用，好有依据地回答。"
        ),
        parameters=[],
    )
    async def bili_browse_status(self, **kwargs):
        session = self._browse_session or {}
        if not session.get("items") and not session.get("started"):
            return "（现在没有正在进行的刷B站活动；如果对方刚让你去刷而你还没刷，就先调用 bili_browse。）"
        minutes = max(1, int((time.time() - session.get("started", time.time())) // 60))
        return ("正在/刚刷过B站：%s 方向，刷了大约 %d 分钟，收藏 %d 条，已分享 %d 条。"
                "（可以可爱地承认自己在刷，还没分享的就先别剧透，等对方问。）" % (
                    session.get("keyword", "随便看看"), minutes,
                    len(self._browse_session.get("items") or []) + session.get("shared", 0),
                    session.get("shared", 0)))


    @Tool(
        "bili_frames",
        description=(
            "看视频画面：按弹幕热度抽帧，再让视觉模型描述每一帧，得到按时间排序的画面内容。"
            "想知道视频里画面发生了什么、角色在做什么、画面字幕写了什么时用。"
            "第一次调用会启动后台处理（约 1-3 分钟），期间先回一句“我看看”；"
            "之后再来问会直接返回结果。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url_or_bvid",
                param_type=ToolParamType.STRING,
                description="视频链接或 BV 号。",
                required=True,
            ),
        ],
    )
    async def bili_frames(self, url_or_bvid: str, **kwargs):
        cfg = self.config.plugin
        if not cfg.frames_enabled:
            return "抽帧看画面现在是关的。"
        bvid = await resolve_bvid(url_or_bvid)
        if not bvid:
            return "没能识别出 BV 号。"
        if self.store:
            cached = await asyncio.to_thread(self.store.get_scene, bvid)
            if cached and asr.usable(cached["text"]):
                return self._render_scene(cached, cached=True)
        if bvid in self._scene_tasks and not self._scene_tasks[bvid].done():
            return ("画面还在分析（大约 1-3 分钟）。先别调这个工具，"
                    "可以正常回一句“我看看”，过一会儿再问。")
        try:
            timeout = httpx.Timeout(20.0, connect=10.0)
            async with httpx.AsyncClient(headers=HEADERS, cookies=self._cookies or None, timeout=timeout,
                                         follow_redirects=True, trust_env=False) as client:
                video = await fetch_view_safe(client, bvid)
        except Exception as exc:
            return "读取视频信息失败：%s" % type(exc).__name__
        duration = float(video.get("duration") or 0)
        if duration <= 0:
            return "这条视频时长信息异常。"
        if duration > cfg.frames_max_minutes * 60:
            return "视频太长（%.0f 分钟），先不抽帧。" % (duration / 60)
        if bvid in self._scene_tasks and not self._scene_tasks[bvid].done():
            return '画面正在分析，完成后再取结果。'
        self._scene_tasks[bvid] = asyncio.create_task(self._scene_job(bvid, video))
        self._track(self._scene_tasks[bvid])
        self._get_logger().info("B站画面：开始分析 %s（%.0f 秒）", bvid, duration)
        return ("已经开始看画面了（视频 %.0f 秒，抽 %d 帧）。大约 1-3 分钟能好，"
                "现在先回一句自然的话，别等结果；过一会儿再问一次就能拿到具体画面内容。"
                % (duration, cfg.frames_budget))

    def _render_scene(self, row, cached=False):
        """把画面分析结果整理成给模型读的时间轴。"""
        hotspots = ""
        try:
            import json as _json
            span = _json.loads(row["hotspots"] or "[]")
            hotspots = "、".join("%.0f-%.0f 秒" % (a, b) for a, b in span[:4])
        except Exception:
            hotspots = ""
        lines = [
            "《%s》画面内容（共 %d 帧，%s）" % (
                (row["bvid"] or "")[:0] or "", int(row["frames"] or 0),
                "来自缓存" if cached else "刚分析完"),
        ]
        if hotspots:
            lines.append("弹幕最热闹的段落：%s" % hotspots)
        lines.append("")
        lines.append(row["text"].strip())
        lines.append("")
        lines.append("（以上是画面里实际看到的内容，配合语音转写一起用时更完整；"
                     "直接用自己的话讲，不要贴时间戳格式。）")
        return "\n".join(lines)

    async def _scene_job(self, bvid, video):
        async with self._scene_sem:
            return await self._scene_job_inner(bvid,video)

    async def _scene_job_inner(self, bvid, video):
        """后台任务：抽帧 → 视觉描述 → 写库 → 删临时文件。"""
        cfg = self.config.plugin
        out_dir = self._temp_dir() / "frames" / bvid
        video_file = out_dir / "video.m4s"
        try:
            timeout = httpx.Timeout(30.0, connect=10.0)
            async with httpx.AsyncClient(headers=HEADERS, cookies=self._cookies or None, timeout=timeout,
                                         follow_redirects=True, trust_env=False) as client:
                xml = await client.get("https://comment.bilibili.com/%s.xml" % video["cid"])
                entries = fr.parse_entries(xml.content)
                times = [stamp for stamp, _ in entries]
                groups, plan = fr.build_plan(
                    times, float(video["duration"]),
                    budget=min(fr.budget_for(float(video['duration'])),cfg.frames_budget)
                    if cfg.frames_auto_budget else cfg.frames_budget)
                url = await fr.fetch_track_url(client, bvid, video["cid"], max_height=cfg.frames_max_height)
                if not url:
                    self._get_logger().warning("B站画面 %s: 没有可用视频轨", bvid)
                    return None
                out_dir.mkdir(parents=True, exist_ok=True)
                await fr.download(client, url, video_file)
            try:
                names = await asyncio.to_thread(fr.extract, video_file, plan, out_dir)
            finally:
                fr.cleanup([video_file])
            if not names:
                return None
            items = [(float(name[1:-4]), out_dir / name) for name in names]
            hints = fr.pick_highlights(entries, groups)
            chunks = await fr.describe(self.ctx.llm.generate, items, batch=cfg.frames_batch, hints=hints)
            text = "\n".join(chunks)
            if not asr.usable(text):
                self._get_logger().info("B站画面 %s: 描述为空", bvid)
                return None
            import json as _json
            if self.store:
                await asyncio.to_thread(self.store.save_scene, bvid, video["cid"],
                                        float(video["duration"]), len(names), text,
                                        _json.dumps(groups, ensure_ascii=False), time.time(),
                                        _json.dumps(hints, ensure_ascii=False))
            self._get_logger().info("B站画面完成 %s: %d 帧，%d 字", bvid, len(names), len(text))
            return text
        except asyncio.CancelledError:
            raise
        except Exception:
            self._get_logger().exception("B站画面分析失败 %s", bvid)
            return None
        finally:
            await asyncio.to_thread(fr.cleanup,[video_file])
            await asyncio.to_thread(self._cleanup_frames, cfg.frames_keep_seconds)


    # ---------- 深看队列 ----------
    def _enqueue_watch(self, video: dict, source: str) -> None:
        """把视频排进深看队列：先听，再看画面，最后交给学习插件判断要不要学。"""
        if not self.config.plugin.browse_deep_watch:
            return
        if self._watch_queue.full():
            self._get_logger().warning('B站深看队列已满，跳过 %s',video['bvid'])
            return
        self._watch_queue.put_nowait({
            "bvid": video["bvid"], "title": video.get("title", ""),
            "desc": video.get("desc", "") or "", "duration": float(video.get("duration") or 0),
            "source": source,
        })
        self._get_logger().info("B站深看：入队 %s（%s）", video["bvid"], source)

    async def _watch_loop(self):
        """串行消费深看队列，避免 CPU 和带宽被同时打满。"""
        while True:
            try:
                job = await self._watch_queue.get()
                await self._deep_watch(job)
                self._watch_queue.task_done()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._get_logger().exception("B站深看：任务异常")
                await asyncio.sleep(2)

    async def _deep_watch(self, job: dict):
        """转写 → 画面描述 → 自动学习。每步失败都不影响后续流程。"""
        bvid = job["bvid"]
        cfg = self.config.plugin
        if float(job.get('duration') or 0) > max(cfg.frames_max_minutes,cfg.transcript_max_minutes)*60:
            return
        try:
            if cfg.transcript_enabled:
                cached = await asyncio.to_thread(self.store.get_transcript, bvid)
                if not (cached and asr.usable(cached.get("text", ""))):
                    timeout = httpx.Timeout(20.0, connect=10.0)
                    async with httpx.AsyncClient(headers=HEADERS, cookies=self._cookies or None, timeout=timeout,
                                                 follow_redirects=True, trust_env=False) as client:
                        video = await fetch_view_safe(client, bvid)
                    await self._transcribe_job(bvid, video)
        except Exception:
            self._get_logger().debug("B站深看：转写失败 %s", bvid, exc_info=True)
        try:
            if cfg.frames_enabled:
                scene = await asyncio.to_thread(self.store.get_scene, bvid)
                if not (scene and asr.usable(scene["text"])):
                    timeout = httpx.Timeout(20.0, connect=10.0)
                    async with httpx.AsyncClient(headers=HEADERS, cookies=self._cookies or None, timeout=timeout,
                                                 follow_redirects=True, trust_env=False) as client:
                        video = await fetch_view_safe(client, bvid)
                    await self.bili_frames(bvid)
                    task=self._scene_tasks.get(bvid)
                    if task:
                        await asyncio.shield(task)
        except Exception:
            self._get_logger().debug("B站深看：画面失败 %s", bvid, exc_info=True)
        try:
            result = await self.ctx.api.call(
                "local.whale-style.auto_learn", version="1", bvid=bvid, source=job.get("source", "browse"))
            self._get_logger().info("B站深看完成 %s → 学习结果 %s", bvid, result)
        except Exception:
            self._get_logger().debug("B站深看：学习调用失败 %s", bvid, exc_info=True)

    # ---------- 主动浏览 ----------
    async def _browse_loop(self):
        """她自己去 B站 搜点东西；频率交给 store.reserve 统一限流。"""
        cfg = self.config.plugin
        await asyncio.sleep(90)
        while True:
            try:
                if cfg.browse_enabled and self.store and cfg.enabled:
                    now = time.time()
                    reserved = await asyncio.to_thread(
                        self.store.reserve_browse, now, cfg.browse_interval_minutes, cfg.browse_daily_max)
                    if reserved:
                        await self._browse_once(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._get_logger().exception("B站主动浏览异常")
            await asyncio.sleep(300)

    async def _browse_once(self, now):
        cfg = self.config.plugin
        keywords = [k for k in (cfg.browse_keywords or cfg.interests) if k]
        if not keywords:
            await asyncio.to_thread(self.store.finish_browse, now, "浏览：没有可用关键词", [])
            return
        keyword = random.choice(keywords)
        try:
            items = await self._search.search(keyword, 6)
        except Exception as exc:
            await asyncio.to_thread(self.store.finish_browse, now, "浏览失败：%s" % type(exc).__name__, [])
            return
        limit = cfg.browse_max_minutes * 60
        usable_items = [i for i in items if parse_duration(i.get("duration")) <= limit]
        if not usable_items:
            await asyncio.to_thread(self.store.finish_browse, now, "浏览：%s 没有合适视频" % keyword, [])
            return
        choice = random.choice(usable_items[:3])
        try:
            async with httpx.AsyncClient(headers=HEADERS, cookies=self._cookies or None,
                                         timeout=httpx.Timeout(15.0, connect=8.0),
                                         follow_redirects=True, trust_env=False) as client:
                video = await fetch_view_safe(client, choice["bvid"])
        except Exception:
            await asyncio.to_thread(self.store.finish_browse, now, "浏览：读取视频失败", [])
            return
        if float(video.get("duration") or 0) > limit:
            await asyncio.to_thread(self.store.finish_browse, now, "浏览：视频太长", [])
            return
        topic = {
            "bvid": video["bvid"], "title": video["title"], "description": video["desc"],
            "category": video["category"], "author": video["author"], "url":
                "https://www.bilibili.com/video/" + video["bvid"],
            "published": video["published"], "fetched": now,
        }
        await asyncio.to_thread(self.store.finish, now,
                                "浏览：%s → %s" % (keyword, video["title"][:24]), [topic])
        self._get_logger().info("B站主动浏览：%s → %s（%.0fs）",
                                keyword, video["title"][:40], video.get("duration") or 0)
        await self._maybe_deep_read({"bvid": video["bvid"], "title": video["title"],
                                     "description": video["desc"], "published": video["published"]},
                                    ignore_cooldown=True)


    # ---------- 语音转写 ----------
    def _track(self, task):
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


    def _cookie_path(self):
        from pathlib import Path
        return Path(str(self.ctx.paths.data_dir)) / "bili-cookie.json"

    def _load_cookies(self):
        """读本地登录态；缺失或损坏时不带登录，不影响匿名可用。"""
        import json
        path = self._cookie_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._get_logger().warning("B站登录态文件损坏，已按未登录处理")
            return {}
        cookies = data.get("cookies") or {}
        if not cookies.get("SESSDATA"):
            return {}
        self._get_logger().info("B站登录态已加载（SESSDATA 有效期由 B站 控制）")
        scoped = httpx.Cookies()
        for key, value in cookies.items():
            scoped.set(str(key), str(value), domain="api.bilibili.com", path="/")
        for cookie in scoped.jar:
            cookie.secure = True
        return scoped

    def _cleanup_frames(self, max_age_seconds: int) -> None:
        """删掉过期的抽帧目录，避免图片堆积。"""
        root = self._temp_dir() / "frames"
        if not root.exists():
            return
        cutoff = time.time() - max_age_seconds
        for path in root.rglob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
        for path in sorted(root.rglob("*"), reverse=True):
            try:
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                continue

    def _temp_dir(self):
        base = getattr(self.ctx.paths, "data_dir", None)
        path = Path(str(base)) if base else Path(".")
        try:
            instance = path.parents[2]
        except (IndexError, AttributeError):
            instance = path
        return instance / "temp" / "bili-audio"

    def _model_dir(self) -> str:
        configured = (self.config.plugin.transcript_model_dir or "").strip()
        if configured:
            return configured
        plugin_file = Path(__file__).resolve()
        candidates = []
        for parent in list(plugin_file.parents)[:6]:
            candidates.append(parent / ".tools" / "whisper-models" / "small")
            candidates.append(parent / "whisper-models" / "small")
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return str(candidates[0])

    async def _ensure_model(self):
        cfg = self.config.plugin
        if self._whisper is not None:
            return self._whisper
        async with self._whisper_lock:
            if self._whisper is not None:
                return self._whisper
            model_dir = self._model_dir()
            if not asr.model_dir_ready(model_dir):
                self._whisper_failed = "模型目录不可用：%s" % model_dir
                return None
            try:
                self._whisper = await asyncio.to_thread(
                    asr.load_model, model_dir, cfg.transcript_device, cfg.transcript_compute_type)
                self._get_logger().info(
                    "B站转写：模型已加载 %s（%s/%s）", model_dir, cfg.transcript_device, cfg.transcript_compute_type)
                return self._whisper
            except Exception as exc:
                self._whisper_failed = "%s: %s" % (type(exc).__name__, str(exc)[:120])
                self._get_logger().warning("B站转写：模型加载失败 %s", self._whisper_failed)
                return None

    async def _transcribe_job(self, bvid: str, video: dict):
        """后台转写任务：完成后写入 SQLite，音频由 transcribe_video 内部删除。"""
        cfg = self.config.plugin
        model = await self._ensure_model()
        if model is None:
            return None
        async with self._asr_sem:
            try:
                result = await asr.transcribe_video(
                    bvid, video["cid"], model, temp_dir=self._temp_dir(),
                    title=video.get("title", ""), description=video.get("desc", ""),
                    language="zh")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._get_logger().warning("B站转写失败 %s: %s %s", bvid, type(exc).__name__, str(exc)[:120])
                return None
            if not asr.usable(result.text):
                self._get_logger().info("B站转写 %s: 没有可用人声（%d 段）", bvid, result.segments)
                return None
            if self.store:
                await asyncio.to_thread(self.store.save_transcript, bvid, video["cid"], result.model,
                                        result.language, video.get("duration") or 0, result.text)
            self._get_logger().info(
                "B站转写完成 %s: %d 字，用时 %.1fs（视频 %.0fs）",
                bvid, len(result.text), result.elapsed, video.get("duration") or 0)
            return result

    def _asr_quality(self, text: str, duration: float) -> str:
        """按字数和视频时长判断转写可用度：good / thin / poor。"""
        chars = len(re.sub(r"[\s\W_]+", "", text or ""))
        minutes = max(0.5, (duration or 0) / 60.0)
        density = chars / minutes
        if chars < 40 or density < 20:
            return "poor"
        if density < 80:
            return "thin"
        return "good"

    def _asr_text(self, bvid: str, video: dict, text: str, note: str = "") -> str:
        cfg = self.config.plugin
        body = asr.summarize_text(text, cfg.transcript_sample_chars)
        lines = [
            "视频：%s" % (video.get("title") or bvid),
            "时长：%s ｜ 链接：https://www.bilibili.com/video/%s" % (
                asr_duration(video.get("duration") or 0), bvid),
            "",
            "语音转写（本地识别，可能有个别错字）：",
            body,
            "",
            "（资料：本地语音转写，可能有个别错字）",
        ]
        quality = self._asr_quality(text, video.get("duration") or 0)
        if quality == "poor":
            lines.append("⚠️ 转写内容很少且断续：这条视频很可能是歌曲、MV 或多人闲聊，"
                         "不要据此编造具体内容，按弹幕和标题聊即可，可以直说「我听不太清」。")
        elif quality == "thin":
            lines.append("（转写只覆盖了部分语音，回答时不要补全没听到的细节。）")
        if note:
            lines.insert(0, note)
        return "\n".join(lines)

    @Tool(
        "bili_transcript",
        description=(
            "把 B站 视频的语音转成文字，用来看「视频里到底讲了什么」。"
            "当用户问视频说了什么、讲了哪些内容、口播了什么，或需要视频里的具体说法时调用。"
            "首次转写要几十秒（长视频更久），重复提问会直接用缓存。只处理人声，不读画面。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url_or_bvid",
                param_type=ToolParamType.STRING,
                description="B站视频链接、b23.tv 短链或 BV 号。",
                required=True,
            ),
        ],
    )
    async def bili_transcript(self, url_or_bvid: str = "", **kwargs):
        cfg = self.config.plugin
        if not cfg.transcript_enabled:
            return "语音转写功能没有开启。"
        if not self.store:
            return "插件还没准备好。"
        async with self._tool_lock:
            allowed = self._allow()
        if not allowed:
            return "调用太频繁了，等一分钟再试。"
        try:
            bvid = await resolve_bvid(url_or_bvid)
        except Exception as exc:
            return "链接解析失败：%s" % type(exc).__name__
        if not bvid:
            return "没能识别出 BV 号，确认一下是 B站 视频链接吗？"

        cached = await asyncio.to_thread(self.store.get_transcript, bvid)
        if cached and asr.usable(cached.get("text", "")):
            video = {"title": "", "duration": cached.get("duration") or 0}
            return self._asr_text(bvid, video, cached["text"])

        try:
            timeout = httpx.Timeout(15.0, connect=8.0)
            async with httpx.AsyncClient(headers=HEADERS, cookies=self._cookies or None, timeout=timeout,
                                     follow_redirects=True, trust_env=False) as client:
                video = await fetch_view_safe(client, bvid)
        except Exception as exc:
            return "读取视频信息失败：%s" % type(exc).__name__

        duration = float(video.get("duration") or 0)
        if duration > cfg.transcript_max_minutes * 60:
            return "这个视频 %d 分钟，超过转写上限 %d 分钟了，换个短一点的吧。" % (
                duration // 60, cfg.transcript_max_minutes)

        task = self._asr_tasks.get(bvid)
        if task is None or task.done():
            task = asyncio.create_task(self._transcribe_job(bvid, video))
            self._asr_tasks[bvid] = task
            self._transcribe_tasks_cleanup()
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=cfg.transcript_inline_seconds)
        except asyncio.TimeoutError:
            return ("语音还在转写（视频约 %d 分钟）。请在 60 秒后再次调用 bili_transcript，"
                    "届时会直接返回内容；不要用 wait 工具原地等待。" % max(1, int(duration // 60)))
        if result is None:
            return "这个视频没有识别到可用的人声（可能纯音乐或没人说话），我这边只能按弹幕和简介来判断。"
        return self._asr_text(bvid, video, result.text)

    async def _prewarm_transcript(self, bvid: str) -> str:
        """探测视频时顺手后台转写，避免用户追问时等太久。

        Returns:
            str: ``cached`` 已有转写 / ``running`` 后台转写中 / ``none`` 不做转写。
        """
        cfg = self.config.plugin
        if not cfg.transcript_enabled or not self.store:
            return "none"
        cached = await asyncio.to_thread(self.store.get_transcript, bvid)
        if cached and asr.usable(cached.get("text", "")):
            return "cached"
        if bvid in self._asr_tasks and not self._asr_tasks[bvid].done():
            return "running"
        try:
            timeout = httpx.Timeout(15.0, connect=8.0)
            async with httpx.AsyncClient(headers=HEADERS, cookies=self._cookies or None, timeout=timeout,
                                     follow_redirects=True, trust_env=False) as client:
                video = await fetch_view_safe(client, bvid)
        except Exception:
            return "none"
        if float(video.get("duration") or 0) > cfg.transcript_max_minutes * 60:
            return "none"
        self._asr_tasks[bvid] = asyncio.create_task(self._transcribe_job(bvid, video))
        self._transcribe_tasks_cleanup()
        self._get_logger().info("B站转写：已为 %s 预热（后台）", bvid)
        return "running"

    def _transcribe_tasks_cleanup(self):
        for key in [k for k, task in self._asr_tasks.items() if task.done()]:
            self._asr_tasks.pop(key, None)
        if len(self._asr_tasks) > 8:
            for key in list(self._asr_tasks)[: len(self._asr_tasks) - 8]:
                self._asr_tasks.pop(key, None)


def create_plugin():
    return BiliTopics()
