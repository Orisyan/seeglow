"""B站数据获取：链接解析、视频信息、字幕、音频流下载。仅依赖 requests。"""

import re
from pathlib import Path

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE_HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

# 模块级会话：复用 HTTPS 连接
_session = requests.Session()


class BilibiliError(RuntimeError):
    pass


def _get(url, params=None, sessdata=""):
    headers = dict(BASE_HEADERS)
    if sessdata:
        headers["Cookie"] = f"SESSDATA={sessdata}"
    try:
        r = _session.get(url, params=params, headers=headers, timeout=20)
    except requests.RequestException as e:
        raise BilibiliError(f"网络请求失败：{e}") from e
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise BilibiliError(f"B站接口错误 {data.get('code')}: {data.get('message')}")
    return data["data"]


def parse_bvid(source: str):
    """从 BV号 / av号 / 视频链接 / b23.tv 短链中解析出 (bvid, page)。"""
    s = (source or "").strip()
    if not s:
        raise BilibiliError("请输入B站视频链接或BV号")

    page = 1
    pm = re.search(r"[?&]p=(\d+)", s)
    if pm:
        page = int(pm.group(1))

    m = re.search(r"(BV[0-9A-Za-z]{10})", s)
    if m:
        return m.group(1), page

    m = re.search(r"av(\d+)", s, re.I)
    if m:
        d = _get("https://api.bilibili.com/x/web-interface/archive/stat", {"aid": m.group(1)})
        return d["bvid"], page

    if "b23.tv" in s:
        r = _session.get(s, headers=BASE_HEADERS, allow_redirects=True, timeout=15)
        return parse_bvid(r.url)

    raise BilibiliError(f"无法从输入中解析出B站视频ID：{source}")


def get_video_info(bvid: str, sessdata="") -> dict:
    d = _get("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid}, sessdata)
    return {
        "bvid": d["bvid"],
        "cid_first": d["cid"],
        "title": d["title"],
        "desc": d.get("desc", ""),
        "owner": d["owner"]["name"],
        "duration": d["duration"],
        "pages": [
            {"page": p["page"], "cid": p["cid"], "part": p.get("part", "")}
            for p in d.get("pages", [])
        ],
    }


def get_cid(info: dict, page: int) -> int:
    for p in info["pages"]:
        if p["page"] == page:
            return p["cid"]
    return info["pages"][0]["cid"] if info["pages"] else info["cid_first"]


def get_subtitle_segments(bvid: str, cid: int, sessdata=""):
    """返回 (segments|None, 字幕列表信息)。优先UP主CC字幕，其次AI字幕。"""
    d = _get("https://api.bilibili.com/x/player/v2", {"bvid": bvid, "cid": cid}, sessdata)
    subs = ((d.get("subtitle") or {}).get("subtitles")) or []
    info_list = [
        {
            "lan": s.get("lan"),
            "lan_doc": s.get("lan_doc", ""),
            "ai": str(s.get("lan", "")).startswith("ai"),
        }
        for s in subs
    ]
    if not subs:
        return None, info_list

    subs_sorted = sorted(subs, key=lambda s: 1 if str(s.get("lan", "")).startswith("ai") else 0)
    sub = subs_sorted[0]
    url = sub.get("subtitle_url") or ""
    if url.startswith("//"):
        url = "https:" + url
    if not url:
        return None, info_list

    r = _session.get(url, headers=BASE_HEADERS, timeout=20)
    r.raise_for_status()
    body = r.json().get("body") or []
    segments = [
        {
            "start": float(item.get("from", 0)),
            "end": float(item.get("to", 0)),
            "text": (item.get("content") or "").strip(),
        }
        for item in body
    ]
    segments = [s for s in segments if s["text"]]
    return (segments or None), info_list


def get_audio_url(bvid: str, cid: int, sessdata="") -> str:
    """取音频流地址。用于 AI 听内容，选最低码率即可（下载更快，语音信息无损）。"""
    d = _get(
        "https://api.bilibili.com/x/player/playurl",
        {"bvid": bvid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 1},
        sessdata,
    )
    dash = d.get("dash") or {}
    candidates = [c for c in (dash.get("audio") or []) if isinstance(c, dict)]
    if not candidates:
        raise BilibiliError("未取到音频流地址，可能需要登录 Cookie（SESSDATA）")
    best = min(candidates, key=lambda a: a.get("bandwidth", 0))
    url = best.get("baseUrl") or best.get("base_url") or ""
    if not url:
        raise BilibiliError("音频流地址为空")
    return url


def download_audio(url, dest: Path, sessdata="", progress_cb=None, stop_check=None) -> Path:
    headers = dict(BASE_HEADERS)
    if sessdata:
        headers["Cookie"] = f"SESSDATA={sessdata}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _session.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if stop_check and stop_check():
                        raise BilibiliError("已取消")
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(done / total, 1.0))
    except requests.RequestException as e:
        raise BilibiliError(f"音频下载失败：{e}") from e
    return dest


def fmt_ts(sec: float) -> str:
    sec = max(int(sec), 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def segments_to_text(segments) -> str:
    return "\n".join(f"[{fmt_ts(s['start'])}] {s['text']}" for s in segments)


def safe_filename(name: str, maxlen: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" ._")
    return name[:maxlen] or "untitled"
