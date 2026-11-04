"""免登录的 B站搜索（wbi 签名）与链接解析。"""
import hashlib
import re
import time
import urllib.parse

import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://www.bilibili.com/"}
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")
URL_PATTERN = re.compile(r"https?://[^\s<>\"'）)】]+")

# wbi 签名用的固定置换表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def extract_bvid(text):
    """从链接/BV号/b23.tv 文本里提取 BV 号。"""
    if not text:
        return ""
    m = BV_PATTERN.search(str(text))
    return m.group(0) if m else ""


async def resolve_bvid(text):
    """接受 BV 号、分享文案、完整链接或 b23.tv 短链，返回 BV 号（短链会跟随跳转）。"""
    raw = str(text or "")
    bvid = extract_bvid(raw)
    if bvid:
        return bvid
    match = URL_PATTERN.search(raw)
    if not match:
        return ""
    url = match.group(0).rstrip(".,;:!?，。；：！？、")
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True, trust_env=False) as client:
        resp = await client.get(url)
        return extract_bvid(str(resp.url)) or extract_bvid(resp.text[:20000])


def _mixin_key(img_key, sub_key):
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _sign(params, img_key, sub_key):
    signed = dict(params)
    signed["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5((query + _mixin_key(img_key, sub_key)).encode()).hexdigest()
    return signed


def clean_title(value):
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return " ".join(text.split())[:120]


class Search:
    """带缓存的 wbi 搜索客户端；密钥 30 分钟刷新一次。"""

    def __init__(self):
        self._keys = None
        self._keys_at = 0.0

    async def _ensure_keys(self, client):
        if self._keys and time.time() - self._keys_at < 1800:
            return self._keys
        resp = await client.get(NAV_URL)
        data = (resp.json() or {}).get("data") or {}
        wbi = data.get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise ValueError("未能获取 wbi 密钥")
        self._keys = (img, sub)
        self._keys_at = time.time()
        return self._keys

    async def search(self, keyword, limit=5):
        keyword = " ".join(str(keyword or "").split())[:60]
        if not keyword:
            return []
        limit = max(1, min(int(limit or 5), 10))
        async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True, trust_env=False) as client:
            img, sub = await self._ensure_keys(client)
            params = _sign(
                {"search_type": "video", "keyword": keyword, "page": 1, "page_size": limit},
                img, sub,
            )
            resp = await client.get(SEARCH_URL, params=params)
            payload = resp.json() or {}
            if payload.get("code") != 0:
                raise ValueError("搜索接口拒绝: %s" % payload.get("message"))
            rows = ((payload.get("data") or {}).get("result") or [])
        items = []
        for row in rows[:limit]:
            bvid = row.get("bvid") or ""
            if not BV_PATTERN.fullmatch(bvid):
                continue
            items.append({
                "bvid": bvid,
                "title": clean_title(row.get("title")),
                "author": str(row.get("author") or "")[:40],
                "play": row.get("play") or 0,
                "danmaku_count": row.get("video_review") or 0,
                "duration": str(row.get("duration") or ""),
                "pubdate": row.get("pubdate") or 0,
                "description": " ".join(str(row.get("description") or "").split())[:180],
                "url": "https://www.bilibili.com/video/" + bvid,
            })
        return items
