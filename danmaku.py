"""公开信息深读：视频元信息 + 弹幕文本 -> 结构化摘要。全程免登录。"""

import json

import re

import time



import httpx



HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"

DANMAKU_URL = "https://comment.bilibili.com/{cid}.xml"

MAX_XML_BYTES = 8 * 1024 * 1024

SUMMARY_RPC_TIMEOUT_SECONDS = 380



SUMMARIZE_SYSTEM = """你是给聊天机器人整理视频资料的分析器。你只能依据给定的视频元信息和弹幕文本判断内容。

硬性规则：

1. 只能依据给定的简介与弹幕整理内容；不要把弹幕玩笑当事实，也不要写“我没看画面”“仅供参考”这类说明。

2. 弹幕是观众反应，不等于视频内容；区分二者，弹幕里的玩笑不要当成事实。

3. 不编造人名、数据、结论；资料不足就写"信息不足"。

4. 只输出一个 JSON 对象，不要代码块，不要解释。"""



SUMMARIZE_USER = """视频标题：{title}

分区：{category}｜时长：{minutes:.1f} 分钟｜简介：{desc}



弹幕样本（{count} 条，按时间顺序）：

{danmaku}



请输出 JSON，字段如下（全部中文，简洁）：

{{"topic": "一句话说这视频在讲什么，不超过40字",

  "keypoints": ["要点1", "要点2", "要点3"],

  "audience": "观众情绪与提到最多的梗/争议，不超过40字",

  "controversy": "有争议或反常识的点，没有就写无",

  "hook": "适合聊天时抛给对方的一句自然问句，不超过40字"}}"""





def clean(value, limit):

    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).split())[:limit]





def parse_danmaku(xml_text, limit):

    items = []

    for time_str, text in re.findall(r'<d p="([\d.]+)[^"]*">([^<]*)</d>', xml_text):

        body = clean(text, 80)

        if 3 <= len(body) <= 60:

            try:

                items.append((float(time_str), body))

            except ValueError:

                continue

    items.sort(key=lambda x: x[0])

    seen = set()

    unique = []

    for ts, body in items:

        key = re.sub(r"[\W_]+", "", body)

        if not key or key in seen:

            continue

        seen.add(key)

        unique.append((ts, body))

        if len(unique) >= limit:

            break

    return unique





def sample(items, want):

    if len(items) <= want:

        return [body for _, body in items]

    step = len(items) / want

    return [items[min(len(items) - 1, int(i * step))][1] for i in range(want)]





async def fetch_view(client, bvid, *, page=None):

    r = await client.get(VIEW_URL, params={"bvid": bvid})

    payload = r.json()

    if payload.get("code") != 0:

        raise ValueError("view api rejected: %s" % payload.get("message"))

    data = payload.get("data") or {}

    cid = data.get("cid")

    pages = data.get("pages") or []

    if page is not None and (page < 1 or page > max(1, len(pages))):
        raise ValueError("video page does not exist")

    part = pages[(page or 1) - 1] if pages else {}

    if isinstance(part, dict) and part.get("cid"):

        cid = part["cid"]

    if not cid:

        raise ValueError("no cid")

    return {

        "bvid": bvid,

        "aid": data.get("aid"),

        "cid": cid,

        "title": clean(data.get("title"), 140),

        "desc": clean(data.get("desc"), 300),

        "category": clean(data.get("tname"), 40),

        "duration": float((part.get("duration") if page is not None else None) or data.get("duration") or 0),

        "author": clean(((data.get("owner") or {}).get("name")), 60),

        "published": float(data.get("pubdate") or 0),

        "pages": len(pages),

    }





async def fetch_danmaku(client, cid, limit):

    r = await client.get(DANMAKU_URL.format(cid=cid))

    r.raise_for_status()

    if len(r.content) > MAX_XML_BYTES:

        raise ValueError("danmaku too large")

    return parse_danmaku(r.content.decode("utf-8", "replace"), limit)





def build_prompt(video, danmaku_bodies):

    return SUMMARIZE_USER.format(

        title=video["title"],

        category=video["category"] or "未知",

        minutes=video["duration"] / 60 if video["duration"] else 0,

        desc=video["desc"] or "无",

        count=len(danmaku_bodies),

        danmaku="\n".join("- " + body for body in danmaku_bodies) or "无弹幕",

    )





def parse_summary(text):

    raw = (text or "").strip()

    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()

    start, end = raw.find("{"), raw.rfind("}")

    if start < 0 or end <= start:

        raise ValueError("no json in model output")

    data = json.loads(raw[start : end + 1])

    points = data.get("keypoints")

    if not isinstance(points, list):

        points = []

    return {

        "topic": clean(data.get("topic"), 80),

        "keypoints": [clean(p, 80) for p in points if clean(p, 80)][:4],

        "audience": clean(data.get("audience"), 80),

        "controversy": clean(data.get("controversy"), 80),

        "hook": clean(data.get("hook"), 80),

    }





def render(summary, video, danmaku_count):

    parts = [summary["topic"]]

    if summary["keypoints"]:

        parts.append("要点：" + "；".join(summary["keypoints"]))

    if summary["audience"] and summary["audience"] not in ("无", "信息不足"):

        parts.append("观众反应：" + summary["audience"])

    if summary["controversy"] and summary["controversy"] not in ("无", "信息不足"):

        parts.append("争议：" + summary["controversy"])

    parts.append("依据 %d 条公开弹幕与简介" % danmaku_count)

    return "\n".join(parts)





def extract_text(result):

    if isinstance(result, dict):

        return result.get("response") or result.get("content") or ""

    return ""





RETRY_HINT = "\n\n上一次输出不是合法 JSON。这一次严格只输出 JSON 对象本身，不要任何解释、不要思考过程、不要代码块。"





async def deep_read(bvid, settings, generate):

    """返回 (video, summary, rendered, danmaku_count)；generate 为 ctx.llm.generate。"""

    timeout = httpx.Timeout(20.0, connect=10.0)

    async with httpx.AsyncClient(headers=HEADERS, timeout=timeout, follow_redirects=True, trust_env=False) as client:

        video = await fetch_view(client, bvid)

        if video["duration"] > settings.deep_read_max_duration:

            raise ValueError("video too long: %.0fs" % video["duration"])

        items = await fetch_danmaku(client, video["cid"], settings.danmaku_limit)

        want = settings.danmaku_sample if video["duration"] <= 600 else min(settings.danmaku_sample, 80)

        bodies = sample(items, want)

    if not bodies:

        raise ValueError("no danmaku available")

    prompt = build_prompt(video, bodies)

    messages = [

        {"role": "system", "content": SUMMARIZE_SYSTEM},

        {"role": "user", "content": prompt},

    ]

    result = await generate(

        prompt=messages,

        task_name=settings.summary_task,

        temperature=0.2,

        max_tokens=settings.summary_max_tokens,

        rpc_timeout_ms=SUMMARY_RPC_TIMEOUT_SECONDS * 1000,

    )

    if isinstance(result, dict) and result.get("success") is False:

        raise RuntimeError("summary_generation_failed")

    try:

        summary = parse_summary(extract_text(result))

    except ValueError:

        # 长视频的思考过程会吃掉输出预算，用更严格的提示再要一次

        messages_retry = [

            {"role": "system", "content": SUMMARIZE_SYSTEM},

            {"role": "user", "content": prompt + RETRY_HINT},

        ]

        result = await generate(

            prompt=messages_retry,

            task_name=settings.summary_task,

            temperature=0.0,

            max_tokens=min(1500, settings.summary_max_tokens + 400),

            rpc_timeout_ms=SUMMARY_RPC_TIMEOUT_SECONDS * 1000,

        )

        summary = parse_summary(extract_text(result))

    return video, summary, render(summary, video, len(bodies)), len(bodies)





def fmt_duration(seconds):

    total = int(seconds or 0)

    if total >= 3600:

        return "%d:%02d:%02d" % (total // 3600, total % 3600 // 60, total % 60)

    return "%d:%02d" % (total // 60, total % 60)





def fmt_count(value):

    n = int(value or 0)

    if n >= 100000000:

        return "%.1f亿" % (n / 100000000)

    if n >= 10000:

        return "%.1f万" % (n / 10000)

    return str(n)





async def probe(bvid, sample_size=50, danmaku_limit=600, max_duration=14400):

    """按需读取单个视频的公开信息与弹幕样本（不调用模型，1~3 秒）。"""

    timeout = httpx.Timeout(15.0, connect=8.0)

    async with httpx.AsyncClient(headers=HEADERS, timeout=timeout, follow_redirects=True, trust_env=False) as client:

        video = await fetch_view(client, bvid)

        if video["duration"] > max_duration:

            raise ValueError("video too long: %.0fs" % video["duration"])

        items = await fetch_danmaku(client, video["cid"], danmaku_limit)

    bodies = sample(items, max(10, min(int(sample_size or 50), 120)))

    return {"video": video, "bodies": bodies, "danmaku_total": len(items)}





def render_probe(result):

    video = result["video"]

    bodies = result["bodies"]

    lines = [

        "标题：%s" % video["title"],

        "UP主：%s ｜ 时长：%s ｜ 分区：%s ｜ 发布：%s" % (

            video["author"] or "未知",

            fmt_duration(video["duration"]),

            video["category"] or "未知",

            time.strftime("%Y-%m-%d", time.localtime(video["published"])) if video["published"] else "未知",

        ),

        "简介：%s" % (video["desc"] or "无"),

        "链接：https://www.bilibili.com/video/%s" % video["bvid"],

        "",

        "公开弹幕样本（共取到 %d 条，此处 %d 条）：" % (result["danmaku_total"], len(bodies)),

    ]

    lines += ["- " + body for body in bodies]

    lines += ["", "（资料：视频简介与公开弹幕）"]

    return "\n".join(lines)

