"""Regression coverage for readiness, conversation boundaries, and media coverage."""
from pathlib import Path
from dataclasses import asdict, replace
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
import asyncio
import importlib
import json
import logging
import sys
import time
import unittest

import av
import httpx

PACKAGE = ModuleType("test_shared_bili")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[PACKAGE.__name__] = PACKAGE
media = importlib.import_module("test_shared_bili.media_watch")
chat = importlib.import_module("test_shared_bili.watch_chat")
Store = importlib.import_module("test_shared_bili.store").Store
Settings = importlib.import_module("test_shared_bili.config").Settings

BV = "BV15vxc6bEbR"
REF = "https://b23.tv/oLynrfz"
CARD = "[小程序] 哔哩哔哩：这条视频总结 链接: " + REF
SECOND_BV = "BV1jcxX6CEyK"
SECOND_CARD = "https://www.bilibili.com/video/" + SECOND_BV


def observation():
    targets = media.frame_targets(31, 24)
    return media.WatchEvidence(BV, 1100, 31.0, 31.0, 0, tuple(targets),
                               ("片头有一只鲸鱼", "结尾对着空鱼缸"), "看着空鱼缸发呆的心情。")


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def test_whole_second_duration_accepts_real_tail_but_not_short_track(self):
        with TemporaryDirectory() as folder:
            out = Path(folder)
            video = out / "short.mp4"
            with av.open(str(video), "w") as output:
                stream = output.add_stream("mpeg4", rate=30)
                stream.width = stream.height = 32
                stream.pix_fmt = "yuv420p"
                # 2.2 seconds of media, reported as 3 seconds by the site.
                for index in range(66):
                    frame = av.VideoFrame(32, 32, "yuv420p")
                    frame.pts = index
                    for plane in frame.planes:
                        plane.update(bytes(plane.buffer_size))
                    output.mux(stream.encode(frame))
                output.mux(stream.encode())
            points = media.frame_targets(3.0, 24)
            original = media.frames.extract(video, points, out / "strict")
            fixed = media.frames.extract(video, points, out / "rounded", declared_duration=3.0)
            self.assertEqual(len(original), len(points) - 1)
            self.assertEqual([float(name[1:-4]) for name in fixed], points)
            short_points = media.frame_targets(4.0, 24)
            rejected = media.frames.extract(video, short_points, out / "short", declared_duration=4.0)
            self.assertNotIn("t%06.1f.jpg" % short_points[-1], rejected)

    def test_real_media_coverage_needs_audio_and_final_frames(self):
        expected = media.frame_targets(159, 24)
        self.assertLess(expected[0], 1)
        self.assertGreater(expected[-1], 158)
        self.assertEqual(len(expected), 24)
        self.assertTrue(all(b - a <= 7 for a, b in zip(expected, expected[1:])))
        e = observation()
        self.assertTrue(e.complete(31, list(e.frame_times)))
        missing_tail = media.WatchEvidence(e.bvid, e.cid, e.duration, e.audio_seconds, 0,
                                          e.frame_times[:-1], e.sections, e.understanding)
        self.assertFalse(missing_tail.complete(31, list(e.frame_times)))
        self.assertFalse(media.WatchEvidence(e.bvid, e.cid, 31, 0, 0, e.frame_times,
                                             e.sections, e.understanding).complete(31, list(e.frame_times)))

    def test_link_resolution_keeps_part_and_drops_tracking(self):
        self.assertEqual(media.parse_ref(f"https://www.bilibili.com/video/{BV}/?p=2&tracking=x"),
                         media.VideoRef(BV, 2))
        self.assertEqual(media.parse_ref(CARD + "?bbid=private-device"), media.VideoRef(REF))
        self.assertIsNone(media.parse_ref("https://not-bili.test/video/" + BV))

    async def test_short_link_never_fetches_external_redirect(self):
        visited = []

        def handler(request):
            visited.append(request.url.host)
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(media.WatchError):
                await media.resolve_ref(client, media.VideoRef(REF))
        self.assertEqual(visited, ["b23.tv"])

    async def test_last_audio_and_visual_window_reaches_fusion(self):
        calls = []

        async def generate(messages, **kwargs):
            calls.append((messages, kwargs))
            return {"success": True, "response": "一段足够明确的声画记录"}

        audio = SimpleNamespace(spans=((0, 1, "片头口播"), (29, 30, "末尾独有台词")))
        frames = [(0.1, Path("first")), (10, Path("middle")), (25, Path("last"))]
        with patch.object(media.frames, "encode_frame", return_value="encoded"):
            await media.understand(generate, frames, audio, 31, 2, "utils")
        self.assertEqual([kwargs["task_name"] for _, kwargs in calls], ["vlm", "vlm", "utils"])
        self.assertNotIn("末尾独有台词", calls[0][0][0]["content"][0]["text"])
        self.assertIn("末尾独有台词", calls[1][0][0]["content"][0]["text"])
        self.assertTrue(any(x["type"] == "image_url" for x in calls[1][0][0]["content"]))
        self.assertIn("31.0", calls[2][0][1]["content"])

    async def test_slow_window_does_not_hold_back_the_next_available_slot(self):
        first_can_finish = asyncio.Event()
        third_started = asyncio.Event()
        in_flight = peak = 0

        async def generate(messages, **kwargs):
            nonlocal in_flight, peak
            if kwargs["task_name"] == "utils":
                return {"success": True, "response": "汇总三段实际声画观察"}
            in_flight += 1
            peak = max(peak, in_flight)
            stamp = messages[0]["content"][1]["text"]
            if stamp.startswith("0.1"):
                await first_can_finish.wait()
            if stamp.startswith("20.0"):
                third_started.set()
            in_flight -= 1
            return {"success": True, "response": "实际画面观察 " + stamp}

        timeline = [(0.1, Path("first")), (10.0, Path("middle")), (20.0, Path("last"))]
        with patch.object(media.frames, "encode_frame", return_value="encoded"):
            pending = asyncio.create_task(media.understand(
                generate, timeline, SimpleNamespace(spans=()), 21, 1, "utils"))
            try:
                await asyncio.wait_for(third_started.wait(), 1)
                self.assertFalse(pending.done())
            finally:
                first_can_finish.set()
                notes, _ = await asyncio.wait_for(pending, 1)
        self.assertEqual(peak, 2)
        self.assertIn("0.1 秒画面", notes[0])
        self.assertIn("20.0 秒画面", notes[-1])

    async def test_fusion_timeout_preserves_completed_visual_and_audio_windows(self):
        async def generate(messages, **kwargs):
            if kwargs["task_name"] == "utils":
                await asyncio.Event().wait()
            return {"success": True, "response": "实际声画记录 " + messages[0]["content"][1]["text"]}

        timeline = [(0.1, Path("first")), (20.0, Path("last"))]
        with patch.object(media.frames, "encode_frame", return_value="encoded"), \
                patch.object(media, "FUSION_WAIT_SECONDS", .02):
            notes, summary = await media.understand(
                generate, timeline, SimpleNamespace(spans=()), 21, 1, "utils")
        self.assertEqual(len(notes), 2)
        self.assertIn("谁做了什么、谁发现或回应", summary.summary)
        self.assertIn("0.1 秒画面", summary.summary)
        self.assertIn("20.0 秒画面", summary.summary)
        self.assertIsNone(summary.role_facts)

    async def test_unreadable_window_keeps_audio_and_the_windows_that_finished(self):
        async def generate(messages, **kwargs):
            if kwargs["task_name"] == "vlm":
                if messages[0]["content"][1]["text"].startswith("5.0"):
                    return {"success": False, "error": "任务 planner 触发硬超时 90s"}
                return {"success": True, "response": "画面里那只桌宠转过头"}
            return {"success": True, "response": "汇总已完成的实际观察"}

        audio = SimpleNamespace(spans=((1, 9, "这一段有台词"),))
        timeline = [(5.0, Path("a")), (15.0, Path("b"))]
        with patch.object(media.frames, "encode_frame", return_value="encoded"):
            notes, summary = await media.understand(generate, timeline, audio, 21, 1, "utils")
        self.assertIn("画面未能看清", notes[0])
        self.assertIn("这一段有台词", notes[0])
        self.assertIn("5.0 秒", notes[0])
        self.assertIn("那只桌宠转过头", notes[1])
        self.assertEqual(summary.summary, "汇总已完成的实际观察")

    async def test_role_binding_survives_a_summary_that_never_finished(self):
        notes = ["0.0–20.0 秒：大肥鱼对着屏幕说：我最喜欢白饭。"]
        binding = json.dumps({"summary": "依据分段原话绑定。", "deepseek_actor": "大肥鱼", "events": [
            {"actor": "大肥鱼", "confirmed": True, "fact": "最喜欢白饭",
             "evidence": "大肥鱼对着屏幕说：我最喜欢白饭。"}]}, ensure_ascii=False)

        async def generate(messages, **kwargs):
            if messages[0]["content"] == media.FUSION_PROMPT:
                await asyncio.Event().wait()
            self.assertEqual(messages[0]["content"], media.ROLE_PROMPT)
            return {"success": True, "response": binding}

        with patch.object(media, "FUSION_WAIT_SECONDS", .02):
            fusion = await media.fuse(generate, notes, "utils", logger=None)
        self.assertEqual(fusion.role_actor, "大肥鱼")
        self.assertEqual(fusion.role_facts, ("最喜欢白饭",))
        self.assertEqual(fusion.summary, "")

    async def test_role_binding_is_skipped_when_no_line_hints_at_the_character(self):
        calls = []

        async def generate(messages, **kwargs):
            calls.append(kwargs["task_name"])
            return {"success": True, "response": "鱼缸里的金鱼在吃食"}

        with patch.object(media, "FUSION_WAIT_SECONDS", .02):
            fusion = await media.fuse(generate, ["0.0–20.0 秒：鱼缸里的金鱼在吃食"], "utils")
        self.assertEqual(calls, ["utils"])
        self.assertIsNone(fusion.role_facts)
        self.assertEqual(fusion.summary, "鱼缸里的金鱼在吃食")

    async def test_fusion_gets_a_fresh_deadline_for_all_four_models(self):
        async def generate(messages, **kwargs):
            self.assertGreater(kwargs["rpc_timeout_ms"], 4 * 90 * 1000)
            await asyncio.sleep(0)
            return {"success": True, "response": "最后一个模型返回的完整剧情汇总"}

        async with asyncio.timeout(0) as watch_deadline:
            summary = await media._fuse(generate, ["片段观察"], "utils", watch_deadline=watch_deadline)
            self.assertGreater(watch_deadline.when() - asyncio.get_running_loop().time(), 4 * 90)
        self.assertEqual(summary.summary, "最后一个模型返回的完整剧情汇总")

    async def test_cached_segments_retry_fusion_without_fetching_media(self):
        cached = replace(observation(), understanding="以下是完整声画分段，按时间排列。旧分析")
        store = Mock()
        store.get_shared_watch.return_value = asdict(cached)
        generate = AsyncMock(return_value={"success": True, "response": "已经重新汇总了整条视频的剧情"})
        ensure_model = AsyncMock()
        with patch.object(media, "resolve_ref", new=AsyncMock(return_value=media.VideoRef(BV))), \
                patch.object(media, "fetch_view", new=AsyncMock(return_value={"duration": 31, "cid": 1100})):
            result = await media.read_video(media.VideoRef(BV), Settings(), store=store, cookies={},
                                            temp_dir=Path("unused"), ensure_model=ensure_model,
                                            asr_sem=None, generate=generate)
        ensure_model.assert_not_awaited()
        generate.assert_awaited_once()
        self.assertEqual(result.sections, cached.sections)
        self.assertEqual(result.understanding, "已经重新汇总了整条视频的剧情")
        store.save_shared_watch.assert_called_once_with(BV, 1100, media.REVISION, asdict(result))

    async def test_existing_full_cache_classifies_roles_without_fetching_media(self):
        notes = ("蓝发女孩说：我最喜欢白饭。主人说：我最喜欢蛋糕。说不清是谁叹了口气。",)
        cached = replace(observation(), sections=notes)
        store = Mock()
        store.get_shared_watch.return_value = asdict(cached)
        payload = {"summary": "两人聊到各自喜欢吃的东西。", "deepseek_actor": "蓝发女孩", "events": [
            {"actor": "蓝发女孩", "fact": "最喜欢白饭", "confirmed": True,
             "evidence": "蓝发女孩说：我最喜欢白饭。"},
            {"actor": "主人", "fact": "最喜欢蛋糕", "confirmed": True,
             "evidence": "主人说：我最喜欢蛋糕。"},
            {"actor": "蓝发女孩", "fact": "叹了口气", "confirmed": False,
             "evidence": "说不清是谁叹了口气。"},
            {"actor": "蓝发女孩", "fact": "最喜欢蛋糕", "confirmed": True,
             "evidence": "主人说：我最喜欢蛋糕。"},
            {"actor": "蓝发女孩", "fact": "喜欢红糖馒头", "confirmed": True,
             "evidence": "蓝发女孩说：我最喜欢红糖馒头。"},
        ]}
        generate = AsyncMock(return_value={"success": True, "response": json.dumps(payload, ensure_ascii=False)})
        ensure_model = AsyncMock()
        video = {"duration": 31, "cid": 1100, "title": "DeepSeek 大肥鱼和主人吃饭"}
        with patch.object(media, "resolve_ref", new=AsyncMock(return_value=media.VideoRef(BV))), \
                patch.object(media, "fetch_view", new=AsyncMock(return_value=video)):
            result = await media.read_video(media.VideoRef(BV), Settings(), store=store, cookies={},
                                            temp_dir=Path("unused"), ensure_model=ensure_model,
                                            asr_sem=None, generate=generate)
        ensure_model.assert_not_awaited()
        generate.assert_awaited_once()
        self.assertEqual(result.role_actor, "蓝发女孩")
        self.assertEqual(result.role_facts, ("最喜欢白饭",))
        self.assertEqual(result.sections, notes)
        store.save_shared_watch.assert_called_once_with(BV, 1100, media.REVISION, asdict(result))

    def test_role_claim_needs_a_recognizable_video_and_an_observed_character(self):
        result = json.dumps({"summary": "有人在桌前吃饭。", "deepseek_actor": "蓝发女孩", "events": [
            {"actor": "蓝发女孩", "fact": "吃饭", "confirmed": True, "evidence": "蓝发女孩在吃饭"},
        ]})
        for title, note in (("午饭", "蓝发女孩在吃饭"), ("大肥鱼", "鱼缸里的金鱼在吃食")):
            with self.subTest(title=title):
                fusion = media._parse_fusion(result, title, "", [note])
                self.assertEqual(fusion.role_actor, "")
                self.assertEqual(fusion.role_facts, ())
        ambiguous = result.replace("蓝发女孩", "她")
        self.assertEqual(media._parse_fusion(ambiguous, "DeepSeek 大肥鱼", "", ["她在吃饭"]).role_facts, ())

    def test_cache_key_cannot_use_wrong_part_or_old_revision(self):
        with TemporaryDirectory() as folder:
            store = Store(Path(folder) / "watch.db")
            store.start()
            store.save_shared_watch(BV, 1100, media.REVISION, {"audio_seconds": 31})
            self.assertEqual(store.get_shared_watch(BV, 1100, media.REVISION), {"audio_seconds": 31})
            self.assertIsNone(store.get_shared_watch(BV, 1101, media.REVISION))
            self.assertIsNone(store.get_shared_watch(BV, 1100, media.REVISION + 1))


    def test_proactive_topic_stays_spent_when_recollected(self):
        now = time.time()
        with TemporaryDirectory() as folder:
            path = Path(folder) / "watch.db"
            store = Store(path)
            store.start()
            topic = dict(bvid=BV, title="测试视频", description="自费开箱", category="", author="",
                         url="https://www.bilibili.com/video/" + BV, published=now, fetched=now)
            store.finish(now, "ok", [topic])
            self.assertEqual(store.pick("private:a", now)["bvid"], BV)
            store.save_summary(BV, {"duration": 31}, {}, "测试视频的内容", 1, now)
            self.assertEqual(store.consume_topics(), [])
            future = now + 8 * 86400
            store.finish(future, "expiry", [])
            store.finish(future, "recrawl", [{**topic, "fetched": future}])
            store = Store(path)
            store.start()
            self.assertIsNone(store.pick("private:a", future))
            self.assertIsNone(store.pick("group:b", future))

    def test_manually_consumed_topic_does_not_reenter_automatic_pool(self):
        now = time.time()
        with TemporaryDirectory() as folder:
            store = Store(Path(folder) / "watch.db")
            store.start()
            topic = dict(bvid=BV, title="测试视频", description="自费开箱", category="", author="",
                         url="https://www.bilibili.com/video/" + BV, published=now, fetched=now)
            store.finish(now, "ok", [topic])
            store.save_summary(BV, {"duration": 31}, {}, "测试视频的内容", 1, now)
            self.assertEqual(store.consume_topics()[0]["bvid"], BV)
            store.finish(now + 1, "recrawl", [{**topic, "fetched": now + 1}])
            self.assertIsNone(store.pick("private:a", now + 1))


class FakeMessages:
    def __init__(self):
        self.rows = []

    async def get_by_id(self, mid, **kwargs):
        return next((r for r in self.rows if r["message_id"] == mid), None)

    async def get_by_time_in_chat(self, **kwargs):
        return list(self.rows)


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages = FakeMessages()
        owner = SimpleNamespace(
            config=SimpleNamespace(plugin=Settings(private_user_ids=["author"], group_ids=["group"])),
            store=object(), ctx=SimpleNamespace(message=self.messages),
            _get_logger=lambda: logging.getLogger("test-watch"),
        )
        self.watch = chat.SharedWatch(owner)
        self.mid = str(uuid4())
        self.display = chat._display_id(self.mid)
        self.add_message(self.mid, CARD)
        self.addAsyncCleanup(self.watch.close)

    def add_message(self, mid, text, **overrides):
        raw = {
            "platform": "qq", "account_id": "bot", "session_id": "test",
            "timestamp": str(time.time()), "message_id": mid,
            "message_info": {"user_info": {"user_id": "author"}},
            "raw_message": [{"type": "text", "data": text}],
            "processed_plain_text": text,
        }
        raw.update(overrides)
        self.messages.rows.append(raw)
        return raw

    def item(self, display=None, text=CARD, image=True):
        parts = [{"type": "text", "text": f'<message msg_id="{display or self.display}">\n{text}'}]
        if image:
            parts.append({"type": "image", "source": {"kind": "cover"}})
        return {"item_type": "UserMessageItem", "parts": parts}

    def planner_args(self, *items):
        return {"session_id": "test", "items": list(items or (self.item(),)),
                "tool_definitions": [{"function": {"name": k}} for k in ("reply", "stop", "bili_video_probe")]}

    def send_args(self):
        return {"message": {"session_id": "test", "reply_to": self.mid}}

    async def test_waits_for_both_modalities_and_hides_only_current_cover(self):
        started, audio, visual = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def read(_):
            started.set()
            await audio.wait()
            await visual.wait()
            return observation()

        self.watch._read = read
        unrelated = self.item("older", "一张别的图片")
        pending = asyncio.create_task(self.watch.planner(self.planner_args(unrelated, self.item())))
        await asyncio.wait_for(started.wait(), 1)
        self.assertEqual((await self.watch.before_send(self.send_args()))["action"], "abort")
        audio.set()
        await asyncio.sleep(0)
        self.assertFalse(pending.done(), "audio alone must not let Planner reply")
        visual.set()
        result = (await asyncio.wait_for(pending, 1))["modified_kwargs"]
        self.assertEqual(result["items"][0]["item_type"], "SystemMessageItem")
        self.assertEqual(result["items"][1], unrelated)
        self.assertEqual([p["type"] for p in result["items"][2]["parts"]], ["text"])
        self.assertNotIn("视频总结", result["items"][2]["parts"][0]["text"])
        self.assertNotIn("bili_video_probe", str(result["tool_definitions"]))
        reply = self.watch.replyer({"session_id": "test", "reply_message_id": self.mid,
                                    "reply_tool_args": {"reply_reference": "按封面先总结"}})["modified_kwargs"]
        self.assertEqual(reply["reply_tool_args"]["reply_reference"], "")
        self.assertEqual(self.watch.replyer({"session_id": "other", "reply_message_id": self.mid}), chat.CONTINUE)
        self.assertEqual((await self.watch.before_send(self.send_args()))["action"], "abort")
        parallel = self.watch.replyer_items({"session_id": "test", "reply_message_id": "", "items": [self.item()]})
        self.assertIn("连贯理解", str(parallel["modified_kwargs"]["items"]))
        self.assertIn("直接给自己的感受", str(parallel["modified_kwargs"]["items"]))
        self.assertNotIn("片段观察", str(parallel["modified_kwargs"]["items"]))
        self.assertNotIn("封面", str(parallel["modified_kwargs"]["items"]))
        self.assertEqual((await self.watch.before_send(self.send_args()))["action"], "continue")

    async def test_first_person_reaction_keeps_video_action_owners(self):
        plot = "镜头前的主人用了另一个 AI。鲸鱼娘发现后问主人为什么要用其他 AI。"

        async def read(_):
            return replace(observation(), understanding=plot)

        self.watch._read = read
        await self.watch.planner(self.planner_args())
        reply = self.watch.replyer_items({"session_id": "test", "reply_message_id": self.mid,
                                          "items": [self.item()]})
        note = reply["modified_kwargs"]["items"][0]["parts"][-1]["text"]
        self.assertIn(plot, note)
        self.assertIn("保留每个角色原来的动作", note)
        self.assertIn("对方分享这段剧情本身不表示现实中发生过", note)
        self.assertIn("直接给自己的感受", note)
        self.assertIn("全条回复用同一个观看者视角", note)
        turn = self.watch._lookup("test", self.mid)
        turn.caption = "这就是你呀"
        self.assertIn("全条用这一个角色的第一人称", self.watch._reply_block(turn))
        turn.caption = "这是别人家的大肥鱼"
        self.assertIn("全条回复用同一个观看者视角", self.watch._reply_block(turn))

    def test_recognized_whale_gets_only_its_own_facts_for_auto_role(self):
        evidence = replace(observation(), understanding="主人说他最喜欢蛋糕。",
                           role_actor="蓝发桌宠", role_facts=("最喜欢白饭", "被拍时比耶"))
        turn = chat.Turn("test", self.mid, self.mid, "author", 0, None, "", False,
                         status="ready", evidence=evidence)
        self.assertEqual(turn.role_mode(), "auto")
        prompt = self.watch._reply_block(turn)
        self.assertIn("全条用这一个角色的第一人称", prompt)
        self.assertIn("蓝发桌宠", prompt)
        self.assertIn("最喜欢白饭", prompt)
        self.assertNotIn("蛋糕", prompt)
        turn.caption = "这是别人家的大肥鱼"
        self.assertEqual(turn.role_mode(), "other")
        self.assertIn("观看者视角", self.watch._reply_block(turn))
        turn.caption = ""
        turn.detail = True
        self.assertEqual(turn.role_mode(), "")
        self.assertIn("主人说他最喜欢蛋糕", self.watch._reply_block(turn))

    async def test_new_topic_releases_pending_reply_without_replaying_video(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def read(_):
            started.set()
            await finish.wait()
            return observation()

        self.watch._read = read
        with patch.object(chat, "POLL_SECONDS", .01):
            old = asyncio.create_task(self.watch.planner(self.planner_args()))
            await asyncio.wait_for(started.wait(), 1)
            self.add_message("new-topic", "那晚饭吃米饭怎么样？")
            result = (await asyncio.wait_for(old, 1))["modified_kwargs"]
        self.assertEqual([chat._tool_name(x) for x in result["tool_definitions"]], ["stop"])
        self.assertEqual((await self.watch.before_send(self.send_args()))["action"], "abort")
        normal = await self.watch.planner(self.planner_args(self.item(), self.item("new-topic", "那晚饭吃米饭怎么样？")))
        self.assertEqual(normal, chat.CONTINUE)
        finish.set()

    async def test_media_failure_never_sends_metadata_guess(self):
        async def read(_):
            raise media.WatchError("no_video_track")

        self.watch._read = read
        await self.watch.planner(self.planner_args())
        outbound = {"message": {**self.send_args()["message"], "message_id": "out",
                                "raw_message": [{"type": "text", "data": "根据封面，这是一部纪录片"}]}}
        edited = (await self.watch.before_send(outbound))["modified_kwargs"]["message"]
        self.assertEqual(edited["raw_message"], [{"type": "text", "data": chat.DEFAULT_FAILURE}])
        self.assertEqual((await self.watch.before_send(outbound))["action"], "abort")

    async def test_explicit_summary_of_latest_video(self):
        async def read(_):
            return observation()

        self.watch._read = read
        await self.watch.planner(self.planner_args())
        self.add_message("summary", "总结一下刚才那个视频")
        own = self.item("out", "看得我心里空空的", image=False)
        own["parts"][0]["text"] = own["parts"][0]["text"].replace(">", ' is_self_message="true">', 1)
        await self.watch.planner(self.planner_args(self.item(), own, self.item("summary", "总结一下刚才那个视频")))
        reply = self.watch.replyer_items({"session_id": "test", "reply_message_id": "summary",
                                         "items": [self.item()]})["modified_kwargs"]
        self.assertIn("片段观察", str(reply["items"]))
        self.assertTrue(chat.wants_detail("总结一下"))
        self.assertFalse(chat.wants_detail("这剧情好怪"))
        self.assertFalse(chat.wants_detail("不用总结，想听你的感受"))

    async def test_new_topic_cleans_host_reused_watch_note(self):
        items = chat.with_system_note([self.item("rice", "吃什么好")], "old video")
        result = await self.watch.planner({"session_id": "test", "items": items})
        self.assertNotIn("old video", str(result["modified_kwargs"]["items"]))
        voice = chat.with_system_note([self.item("rice", "吃什么好")], "old video", chat.REPLY_MARKER)
        result = self.watch.replyer_items({"session_id": "test", "reply_message_id": "rice", "items": voice})
        self.assertNotIn("old video", str(result["modified_kwargs"]["items"]))

    async def test_undirected_group_card_does_not_start_work(self):
        self.messages.rows[0]["message_info"]["group_info"] = {"group_id": "group"}
        self.assertEqual(await self.watch.planner(self.planner_args()), chat.CONTINUE)
        self.assertFalse(self.watch.jobs)

    async def test_group_cancel_without_at_only_affects_the_bound_share(self):
        async def read(_):
            return observation()

        self.watch._read = read
        group = {"group_id": "group"}
        self.messages.rows[0]["message_info"]["group_info"] = group
        self.messages.rows[0]["is_at"] = True
        await self.watch.planner(self.planner_args())
        sender = {"user_info": {"user_id": "author"}, "group_info": group}
        self.add_message("second", SECOND_CARD, message_info=sender, is_at=True)
        second_item = self.item("second", SECOND_CARD)
        await self.watch.planner(self.planner_args(self.item(), second_item))
        await self.watch.prepare_reply({"session_id": "test", "reply_message_id": "second"})
        self.watch.replyer_items({"session_id": "test", "reply_message_id": "second", "items": [second_item]})
        outbound = {"message": {"session_id": "test", "reply_to": "second"}}
        self.add_message("other-cancel", "视频不用看了", reply_to="second",
                         message_info={"user_info": {"user_id": "other"}, "group_info": group})
        self.add_message("cancel-first", "先别回", reply_to=self.mid, message_info=sender)
        self.add_message("group-chat", "晚饭吃什么？", message_info=sender)
        self.assertEqual((await self.watch.before_send(outbound))["action"], "continue")
        self.add_message("cancel-second", "视频不用看了，晚饭吃什么？", message_info=sender)
        self.assertEqual((await self.watch.before_send(outbound))["action"], "abort")

    async def test_summary_after_two_cards_clarifies_and_then_uses_the_quote(self):
        async def read(ref):
            return replace(observation(), bvid=SECOND_BV) if ref.target == SECOND_BV else observation()

        self.watch._read = read
        self.add_message("second", SECOND_CARD)
        second_item = self.item("second", SECOND_CARD)
        await self.watch.planner(self.planner_args(self.item(), second_item))
        self.add_message("ambiguous", "能总结一下吗")
        ambiguous_item = self.item("ambiguous", "能总结一下吗", image=False)
        await self.watch.planner(self.planner_args(self.item(), second_item, ambiguous_item))
        turn = self.watch._lookup("test", "ambiguous")
        self.assertEqual(turn.failure, "ambiguous_video")
        output = await self.watch.before_send({"message": {"session_id": "test", "reply_to": "ambiguous",
                                                          "message_id": "question"}})
        self.assertEqual(output["modified_kwargs"]["message"]["processed_plain_text"], turn.failure_text())
        quoted_segments = [{"type": "reply", "data": {"target_message_id": self.mid}},
                           {"type": "text", "data": "简单讲讲"}]
        self.add_message("quoted", "简单讲讲", raw_message=quoted_segments)
        quoted_item = self.item("quoted", "简单讲讲\n引用里的卡片封面")
        quoted_item["parts"][0]["text"] = quoted_item["parts"][0]["text"].replace(
            ">", f' quote="{self.display}">', 1)
        await self.watch.planner(self.planner_args(self.item(), second_item, ambiguous_item, quoted_item))
        reply = self.watch.replyer_items({"session_id": "test", "reply_message_id": "quoted",
                                         "items": [self.item(), second_item, quoted_item]})
        rendered = str(reply["modified_kwargs"]["items"])
        self.assertIn(BV, rendered)
        self.assertNotIn(SECOND_BV, rendered)
        self.assertNotIn("引用里的卡片封面", rendered)
        self.assertIn("片段观察", rendered)

    async def test_bare_summary_binds_only_before_a_topic_change(self):
        async def read(_):
            return observation()

        self.watch._read = read
        self.add_message("summary", "能总结一下吗")
        await self.watch.planner(self.planner_args(self.item(), self.item("summary", "能总结一下吗")))
        self.assertTrue(self.watch._lookup("test", "summary").detail)
        self.add_message("new-topic", "那晚饭吃米饭怎么样？")
        self.add_message("dinner-summary", "简单总结一下")
        self.assertEqual(await self.watch.planner(
            self.planner_args(self.item(), self.item("dinner-summary", "简单总结一下"))), chat.CONTINUE)

    async def test_pending_watch_uses_close_lead_in_and_followup_focus(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def read(_):
            started.set()
            await finish.wait()
            return observation()

        self.watch._read = read
        timestamp = float(self.messages.rows[0]["timestamp"])
        self.add_message("unrelated", "昨晚那条视频根本没加载出来", timestamp=timestamp - 2)
        self.add_message("lead", "注意看开头的镜头", timestamp=timestamp - 1)
        with patch.object(chat, "POLL_SECONDS", .01):
            pending = asyncio.create_task(self.watch.planner(self.planner_args()))
            await asyncio.wait_for(started.wait(), 1)
            self.add_message("focus", "看她最后那个表情")
            finish.set()
            await asyncio.wait_for(pending, 1)
        # The speculative Replyer can now have the new focus as its last anchor.
        reply = self.watch.replyer_items({"session_id": "test", "reply_message_id": "",
                                         "items": [self.item(), self.item("focus", "看她最后那个表情")]})
        rendered = str(reply["modified_kwargs"]["items"])
        self.assertIn("注意看开头的镜头", rendered)
        self.assertIn("看她最后那个表情", rendered)
        self.assertNotIn("昨晚那条", rendered)
        self.assertNotIn("片段观察", rendered)
        self.assertEqual((await self.watch.before_send(self.send_args()))["action"], "continue")

    async def test_new_card_does_not_inherit_the_previous_card_focus(self):
        async def read(_):
            return observation()

        self.watch._read = read
        self.add_message("old-focus", "注意看结尾")
        self.add_message("second", SECOND_CARD)
        second = self.item("second", SECOND_CARD)
        await self.watch.planner(self.planner_args(self.item(), self.item("old-focus", "注意看结尾"), second))
        await self.watch.prepare_reply({"session_id": "test", "reply_message_id": "second"})
        reply = self.watch.replyer_items({"session_id": "test", "reply_message_id": "second",
                                         "items": [self.item("old-focus", "注意看结尾"), second]})
        self.assertNotIn("注意看结尾", str(reply["modified_kwargs"]["items"]))

    async def test_batched_focus_after_second_card_follows_that_card(self):
        async def read(ref):
            return replace(observation(), bvid=SECOND_BV) if ref.target == SECOND_BV else observation()

        self.watch._read = read
        self.add_message("second", SECOND_CARD)
        self.add_message("focus", "看她最后那个表情")
        second = self.item("second", SECOND_CARD)
        focus = self.item("focus", "看她最后那个表情", image=False)
        await self.watch.planner(self.planner_args(self.item(), second, focus))
        turn = self.watch._lookup("test", "focus")
        self.assertEqual(turn.source_id, "second")
        await self.watch.prepare_reply({"session_id": "test", "reply_message_id": "focus"})
        self.assertEqual(turn.status, "ready")
        self.assertFalse(turn.detail)
        reply = self.watch.replyer_items({"session_id": "test", "reply_message_id": "",
                                         "items": [self.item(), second, focus]})
        rendered = str(reply["modified_kwargs"]["items"])
        self.assertIn(SECOND_BV, rendered)
        self.assertIn("看她最后那个表情", rendered)
        self.assertNotIn(REF, rendered)

    async def test_consecutive_cards_each_get_their_own_reply_in_arrival_order(self):
        targets = []

        async def read(ref):
            targets.append(ref.target)
            if ref.target == SECOND_BV:
                return replace(observation(), bvid=SECOND_BV, understanding="第二条视频才有的情节")
            return replace(observation(), understanding="第一条视频才有的情节")

        self.watch._read = read
        self.add_message("second", SECOND_CARD)
        second = self.item("second", SECOND_CARD)
        # The previous card can be outside the model's selected context.
        await self.watch.planner(self.planner_args(second))
        self.assertEqual(targets, [REF])
        routed = await self.watch.route_planner_reply({"session_id": "test", "output_items": []})
        calls = [item["tool_call"] for item in routed["modified_kwargs"]["output_items"]]
        self.assertEqual([call["func_name"] for call in calls], ["reply", "reply"])
        self.assertEqual([call["args"]["msg_id"] for call in calls], [self.mid, "second"])
        for index, call in enumerate(calls):
            mid = call["args"]["msg_id"]
            args = {"session_id": "test", "reply_message_id": mid, "items": [self.item(), second]}
            await self.watch.prepare_reply(args)
            reply = self.watch.replyer_items(args)
            rendered = str(reply["modified_kwargs"]["items"])
            self.assertIn(("第一条", "第二条")[index] + "视频才有的情节", rendered)
            self.assertNotIn(("第二条", "第一条")[index] + "视频才有的情节", rendered)
            send = {"message": {"session_id": "test", "reply_to": mid}}
            self.assertEqual(await self.watch.before_send(send), chat.CONTINUE)
            self.watch.after_send({**send, "sent": True})
        self.assertEqual(targets, [REF, SECOND_BV])
        await self.watch.planner(self.planner_args(second))
        routed = await self.watch.route_planner_reply({"session_id": "test", "output_items": []})
        self.assertEqual(routed["modified_kwargs"]["output_items"][0]["tool_call"]["func_name"], "stop")

    async def test_new_card_does_not_cancel_a_video_already_being_watched(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def read(ref):
            if ref.target == REF:
                started.set()
                await release.wait()
                return observation()
            return replace(observation(), bvid=SECOND_BV)

        self.watch._read = read
        pending = asyncio.create_task(self.watch.planner(self.planner_args()))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.add_message("second", SECOND_CARD)
            self.assertFalse(await self.watch._superseded(self.watch._lookup("test", self.mid)))
        finally:
            release.set()
            await asyncio.wait_for(pending, 1)
        self.assertEqual(self.watch._lookup("test", self.mid).status, "ready")
        routed = await self.watch.route_planner_reply({"session_id": "test", "output_items": []})
        self.assertEqual(routed["modified_kwargs"]["output_items"][0]["tool_call"]["args"]["msg_id"], self.mid)
        self.watch.after_send({**self.send_args(), "sent": True})
        await self.watch.planner(self.planner_args(self.item("second", SECOND_CARD)))
        self.assertEqual(self.watch._lookup("test", "second").status, "ready")

    async def test_identity_correction_keeps_current_video_context(self):
        async def read(_):
            return observation()

        self.watch._read = read
        self.add_message("identity", "别人家的大肥鱼")
        identity = self.item("identity", "别人家的大肥鱼", image=False)
        await self.watch.planner(self.planner_args(self.item(), identity))
        turn = self.watch._lookup("test", "identity")
        self.assertEqual(turn.source_id, self.mid)
        reply = self.watch.replyer_items({"session_id": "test", "reply_message_id": "identity",
                                         "items": [self.item(), identity]})
        rendered = str(reply["modified_kwargs"]["items"])
        self.assertIn("别人家的大肥鱼", rendered)
        self.assertIn("用户指明这是别人家的鲸鱼娘", rendered)
        self.assertIn("连贯理解", rendered)

    async def test_self_role_retries_a_disclaimer_then_strips_only_the_disclaimer(self):
        async def read(_):
            return observation()

        self.watch._read = read
        caption = "这就是你呀 " + REF
        self.messages.rows[0]["raw_message"] = [{"type": "text", "data": caption}]
        card = self.item(text=caption)
        await self.watch.planner(self.planner_args(card))
        reply = self.watch.replyer_items({"session_id": "test", "reply_message_id": self.mid, "items": [card]})
        self.assertIn("用户明确邀你代入片中的鲸鱼娘", str(reply["modified_kwargs"]["items"]))
        draft = {"session_id": "test", "reply_message_id": self.mid, "retry_count": 0,
                 "response": "我那个时候心里还挺难受…才不是我"}
        self.assertTrue(self.watch.retry_role_reply(draft)["modified_kwargs"]["retry"])
        self.assertEqual(self.watch.retry_role_reply({**draft, "retry_count": 1}), chat.CONTINUE)
        self.assertEqual(self.watch.strip_role_disclaimer(draft)["modified_kwargs"]["response"],
                         "我那个时候心里还挺难受")
        self.assertEqual(self.watch.retry_role_reply({**draft, "response": "我才没脸红呢"}), chat.CONTINUE)

    async def test_long_watch_followup_is_recent_when_the_result_just_arrived(self):
        async def read(_):
            return observation()

        self.watch._read = read
        await self.watch.planner(self.planner_args())
        source = self.messages.rows[0]
        source["timestamp"] = float(source["timestamp"]) - 900
        self.add_message("summary", "能总结一下吗")
        target = self.item("summary", "能总结一下吗", image=False)
        await self.watch.planner(self.planner_args(self.item(), target))
        turn = self.watch._lookup("test", "summary")
        self.assertEqual(turn.source_id, self.mid)
        self.assertTrue(turn.detail)

    async def test_completed_share_routes_to_real_target_instead_of_another_probe(self):
        async def read(_):
            return observation()

        self.watch._read = read
        await self.watch.planner(self.planner_args())
        planned = await self.watch.route_planner_reply({"session_id": "test", "output_items": [
            {"item_type": "FunctionCallItem", "tool_call": {"func_name": "bili_frames", "args": {}}},
        ]})
        call = planned["modified_kwargs"]["output_items"][0]["tool_call"]
        self.assertEqual(call["func_name"], "reply")
        self.assertEqual(call["args"]["msg_id"], self.mid)


if __name__ == "__main__":
    unittest.main()
