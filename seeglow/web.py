"""本地 Web 服务：FastAPI + 单页前端。"""

import json as _json
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import pipeline, tasks
from .config import PRESETS, load_config, save_config

app = FastAPI(title="SeeGlow API")
STATIC_DIR = Path(__file__).resolve().parent / "static"


class ParseReq(BaseModel):
    url: str


class StartReq(BaseModel):
    url: str
    page: Optional[int] = None
    all_pages: bool = False
    style: Optional[str] = "general"


class BatchReq(BaseModel):
    items: list  # [{"bvid":..., "title":...}]
    style: Optional[str] = "general"


class FeedReq(BaseModel):
    ups: list  # UP主主页链接或 uid 列表
    days: Optional[int] = 14


class ConfigReq(BaseModel):
    provider: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    sessdata: Optional[str] = None
    output_dir: Optional[str] = None


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/parse")
def parse_video(req: ParseReq):
    """解析链接。支持：视频/BV/av/短链（返回视频信息+分P+合集）；
    收藏夹或合集链接（返回 collection 列表）。"""
    from . import bilibili
    from .config import load_config

    sess = load_config().get("sessdata", "")
    try:
        # 先看是不是收藏夹/合集直达链接
        col = bilibili.parse_collection_url(req.url)
        if col:
            if col["kind"] == "fav":
                items = bilibili.get_favlist_videos(col["media_id"], sess)
                if not items:
                    raise HTTPException(400, "收藏夹为空或非公开（需在B站设为公开）")
                return {"collection": {"kind": "fav", "title": f"收藏夹 {col['media_id']}", "items": items}}
            if col["kind"] == "season":
                if not col.get("mid"):
                    raise HTTPException(400, "合集链接缺少 mid，请粘贴合集内任一视频链接，会自动识别合集")
                items = bilibili.get_season_videos(col["sid"], col["mid"], sess)
                if not items:
                    raise HTTPException(400, "未取到合集视频（可能非公开）")
                return {"collection": {"kind": "season", "title": f"合集 {col['sid']}", "items": items}}

        bvid, _page = bilibili.parse_bvid(req.url)
        info = bilibili.get_video_info(bvid, sess)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))
    season = info.get("season") or {}
    return {
        "bvid": info["bvid"],
        "title": info["title"],
        "owner": info["owner"],
        "duration": info["duration"],
        "pages": info["pages"] or [{"page": 1, "cid": info["cid_first"], "part": ""}],
        # 视频属于合集时带出整个合集（前端可一键批量）
        "season": (
            {"title": season["title"], "count": len(season["videos"]), "videos": season["videos"]}
            if season.get("id") and len(season.get("videos") or []) > 1 else None
        ),
    }


@app.post("/api/start")
def start(req: StartReq):
    if not req.url.strip():
        raise HTTPException(400, "请输入视频链接")
    tid = tasks.create_task()

    def job(progress_cb):
        return pipeline.run_pipeline(
            req.url,
            {"page": req.page, "all_pages": req.all_pages, "style": req.style or "general"},
            progress_cb,
            stop_check=lambda: tasks.is_stopped(tid),
        )

    tasks.run_in_background(tid, job)
    return {"task_id": tid}


@app.post("/api/start_batch")
def start_batch(req: BatchReq):
    """批量总结合集/收藏夹视频（上限 30 支）。"""
    items = [i for i in (req.items or []) if i.get("bvid")][:30]
    if not items:
        raise HTTPException(400, "批量列表为空")
    tid = tasks.create_task()

    def job(progress_cb):
        return pipeline.run_batch_pipeline(
            items, req.style or "general", progress_cb,
            stop_check=lambda: tasks.is_stopped(tid),
        )

    tasks.run_in_background(tid, job)
    return {"task_id": tid, "count": len(items)}


@app.post("/api/start_file")
async def start_file(file: UploadFile = File(...), style: str = Form("general")):
    """总结本地音视频文件（拖拽/选择上传，存临时目录，用完即删）。"""
    import os as _os
    import tempfile as _tf

    suffix = "." + (file.filename or "x").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    if suffix and suffix not in {".mp4", ".mkv", ".flv", ".mov", ".avi", ".webm",
                                 ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".m4s", ".ts"}:
        raise HTTPException(400, f"暂不支持 {suffix} 格式")
    dest = Path(_tf.gettempdir()) / f"seeglow_upload_{int(time.time())}_{_os.getpid()}{suffix}"
    try:
        size = 0
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > 4 * 1024 * 1024 * 1024:
                    raise HTTPException(400, "文件超过 4GB 上限")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "文件为空")
    finally:
        await file.close()

    title = (file.filename or "本地文件").rsplit(".", 1)[0]
    tid = tasks.create_task()

    def job(progress_cb):
        try:
            return pipeline.run_file_pipeline(dest, title, style, progress_cb,
                                              stop_check=lambda: tasks.is_stopped(tid))
        finally:
            try:
                dest.unlink()
            except OSError:
                pass

    tasks.run_in_background(tid, job)
    return {"task_id": tid}


@app.get("/api/danmaku")
def danmaku(bvid: str = Query(...), cid: int = Query(0), duration: float = Query(0)):
    """弹幕高能时间轴（前端总结完成后展示）。"""
    from . import bilibili
    from .config import load_config

    try:
        if not cid:
            info = bilibili.get_video_info(bvid, load_config().get("sessdata", ""))
            cid = info["cid_first"]
            duration = duration or info["duration"]
        d = bilibili.get_danmaku_density(cid, max(duration, 1),
                                         load_config().get("sessdata", ""))
        if not d["total"]:
            return {"ok": False, "msg": "该视频暂无弹幕"}
        return {"ok": True, **d, "duration": duration}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:120]}


@app.post("/api/feed_check")
def feed_check(req: FeedReq):
    """追更：检查 UP 主最近 days 天的新投稿（需要配置 SESSDATA）。"""
    import re as _re
    import time as _time

    from . import bilibili
    from .config import load_config

    sess = load_config().get("sessdata", "")
    out, errors = [], []
    cutoff = _time.time() - (req.days or 14) * 86400
    for u in (req.ups or [])[:20]:
        s = str(u).strip()
        m = _re.search(r"space\.bilibili\.com/(\d+)", s) or _re.search(r"^(\d{3,})$", s)
        if not m:
            errors.append({"up": s, "error": "无法识别 UP 主（粘贴主页链接或纯数字 uid）"})
            continue
        mid = int(m.group(1))
        try:
            vids = bilibili.get_up_recent_videos(mid, sess, limit=10)
            fresh = [v for v in vids if v.get("created", 0) >= cutoff]
            for v in fresh:
                out.append({**v, "mid": mid})
        except Exception as e:
            msg = str(e)
            if "-352" in msg or "风控" in msg:
                msg = "该接口需要登录态：请在设置中配置 B站 Cookie SESSDATA"
            errors.append({"up": s, "error": msg[:100]})
    out.sort(key=lambda v: -v.get("created", 0))
    return {"videos": out, "errors": errors, "need_sessdata": any("-352" in e["error"] or "SESSDATA" in e["error"] for e in errors)}


@app.get("/api/task/{tid}")
def task_status(tid: str):
    t = tasks.get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.post("/api/stop/{tid}")
def stop_task(tid: str):
    tasks.request_stop(tid)
    return {"ok": True}


class AskReq(BaseModel):
    file: str
    question: str
    history: Optional[list] = None


@app.post("/api/ask")
def ask(req: AskReq):
    """基于已保存的视频上下文回答问题（引用时间点）。"""
    import json as _json

    from .summarize import LLMClient

    q = (req.question or "").strip()
    if not q:
        raise HTTPException(400, "请输入问题")

    base = _output_dir().resolve()
    ctx_path = (base / (req.file + ".ctx.json")).resolve()
    if ctx_path.parent != base or not ctx_path.exists():
        raise HTTPException(404, "未找到该视频的问答上下文")

    data = _json.loads(ctx_path.read_text(encoding="utf-8"))
    items = data.get("items") or []
    if not items:
        raise HTTPException(404, "该笔记没有可用的问答上下文")

    # 简单相关性挑选：按问题字符与条目文本的重叠度排序，控制总长度
    from .bilibili import fmt_ts

    def _fmt(s):
        return fmt_ts(float(s))

    def score(text: str) -> int:
        return sum(text.count(q[i : i + 2]) for i in range(len(q) - 1))

    scored = sorted(items, key=lambda it: -score(it["text"]))
    budget, chosen = 12000, []
    for it in scored:
        if budget <= 0:
            break
        piece = f"[{_fmt(it['start'])}-{_fmt(it['end'])}] {it['text']}"
        if len(piece) > budget:
            piece = piece[:budget]
        chosen.append(piece)
        budget -= len(piece) + 1

    chosen.sort()  # 按时间字符串顺序（mm:ss 字典序即可）
    context = "\n".join(chosen)

    cfg = load_config()
    client = LLMClient(
        cfg.get("api_base"), cfg.get("api_key"), cfg.get("model"), cfg.get("temperature", 0.3)
    )

    history_msgs = []
    for h in (req.history or [])[-6:]:
        if isinstance(h, dict) and h.get("q") and h.get("a"):
            history_msgs.append({"role": "user", "content": h["q"]})
            history_msgs.append({"role": "assistant", "content": str(h["a"])[:1500]})

    system = (
        "你是「拾光 SeeGlow」的视频内容助手。只依据提供的视频内容片段回答用户问题，"
        "引用内容时标注 [mm:ss] 时间点；如果上下文中没有相关信息，请明确说明"
        "“视频中未涉及”，不要编造。回答使用中文 Markdown。"
    )
    user = f"以下是视频《{data.get('title','')}》的内容片段：\n\n{context}\n\n用户的问题：{q}"

    try:
        answer = client.chat(
            [{"role": "system", "content": system}] + history_msgs + [{"role": "user", "content": user}]
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"answer": answer}


class CardsReq(BaseModel):
    file: str
    count: Optional[int] = 10


class SaveExportReq(BaseModel):
    name: str            # 默认文件名（含扩展名）
    content: str         # 文件内容（文本）
    title: Optional[str] = ""  # 对话框标题


def _native_save_dialog(default_name: str, title: str = "保存文件"):
    """系统「另存为」对话框，返回用户选择的路径；取消返回 None。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            path = filedialog.asksaveasfilename(
                title=title, initialfile=default_name, confirmoverwrite=True,
                parent=root,
            )
        finally:
            root.destroy()
        return path or None
    except Exception:
        return None


@app.post("/api/save_export")
def save_export(req: SaveExportReq):
    """桌面版导出：弹系统保存对话框直接写盘（绕开 WebView 的下载限制）。"""
    name = (req.name or "seeglow.txt").strip() or "seeglow.txt"
    path = _native_save_dialog(name, req.title or "保存文件")
    if not path:
        return {"ok": False, "cancelled": True}
    try:
        Path(path).write_text(req.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"写入失败：{e}")
    return {"ok": True, "path": path}


# ---------------- 图文改写（小红书/公众号等） ----------------

class RewriteReq(BaseModel):
    file: str
    target: str  # xiaohongshu / gongzhonghao / weibo / zhihu


REWRITE_GUIDES = {
    "xiaohongshu": (
        "把这份视频笔记改写成一篇小红书笔记。要求：\n"
        "- 第一行给标题：20字内，吸睛，含1~2个emoji\n"
        "- 正文300~500字，口语化、真诚分享感，分成4~6个短段落，每段开头带一个emoji\n"
        "- 保留最有信息量的要点和时间点（[mm:ss]格式），像在给朋友安利这支视频\n"
        "- 结尾单独一行给3~6个#标签\n"
        "- 不要编造笔记中没有的信息"
    ),
    "gongzhonghao": (
        "把这份视频笔记改写成一篇公众号文章。要求：\n"
        "- 标题：吸引点击但不做标题党，25字内\n"
        "- 开头一段60字以内的导语，抛出问题或痛点\n"
        "- 正文800~1200字，用2~3个小标题组织，逻辑顺畅，要点完整保留\n"
        "- 关键处保留时间点引用，如（04:15处提到）\n"
        "- 结尾一段总结+引导「关注我看更多视频笔记」"
    ),
    "weibo": (
        "把这份视频笔记改写成一条微博。要求：\n"
        "- 200字以内，观点鲜明，适合快速转发\n"
        "- 可用1~2个emoji，结尾带2~3个#标签\n"
        "- 保留最有冲击力的1~2个数据或金句"
    ),
    "zhihu": (
        "把这份视频笔记改写成一篇知乎回答（问题是：如何看待/评价这支视频的内容？）。要求：\n"
        "- 先给结论（一句话加粗）\n"
        "- 然后3~5条论据展开，引用视频中的具体内容和时间点\n"
        "- 语气客观理性，结尾注明「以上内容整理自视频，建议观看原片」\n"
        "- 总长400~700字"
    ),
}


@app.post("/api/rewrite")
def rewrite(req: RewriteReq):
    """把已保存的视频笔记改写成平台文案（小红书/公众号/微博/知乎）。"""
    from .summarize import LLMClient

    guide = REWRITE_GUIDES.get(req.target)
    if not guide:
        raise HTTPException(400, f"不支持的改写目标：{req.target}")

    base = _output_dir().resolve()
    fname = req.file if req.file.endswith(".md") else req.file + ".md"
    md_path = (base / fname).resolve()
    if md_path.parent != base or not md_path.exists():
        raise HTTPException(404, "未找到该笔记")
    summary = md_path.read_text(encoding="utf-8")[:8000]

    cfg = load_config()
    client = LLMClient(cfg.get("api_base"), cfg.get("api_key"), cfg.get("model"),
                       cfg.get("temperature", 0.3))
    prompt = f"{guide}\n\n【视频笔记】\n{summary}"
    try:
        text = client.chat([
            {"role": "system", "content": "你是资深新媒体编辑，擅长把内容改写成不同平台的爆款文案。直接输出正文，不要解释。"},
            {"role": "user", "content": prompt},
        ])
    except Exception as e:
        raise HTTPException(500, f"改写失败：{str(e)[:120]}")
    return {"text": text}


# ---------------- 字幕导出（SRT / VTT） ----------------

def _fmt_srt_ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_vtt_ts(sec: float) -> str:
    return _fmt_srt_ts(sec).replace(",", ".")


@app.get("/api/export_srt")
def export_srt(name: str = Query(...), fmt: str = Query("srt")):
    """导出该笔记的视频字幕（仅「B站字幕」来源的笔记有完整字幕）。"""
    base = _output_dir().resolve()
    fname = name if name.endswith(".md") else name + ".md"
    ctx_path = (base / (fname + ".ctx.json")).resolve()
    if ctx_path.parent != base or not ctx_path.exists():
        raise HTTPException(404, "未找到字幕数据")
    items = (_json.loads(ctx_path.read_text(encoding="utf-8")) or {}).get("items") or []
    # AI直听路径的分段是小结而非逐句字幕；字幕路径是逐句（start/end/text）
    has_ts = all(("start" in it and "end" in it) for it in items) and len(items) >= 5
    if not has_ts:
        raise HTTPException(400, "该笔记由 AI 直听生成，没有逐句字幕（B站有字幕的视频才能导出）")

    fmt = "vtt" if fmt == "vtt" else "srt"
    fmt_ts = _fmt_vtt_ts if fmt == "vtt" else _fmt_srt_ts
    lines = ["WEBVTT\n"] if fmt == "vtt" else []
    for i, it in enumerate(items, 1):
        a, b = fmt_ts(it["start"]), fmt_ts(it["end"])
        lines.append(f"{i}\n{a} --> {b}\n{it['text']}\n")
    content = "\n".join(lines)
    return {"ok": True, "fmt": fmt, "content": content,
            "filename": fname.replace(".md", f".{fmt}")}


@app.post("/api/flashcards")
def flashcards(req: CardsReq):
    """从已保存的视频笔记生成 Anki 闪卡（Q&A 对）。"""
    from .summarize import LLMClient

    base = _output_dir().resolve()
    fname = req.file if req.file.endswith(".md") else req.file + ".md"
    md_path = (base / fname).resolve()
    ctx_path = (base / (fname + ".ctx.json")).resolve()
    if md_path.parent != base or not md_path.exists():
        raise HTTPException(404, "未找到该笔记")
    summary = md_path.read_text(encoding="utf-8")[:6000]
    ctx_text = ""
    if ctx_path.exists():
        items = (_json.loads(ctx_path.read_text(encoding="utf-8")) or {}).get("items") or []
        ctx_text = "\n".join(
            f"[{it.get('start', 0):.0f}s] {str(it.get('text', ''))[:200]}" for it in items[:60]
        )[:8000]

    cfg = load_config()
    client = LLMClient(cfg.get("api_base"), cfg.get("api_key"), cfg.get("model"),
                       cfg.get("temperature", 0.3))
    prompt = (
        f"以下是一支视频的总结笔记与内容片段。请据此出 {req.count or 10} 张中文问答闪卡，"
        "用于复习视频中的知识点。\n\n【总结笔记】\n" + summary
        + ("\n\n【内容片段】\n" + ctx_text if ctx_text else "")
        + "\n\n【输出要求】只输出 JSON 数组，不要任何解释或代码块标记，格式："
        '[{"q":"问题","a":"答案（50字内）"}]'
    )
    try:
        raw = client.chat([{"role": "system", "content": "你是出题助手，只输出 JSON。"},
                           {"role": "user", "content": prompt}])
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").lstrip("json").strip()
        start, end = raw.find("["), raw.rfind("]")
        cards = _json.loads(raw[start : end + 1] if start >= 0 else "[]")
        cards = [c for c in cards if isinstance(c, dict) and c.get("q") and c.get("a")]
    except Exception as e:
        raise HTTPException(500, f"闪卡生成失败：{str(e)[:120]}")
    if not cards:
        raise HTTPException(500, "模型未返回有效闪卡，请重试")
    return {"cards": cards}


def _output_dir() -> Path:
    d = Path(load_config()["output_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.get("/api/history")
def history():
    out = []
    for f in sorted(_output_dir().glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        out.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


@app.get("/api/output")
def read_output(name: str = Query(...)):
    base = _output_dir().resolve()
    path = (base / name).resolve()
    if path.parent != base or path.suffix != ".md" or not path.exists():
        raise HTTPException(404, "文件不存在")
    # 顺带返回 ctx.json 里的视频信息（历史笔记也能跳时间戳/显示弹幕条）
    video_url = ""
    ctx_path = base / (name + ".ctx.json")
    if ctx_path.exists():
        try:
            c = _json.loads(ctx_path.read_text(encoding="utf-8"))
            video_url = c.get("url") or ""
        except Exception:
            pass
    return {"name": name, "content": path.read_text(encoding="utf-8"), "video_url": video_url}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "***"
    return key[:5] + "***" + key[-4:]


@app.get("/api/config")
def get_config():
    cfg = load_config()
    cfg["api_key_masked"] = _mask_key(cfg.get("api_key", ""))
    cfg.pop("api_key", None)
    cfg.pop("sessdata", None)
    cfg["has_sessdata"] = bool(load_config().get("sessdata"))
    return {"config": cfg, "presets": PRESETS}


@app.post("/api/config")
def update_config(req: ConfigReq):
    update = {k: v for k, v in req.model_dump().items() if v not in (None, "")}
    # 空字符串的 api_key/sessdata 表示“保持不变”，避免误清空
    if req.api_key == "":
        update.pop("api_key", None)
    if req.sessdata == "":
        update.pop("sessdata", None)
    cfg = save_config(update)
    cfg["api_key_masked"] = _mask_key(cfg.get("api_key", ""))
    cfg["has_sessdata"] = bool(cfg.get("sessdata"))
    cfg.pop("api_key", None)
    cfg.pop("sessdata", None)
    return cfg


def _disable_quick_edit():
    """Windows 控制台的“快速编辑模式”会在用户点击窗口时冻结程序写日志，
    导致整个服务假死。启动时主动关闭它。"""
    import sys

    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT = 0x0040
            ENABLE_EXTENDED_FLAGS = 0x0080
            new_mode = (mode.value & ~ENABLE_QUICK_EDIT) | ENABLE_EXTENDED_FLAGS
            kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


def main(host="127.0.0.1", port=8765):
    import uvicorn

    _disable_quick_edit()
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
