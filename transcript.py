"""B站视频语音转写：免登录取音频 → 本地 faster-whisper 转写 → 用完即删音频。"""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
PLAYURL = "https://api.bilibili.com/x/player/playurl"
MAX_AUDIO_BYTES = 40 * 1024 * 1024
NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,14}|[\u4e00-\u9fa5]{2,6}")

# 常见术语纠错提示用的高频词（只用于提升专有名词识别，不影响内容）
BASE_PROMPT = "以下是中文游戏与赛事解说，可能出现英文队名、选手ID和数字。"


@dataclass
class Transcript:
    text: str
    language: str
    duration: float
    model: str
    segments: int
    elapsed: float
    # Preserve the audio timeline for joint video analysis. Older tools still
    # use the flat text and segment count.
    spans: tuple[tuple[float, float, str], ...] = ()


def usable(text: str) -> bool:
    """去掉空白与标点后是否还有实质内容。"""
    return len(re.sub(r"[\s\W_]+", "", text or "")) >= 8


def build_prompt(title: str, description: str = "", danmaku: list | None = None) -> str:
    """从标题/简介/弹幕里抽专有名词，拼成 whisper 的 initial_prompt。"""
    terms: list[str] = []
    for source in (title, description, " ".join(danmaku or [])[:400]):
        for match in NAME_PATTERN.findall(source or ""):
            term = match.strip()
            if 2 <= len(term) <= 16 and term not in terms:
                terms.append(term)
    head = "、".join(terms[:12])
    return BASE_PROMPT + ("术语：" + head if head else "")


def pick_audio(dash: dict) -> dict:
    """挑码率最低的音频轨，够转写又不浪费流量。"""
    tracks = [t for t in (dash.get("audio") or []) if t.get("baseUrl")]
    if not tracks:
        raise ValueError("没有可用的音频流")
    tracks.sort(key=lambda t: t.get("bandwidth") or 0)
    return tracks[0]


async def fetch_audio_url(bvid: str, cid: int) -> tuple[str, int]:
    """取音频直链与体积估计（免登录）。"""
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True, trust_env=False) as client:
        resp = await client.get(PLAYURL, params={"bvid": bvid, "cid": cid, "fnval": 16})
        payload = resp.json() or {}
        if payload.get("code") != 0:
            raise ValueError("音频接口拒绝: %s" % payload.get("message"))
        track = pick_audio((payload.get("data") or {}).get("dash") or {})
        return track["baseUrl"], int(track.get("bandwidth") or 0)


async def download_audio(url: str, target: Path) -> int:
    """下载音频到临时文件；超过上限立即中止。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    async with httpx.AsyncClient(headers=HEADERS, timeout=60, follow_redirects=True, trust_env=False) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(target, "wb") as handle:
                async for chunk in resp.aiter_bytes(1 << 18):
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise ValueError("音频超过 %d MB 上限" % (MAX_AUDIO_BYTES // (1024 * 1024)))
                    handle.write(chunk)
    return size


def transcribe_file(path: Path, model, language: str | None = "zh", prompt: str = "") -> Transcript:
    """同步转写（在线程里跑），返回合并后的全文。"""
    started = time.time()
    segments, info = model.transcribe(
        str(path), language=language, beam_size=1, vad_filter=True,
        initial_prompt=prompt or None, condition_on_previous_text=False,
    )
    parts = []
    spans = []
    count = 0
    for segment in segments:
        text = (segment.text or "").strip()
        if text:
            parts.append(text)
            spans.append((float(segment.start), float(segment.end), text))
            count += 1
    return Transcript(
        text="".join(parts), language=getattr(info, "language", language) or language or "",
        duration=float(getattr(info, "duration", 0.0) or 0.0), model=getattr(model, "model_size_or_path", ""),
        segments=count, elapsed=time.time() - started, spans=tuple(spans),
    )


def load_model(model_dir: str, device: str = "cpu", compute_type: str = "int8"):
    """加载 faster-whisper 模型；失败抛异常由调用方降级。"""
    from faster_whisper import WhisperModel

    return WhisperModel(model_dir, device=device, compute_type=compute_type, local_files_only=True)


def temp_path(temp_dir: Path, bvid: str) -> Path:
    return Path(temp_dir) / ("%s-%s.m4a" % (bvid, uuid.uuid4().hex[:8]))


def cleanup_temp(temp_dir: Path, max_age_seconds: int = 3600, max_total_bytes: int = 200 * 1024 * 1024) -> dict:
    """清理临时音频：先按年龄，再按总量从旧到新删。"""
    temp_dir = Path(temp_dir)
    if not temp_dir.exists():
        return {"removed": 0, "freed": 0}
    now = time.time()
    removed = freed = 0
    files = sorted((f for f in temp_dir.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime)
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        if now - stat.st_mtime > max_age_seconds:
            freed += stat.st_size
            path.unlink(missing_ok=True)
            removed += 1
    remaining = [f for f in temp_dir.iterdir() if f.is_file()]
    total = sum(f.stat().st_size for f in remaining)
    for path in sorted(remaining, key=lambda f: f.stat().st_mtime):
        if total <= max_total_bytes:
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        path.unlink(missing_ok=True)
        total -= size
        freed += size
        removed += 1
    return {"removed": removed, "freed": freed}


async def transcribe_video(bvid: str, cid: int, model, *, temp_dir: Path,
                           title: str = "", description: str = "",
                           danmaku: list | None = None, language: str = "zh") -> Transcript:
    """完整流程：取链 → 下载 → 转写 → finally 删除音频。"""
    path = temp_path(Path(temp_dir), bvid)
    url, _bandwidth = await fetch_audio_url(bvid, cid)
    try:
        await download_audio(url, path)
        prompt = build_prompt(title, description, danmaku)
        return await asyncio.to_thread(transcribe_file, path, model, language, prompt)
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def summarize_text(text: str, limit: int = 1200) -> str:
    """截断超长转写，避免塞爆提示词。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def model_dir_ready(path: str) -> bool:
    """模型目录里是否已经有可加载的文件。"""
    folder = Path(path)
    return folder.exists() and any(folder.glob("*.bin"))


def describe_env(path: str) -> dict:
    """给管理页/日志用的环境摘要。"""
    folder = Path(path)
    size = 0
    if folder.exists():
        size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    return {"model_dir": str(folder), "ready": model_dir_ready(path),
            "size_mb": round(size / 1e6), "python_av": _has_av()}


def _has_av() -> bool:
    try:
        import av  # noqa: F401
        return True
    except Exception:
        return False


def ffmpeg_available() -> bool:
    """是否需要额外 ffmpeg（有 av 时不需要）。"""
    return not _has_av()


def remove_leftovers(temp_dir: Path) -> int:
    """启动时清空残留音频。"""
    temp_dir = Path(temp_dir)
    if not temp_dir.exists():
        return 0
    count = 0
    for path in temp_dir.iterdir():
        if path.is_file():
            try:
                path.unlink()
                count += 1
            except OSError:
                pass
    return count
