"""B站数据获取：链接解析、视频信息、字幕、音频流下载。仅依赖 requests。"""

import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE_HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

# 模块级会话：复用 HTTPS 连接
_session = requests.Session()
_buvid_ready = False


def _ensure_buvid():
    """拿 buvid3/buvid4 匿名设备指纹（部分接口如弹幕的风控要求）。失败静默。"""
    global _buvid_ready
    if _buvid_ready:
        return
    try:
        r = _session.get("https://api.bilibili.com/x/frontend/finger/spi",
                         headers=BASE_HEADERS, timeout=10)
        d = (r.json() or {}).get("data") or {}
        b3, b4 = d.get("b_3"), d.get("b_4")
        if b3:
            _session.cookies.set("buvid3", b3, domain=".bilibili.com")
            _session.cookies.set("buvid4", b4 or b3, domain=".bilibili.com")
            _buvid_ready = True
    except Exception:
        pass


class BilibiliError(RuntimeError):
    pass


def _get(url, params=None, sessdata=""):
    _ensure_buvid()
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
    # 视频 所属合集信息（有则返回章节视频列表，供批量总结）
    season = {"id": 0, "title": "", "mid": 0, "videos": []}
    us = d.get("ugc_season") or {}
    if us.get("id"):
        season["id"] = us["id"]
        season["title"] = us.get("title", "")
        season["mid"] = (us.get("mid") or d.get("owner", {}).get("mid") or 0)
        for sec in us.get("sections") or []:
            for ep in sec.get("episodes") or []:
                if ep.get("bvid"):
                    season["videos"].append({
                        "bvid": ep["bvid"],
                        "title": ep.get("title", ""),
                        "duration": ep.get("page", {}).get("duration", 0),
                    })
    return {
        "bvid": d["bvid"],
        "cid_first": d["cid"],
        "title": d["title"],
        "desc": d.get("desc", ""),
        "owner": d["owner"]["name"],
        "owner_mid": d.get("owner", {}).get("mid", 0),
        "duration": d["duration"],
        "season": season,
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


# ---------------- WBI 签名（弹幕/空间等接口需要） ----------------

# 官方混淆表（bilibili-API-collect）
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
_wbi_cache = {"key": "", "ts": 0.0}


def _get_wbi_key() -> str:
    """获取 WBI mixin key（内存缓存 30 分钟）。未登录也能拿到 wbi_img。"""
    if _wbi_cache["key"] and time.time() - _wbi_cache["ts"] < 1800:
        return _wbi_cache["key"]
    try:
        r = _session.get("https://api.bilibili.com/x/web-interface/nav",
                         headers=BASE_HEADERS, timeout=15)
        wbi = ((r.json() or {}).get("data") or {}).get("wbi_img") or {}
    except Exception:
        wbi = {}
    img = str(wbi.get("img_url", "")).rsplit("/", 1)[-1].split(".")[0]
    sub = str(wbi.get("sub_url", "")).rsplit("/", 1)[-1].split(".")[0]
    raw = img + sub
    key = "".join(raw[i] for i in _MIXIN_TAB if i < len(raw))[:32]
    if key:
        _wbi_cache.update(key=key, ts=time.time())
    return key


def wbi_sign(params: dict) -> dict:
    """给参数加上 wts/w_rid 签名。"""
    key = _get_wbi_key()
    p = {k: v for k, v in params.items() if v is not None}
    p["wts"] = int(time.time())
    query = urlencode(sorted(p.items()))
    p["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
    return p


# ---------------- 弹幕密度（高能时间轴） ----------------

def _pb_varint(buf: bytes, i: int):
    val, shift = 0, 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7


def _pb_parse_danmaku(data: bytes) -> list:
    """极简 protobuf 解析：顶层 repeated field1=DanmakuElem，取 field2=进度ms、field7=文本。"""
    out, i, n = [], 0, len(data)
    while i < n:
        try:
            tag, i = _pb_varint(data, i)
            f, wt = tag >> 3, tag & 7
            if wt == 2:
                ln, i = _pb_varint(data, i)
                elem = data[i : i + ln]
                i += ln
                if f != 1:
                    continue
                j, prog, text = 0, None, None
                while j < len(elem):
                    t2, j = _pb_varint(elem, j)
                    f2, w2 = t2 >> 3, t2 & 7
                    if w2 == 0:
                        v, j = _pb_varint(elem, j)
                        if f2 == 2:
                            prog = v
                    elif w2 == 2:
                        l2, j = _pb_varint(elem, j)
                        if f2 == 7:
                            try:
                                text = elem[j : j + l2].decode("utf-8")
                            except UnicodeDecodeError:
                                text = None
                        j += l2
                    elif w2 == 5:
                        j += 4
                    elif w2 == 1:
                        j += 8
                    else:
                        break
                if prog is not None and text:
                    out.append((prog / 1000.0, text))
            elif wt == 0:
                _, i = _pb_varint(data, i)
            elif wt == 5:
                i += 4
            elif wt == 1:
                i += 8
            else:
                break
        except (IndexError, ValueError):
            break
    return out


def get_danmaku_density(cid: int, duration: float, sessdata: str = "", bins: int = 100) -> dict:
    """拉取分段弹幕并统计密度。返回 {total, bins:[每桶条数], hot:[{t,count}]}。"""
    segs = max(1, int(duration // 3600) + 1)
    times = []
    _ensure_buvid()
    headers = dict(BASE_HEADERS)
    if sessdata:
        headers["Cookie"] = f"SESSDATA={sessdata}"
    for seg in range(1, segs + 1):
        try:
            params = wbi_sign({"type": 1, "oid": cid, "segment_index": seg,
                               "timezone_offset": -480})
            r = _session.get("https://api.bilibili.com/x/v2/dm/web/seg.so",
                             params=params, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            times.extend(t for t, _ in _pb_parse_danmaku(r.content))
        except Exception:
            continue
    counts = [0] * bins
    for t in times:
        i = min(int(t / max(duration, 1) * bins), bins - 1)
        counts[i] += 1
    hot = []
    if times:
        step = max(duration / bins, 1)
        ranked = sorted(range(bins), key=lambda i: -counts[i])[:6]
        hot = [{"t": round(i * step), "count": counts[i]} for i in sorted(ranked) if counts[i]]
    return {"total": len(times), "bins": counts, "hot": hot}


# ---------------- 收藏夹 / 合集 解析 ----------------

def parse_collection_url(source: str) -> dict:
    """识别收藏夹/合集链接。返回 {"kind":"fav","media_id":n} / {"kind":"season","sid":n,"mid":n} / None。"""
    s = (source or "").strip()
    m = re.search(r"(?:medialist/detail/ml|favlist\?fid=|media_id=)(\d+)", s) or re.search(r"/(\d+)/info", s + "/")
    if "medialist/detail" in s or "favlist" in s:
        m = re.search(r"(?:ml|fid=|media_id=)(\d+)", s)
        if m:
            return {"kind": "fav", "media_id": int(m.group(1))}
    m = re.search(r"medialist/play/(\d+)/(\d+)", s)
    if m:
        return {"kind": "season", "mid": int(m.group(1)), "sid": int(m.group(2))}
    m = re.search(r"collectiondetail\?sid=(\d+)", s)
    if m:
        mid = re.search(r"mid=(\d+)", s)
        return {"kind": "season", "sid": int(m.group(1)), "mid": int(mid.group(1)) if mid else 0}
    if re.search(r"[?&]sid=(\d+)", s):
        m = re.search(r"[?&]sid=(\d+)", s)
        mid = re.search(r"[?&]mid=(\d+)", s)
        return {"kind": "season", "sid": int(m.group(1)), "mid": int(mid.group(1)) if mid else 0}
    return None


def get_favlist_videos(media_id: int, sessdata: str = "", limit: int = 60) -> list:
    """收藏夹视频列表 [{bvid,title,duration,author}]。"""
    out = []
    pn = 1
    while len(out) < limit:
        d = _get("https://api.bilibili.com/x/v3/fav/resource/list",
                 {"media_id": media_id, "pn": pn, "ps": 20, "keyword": ""}, sessdata)
        medias = d.get("medias") or []
        if not medias:
            break
        for v in medias:
            if v.get("bvid"):
                out.append({"bvid": v["bvid"], "title": v.get("title", ""),
                            "duration": v.get("duration", 0), "author": v.get("upper", {}).get("name", "")})
        if d.get("has_more") is False or len(medias) < 20:
            break
        pn += 1
    return out[:limit]


def get_season_videos(sid: int, mid: int, sessdata: str = "", limit: int = 60) -> list:
    """合集（ugc season）视频列表。"""
    out, page = [], 1
    while len(out) < limit:
        d = _get("https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
                 {"season_id": sid, "mid": mid, "page_no": page, "page_size": 30}, sessdata)
        arcs = d.get("archives") or []
        if not arcs:
            break
        for v in arcs:
            if v.get("bvid"):
                out.append({"bvid": v["bvid"], "title": v.get("title", ""),
                            "duration": v.get("duration", 0), "author": ""})
        if len(arcs) < 30:
            break
        page += 1
    return out[:limit]


def get_up_recent_videos(mid: int, sessdata: str = "", limit: int = 10) -> list:
    """UP主最近投稿（按发布时间倒序）[{bvid,title,created,duration}]。"""
    d = _get("https://api.bilibili.com/x/space/wbi/arc/search",
             wbi_sign({"mid": mid, "pn": 1, "ps": limit, "order": "pubdate",
                       "tid": 0, "keyword": ""}), sessdata)
    out = []
    for v in (d.get("list") or {}).get("vlist") or []:
        out.append({"bvid": v["bvid"], "title": v.get("title", ""),
                    "created": v.get("created", 0), "duration": v.get("length", "")})
    return out
