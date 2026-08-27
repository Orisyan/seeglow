"""Web 服务：FastAPI + 单页前端。

两种运行模式：
- 私有模式（默认）：本机自用，配置存 config.json，笔记落盘 可直接读历史
- 公共模式（SEELOW_PUBLIC=1）：部署到服务器供多设备/他人使用，自带凭据（BYOK）、
  笔记按任务令牌隔离、每 IP 限流、并发与批量上限
"""

import json as _json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import pipeline, tasks
from .config import PRESETS, load_config, save_config

app = FastAPI(title="SeeGlow API")
STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------- 网站版付费模式（服务器内置官方 API，赞助/激活码解锁） ----------------
# SEELOW_SITE_PAID=1 时启用：未激活访客所有总结类接口返回 402；
# 已激活访客（票据在请求头）免 Key 使用服务端配置的官方 API。
SITE_PAID_MODE = os.getenv("SEELOW_SITE_PAID", "") in ("1", "true", "on", "yes")

def _auth(request: Request) -> dict | None:
    if not SITE_PAID_MODE:
        return {"kind": "open"}
    from . import pro_web
    return pro_web.check_request_auth(request)

# ---------------- 公共模式开关与限额 ----------------
PUBLIC_MODE = os.getenv("SEELOW_PUBLIC", "") in ("1", "true", "on", "yes")

MAX_CONCURRENT = int(os.getenv("SEELOW_MAX_CONCURRENT", "2"))   # 全局同时任务数
RATE_PER_IP = int(os.getenv("SEELOW_RATE_PER_IP", "10"))        # 每 IP 每 10 分钟启动数
RATE_WINDOW = 600
MAX_BATCH_PUBLIC = 6                                            # 公共模式单批上限
UPLOAD_LIMIT = 200 * 1024 * 1024                                # 公共模式上传上限 200MB

# 任务令牌：task_id -> token；公共模式下笔记/问答等只放行持票人
_task_tokens: dict = {}
_lock = threading.Lock()

_rate: dict = {}


def client_ip(request: Request) -> str:
    xf = request.headers.get("x-forwarded-for", "")
    return xf.split(",")[0].strip() or (request.client.host if request.client else "?")


def rate_hit(ip: str) -> bool:
    """True = 超限。滑动窗口计数。"""
    now = time.time()
    hits = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW]
    over = len(hits) >= RATE_PER_IP
    if not over:
        hits.append(now)
        _rate[ip] = hits
    return over


def running_count() -> int:
    return tasks.running_count()


def new_token() -> str:
    return uuid.uuid4().hex[:20]


def issue_task_token(tid: str, request: Optional[Request] = None) -> str:
    """创建任务后签发访问令牌。公共模式下读写该任务的产物都必须带令牌。"""
    tok = new_token()
    ip = client_ip(request) if request else ""
    with _lock:
        _task_tokens[tid] = {"token": tok, "ip": ip, "created": time.time()}
    # 过期令牌清理（48h）
    cutoff = time.time() - 172800
    stale = [k for k, v in _task_tokens.items() if v.get("created", 0) < cutoff]
    for k in stale:
        _task_tokens.pop(k, None)
    return tok


def check_task_access(tid: str, token: str, request: Request):
    """校验“这个 IP 能否读这个任务”。通过返回令牌记录，否则抛 403/404。"""
    with _lock:
        rec = _task_tokens.get(tid)
    if not rec:
        raise HTTPException(404, "任务不存在")
    if not PUBLIC_MODE:
        return rec  # 私有模式：本机自用不设防
    if rec["token"] and token == rec["token"]:
        return rec
    if rec.get("ip") and rec["ip"] == client_ip(request):
        return rec
    raise HTTPException(403, "无权访问该任务的结果")


# 凭据注入白名单：公共模式下请求可携带自己的 API 信息覆盖服务端配置
CRED_FIELDS = {"api_base", "api_key", "model", "temperature"}


def resolve_run_cfg(cred: Optional[dict], auth: dict | None) -> dict:
    """决定本次流水线用哪套 API 配置：
    - 付费模式且已激活 → 服务端官方配置（访客无需 Key）
    - 其余 → 用户自带凭据（BYOK）或服务器配置
    """
    base_cfg = load_config()
    if SITE_PAID_MODE and auth:
        return base_cfg  # 官方代答
    return merge_cred(base_cfg, cred)


def merge_cred(cfg: dict, cred: Optional[dict]) -> dict:
    """BYOK：把用户自带的 API 凭据合并进服务端配置（仅白名单字段）。"""
    if not cred:
        return cfg
    out = dict(cfg)
    for k in CRED_FIELDS:
        v = cred.get(k)
        if k == "temperature":
            if isinstance(v, (int, float)) and 0 <= float(v) <= 1:
                out[k] = float(v)
        elif isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def require_paid(request: Request) -> dict:
    """付费模式：校验授权票据。未激活返回 402（前端弹赞助/激活窗）。"""
    if not SITE_PAID_MODE:
        return {"kind": "open"}
    auth = _auth(request)
    if auth is None:
        raise HTTPException(
            402,
            "本站由服务器官方 API 代答，需赞助解锁：爱发电 ¥29.9/月，或使用作者发的激活码",
        )
    return auth


class ParseReq(BaseModel):
    url: str


class StartReq(BaseModel):
    url: str
    page: Optional[int] = None
    all_pages: bool = False
    style: Optional[str] = "general"
    cred: Optional[dict] = None  # 公共模式：用户自带 API 凭据（可选）


class BatchReq(BaseModel):
    items: list  # [{"bvid":..., "title":...}]
    style: Optional[str] = "general"
    cred: Optional[dict] = None


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
    vision: Optional[bool] = None


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
def start(req: StartReq, request: Request):
    if not req.url.strip():
        raise HTTPException(400, "请输入视频链接")
    auth = require_paid(request)
    if PUBLIC_MODE:
        ip = client_ip(request)
        if running_count() >= MAX_CONCURRENT:
            raise HTTPException(429, f"当前有 {running_count()} 个任务进行中（上限 {MAX_CONCURRENT}），请稍后再试")
        if rate_hit(ip):
            raise HTTPException(429, "启动过于频繁，请 10 分钟后再试")
        cfg = resolve_run_cfg(req.cred, auth)
        if not SITE_PAID_MODE and not (cfg.get("api_key") and cfg.get("model")):
            raise HTTPException(400, "本站未提供公共 API Key，请在「设置」里填入你自己的 API 地址与 Key（仅保存在你的浏览器本地）")
    else:
        cfg = load_config()
    tid = tasks.create_task()
    issue_task_token(tid, request)

    def job(progress_cb):
        return pipeline.run_pipeline(
            req.url,
            {"page": req.page, "all_pages": req.all_pages, "style": req.style or "general"},
            progress_cb,
            stop_check=lambda: tasks.is_stopped(tid),
            cfg_override=cfg,
        )

    tasks.run_in_background(tid, job)
    return {"task_id": tid}


@app.post("/api/start_batch")
def start_batch(req: BatchReq, request: Request):
    """批量总结合集/收藏夹视频（私有上限 30 支，公共模式 6 支）。"""
    auth = require_paid(request)
    cap = MAX_BATCH_PUBLIC if PUBLIC_MODE else 30
    items = [i for i in (req.items or []) if i.get("bvid")][:cap]
    if not items:
        raise HTTPException(400, "批量列表为空")
    if PUBLIC_MODE:
        ip = client_ip(request)
        if running_count() >= MAX_CONCURRENT:
            raise HTTPException(429, f"当前有 {running_count()} 个任务进行中（上限 {MAX_CONCURRENT}），请稍后再试")
        if rate_hit(ip):
            raise HTTPException(429, "启动过于频繁，请 10 分钟后再试")
        cfg = resolve_run_cfg(req.cred, auth)
        if not SITE_PAID_MODE and not (cfg.get("api_key") and cfg.get("model")):
            raise HTTPException(400, "本站未提供公共 API Key，请在「设置」里填入你自己的 API 地址与 Key（仅保存在你的浏览器本地）")
    else:
        cfg = load_config()
    tid = tasks.create_task()
    issue_task_token(tid, request)

    def job(progress_cb):
        return pipeline.run_batch_pipeline(
            items, req.style or "general", progress_cb,
            stop_check=lambda: tasks.is_stopped(tid),
            cfg_override=cfg,
        )

    tasks.run_in_background(tid, job)
    return {"task_id": tid, "count": len(items)}


@app.post("/api/start_file")
async def start_file(request: Request, file: UploadFile = File(...), style: str = Form("general"),
                     api_base: str = Form(""), api_key: str = Form(""), model: str = Form("")):
    """总结本地音视频文件（拖拽/选择上传，存临时目录，用完即删）。"""
    import os as _os
    import tempfile as _tf

    auth = require_paid(request)
    if PUBLIC_MODE:
        ip = client_ip(request)
        if running_count() >= MAX_CONCURRENT:
            raise HTTPException(429, f"当前有 {running_count()} 个任务进行中（上限 {MAX_CONCURRENT}），请稍后再试")
        if rate_hit(ip):
            raise HTTPException(429, "启动过于频繁，请 10 分钟后再试")
        cfg = resolve_run_cfg({"api_base": api_base, "api_key": api_key, "model": model}, auth)
        if not SITE_PAID_MODE and not (cfg.get("api_key") and cfg.get("model")):
            raise HTTPException(400, "本站未提供公共 API Key，请在「设置」里填入你自己的 API 地址与 Key")
    else:
        cfg = load_config()

    limit = UPLOAD_LIMIT if PUBLIC_MODE else 4 * 1024 * 1024 * 1024
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
                if size > limit:
                    raise HTTPException(400, f"文件超过 {limit // (1024*1024)}MB 上限")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "文件为空")
    finally:
        await file.close()

    title = (file.filename or "本地文件").rsplit(".", 1)[0]
    tid = tasks.create_task()
    issue_task_token(tid, request)

    def job(progress_cb):
        try:
            return pipeline.run_file_pipeline(dest, title, style, progress_cb,
                                              stop_check=lambda: tasks.is_stopped(tid),
                                              cfg_override=cfg)
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
def task_status(tid: str, request: Request, token: str = Query("")):
    if PUBLIC_MODE:
        check_task_access(tid, token, request)
    t = tasks.get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    # 公共模式下任务完成后结果只给持票人（文件名可当凭据换阅，不回传全文重复消耗）
    return t


@app.post("/api/stop/{tid}")
def stop_task(tid: str, request: Request, token: str = Query("")):
    if PUBLIC_MODE:
        check_task_access(tid, token, request)
    tasks.request_stop(tid)
    return {"ok": True}


def _check_file_access(fname: str, request: Request, token: str = ""):
    """公共模式：校验请求者对该笔记文件的访问权（按创建任务的 IP 或令牌）。"""
    if not PUBLIC_MODE:
        return
    with _lock:
        recs = list(_task_tokens.values())
    ip = client_ip(request)
    for r in recs:
        if token and r.get("token") == token:
            return
        if r.get("ip") and r.get("ip") == ip:
            return
    raise HTTPException(403, "请从发起总结的设备访问（或携带任务令牌）")


class AskReq(BaseModel):
    file: str
    question: str
    history: Optional[list] = None
    token: Optional[str] = None   # 公共模式：任务访问令牌
    cred: Optional[dict] = None


@app.post("/api/ask")
def ask(req: AskReq, request: Request):
    """基于已保存的视频上下文回答问题（引用时间点）。"""
    import json as _json

    from .summarize import LLMClient

    if PUBLIC_MODE:
        # 笔记文件名 <-> 任务令牌 的归属校验（同一 IP 或持令牌者可问）
        _check_file_access(req.file, request, req.token)
    require_paid(request)

    q = (req.question or "").strip()
    if not q:
        raise HTTPException(400, "请输入问题")

    base = _output_dir().resolve()
    ctx_path = (base / (req.file + ".ctx.json")).resolve()
    if ctx_path.parent != base or not ctx_path.exists():
        raise HTTPException(404, "未找到该视频的问答上下文")

    data = _json.loads(ctx_path.read_text(encoding="utf-8"))
    items = list(data.get("items") or [])

    # 期末资料并入问答上下文：kind=doc 的条目用 (来源,页码) 做定位
    docs = _load_docs(req.file)
    for b in docs.get("blocks", []):
        items.append({
            "start": 0,
            "end": 0,
            "text": f"[资料·{b.get('source','')} 第{b.get('page','?')}页] {b.get('text','')}",
        })
    if not items:
        raise HTTPException(404, "该笔记没有可用的问答上下文")

    # 简单相关性挑选：按问题字符与条目文本的重叠度排序，控制总长度
    from .bilibili import fmt_ts

    def _fmt(s):
        try:
            return fmt_ts(float(s))
        except (TypeError, ValueError):
            return "00:00"

    def score(text: str) -> int:
        return sum(text.count(q[i : i + 2]) for i in range(len(q) - 1))

    # 资料条目不参与 [start-end] 格式化，但仍参与相关性排序
    scored = sorted(items, key=lambda it: -score(it["text"]))
    budget, chosen, chosen_raw = 12000, [], []
    for it in scored:
        if budget <= 0:
            break
        if it["text"].startswith("[资料·"):
            piece = it["text"]
        else:
            piece = f"[{_fmt(it['start'])}-{_fmt(it['end'])}] {it['text']}"
        if len(piece) > budget:
            piece = piece[:budget]
        chosen.append(piece)
        budget -= len(piece) + 1

    chosen.sort()  # 按时间字符串顺序（mm:ss 字典序即可）
    context = "\n".join(chosen)

    cfg = resolve_run_cfg(req.cred, _auth(request))
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
    token: Optional[str] = None
    cred: Optional[dict] = None


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
    """桌面版导出：弹系统保存对话框直接写盘（绕开 WebView 的下载限制）。

    服务器/容器等无桌面环境返回 cancelled，前端自动回退浏览器下载。
    """
    if PUBLIC_MODE:
        return {"ok": False, "cancelled": True}
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
    token: Optional[str] = None
    cred: Optional[dict] = None


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
def rewrite(req: RewriteReq, request: Request):
    """把已保存的视频笔记改写成平台文案（小红书/公众号/微博/知乎）。"""
    from .summarize import LLMClient

    if PUBLIC_MODE:
        _check_file_access(req.file, request, req.token)
    require_paid(request)
    guide = REWRITE_GUIDES.get(req.target)
    if not guide:
        raise HTTPException(400, f"不支持的改写目标：{req.target}")

    base = _output_dir().resolve()
    fname = req.file if req.file.endswith(".md") else req.file + ".md"
    md_path = (base / fname).resolve()
    if md_path.parent != base or not md_path.exists():
        raise HTTPException(404, "未找到该笔记")
    summary = md_path.read_text(encoding="utf-8")[:8000]

    cfg = resolve_run_cfg(req.cred, _auth(request))
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
def export_srt(request: Request, name: str = Query(...), fmt: str = Query("srt"), token: str = Query("")):
    """导出该笔记的视频字幕（仅「B站字幕」来源的笔记有完整字幕）。"""
    if PUBLIC_MODE:
        _check_file_access(name, request, token)
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
def flashcards(req: CardsReq, request: Request):
    """从已保存的视频笔记生成 Anki 闪卡（Q&A 对）。"""
    from .summarize import LLMClient

    if PUBLIC_MODE:
        _check_file_access(req.file, request, req.token)
    require_paid(request)
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

    cfg = resolve_run_cfg(req.cred, _auth(request))
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
def history(request: Request):
    """私有模式：本机历史列表。公共模式：不暴露别人的笔记列表，返回空。"""
    if PUBLIC_MODE:
        return []
    out = []
    for f in sorted(_output_dir().glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        out.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


@app.get("/api/output")
def read_output(request: Request, name: str = Query(...), token: str = Query("")):
    if PUBLIC_MODE:
        _check_file_access(name, request, token)
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
def get_config(request: Request):
    cfg = load_config()
    if SITE_PAID_MODE:
        # 网站付费模式：服务器内置官方 API，前端不再要求 BYOK；激活状态由浏览器票据决定
        return {
            "public": True,
            "site_paid": True,
            "presets": PRESETS,
            "config": {"public": True, "site_paid": True, "vision": bool(cfg.get("vision"))},
            "afdian_url": "https://afdian.com/a/Orisyan",
            "plan_price": float(os.getenv("SEEGLOW_PLAN_PRICE", "29.9")),
        }
    cfg["api_key_masked"] = _mask_key(cfg.get("api_key", ""))
    cfg.pop("api_key", None)
    cfg.pop("sessdata", None)
    cfg["has_sessdata"] = bool(cfg.get("sessdata"))
    if PUBLIC_MODE:
        # 公共模式：不下发服务端配置，前端改用浏览器本地凭据（BYOK）
        return {
            "public": True,
            "presets": PRESETS,
            "config": {
                "public": True,
                "server_has_key": bool(load_config().get("api_key")),
                "vision": bool(cfg.get("vision")),
            },
        }
    return {"config": cfg, "presets": PRESETS}


@app.post("/api/config")
def update_config(req: ConfigReq):
    if PUBLIC_MODE:
        raise HTTPException(403, "公共模式：API 配置仅保存在你自己设备的浏览器里，无法修改服务器配置")
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


# ---------------- 网站版激活（爱发电订单 / 作者激活码） ----------------

class SiteOrderReq(BaseModel):
    order_no: str
    device: str = ""   # 兼容保留；票据已不绑设备


class SiteCodeReq(BaseModel):
    code: str
    device: str = ""


@app.post("/api/pro/order")
def site_order(req: SiteOrderReq):
    """爱发电订单号在线验证 → 签发浏览器票据。"""
    if not SITE_PAID_MODE:
        raise HTTPException(404, "未启用付费模式")
    from . import pro_web

    if not req.order_no.strip():
        raise HTTPException(400, "请输入赞助订单号")
    # 简单防爆破：全局每分钟最多 10 次订单验证
    now = time.time()
    hits = [t for t in getattr(site_order, "_hits", []) if now - t < 60]
    if len(hits) >= 10:
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    site_order._hits = hits + [now]

    o = pro_web.query_order(req.order_no.strip())
    if o is None:
        return {"ok": False, "error": "未找到该订单。请确认订单号复制自「我的订单」，且已在 afdian.com/a/Orisyan 赞助；若网络异常可改用激活码"}
    amount = pro_web.order_amount_yuan(o)
    if amount < pro_web.PLAN_PRICE:
        return {"ok": False, "error": f"该订单金额（¥{amount:.2f}）未达到 ¥{pro_web.PLAN_PRICE:g}/月 档位门槛"}
    months = max(int(o.get("month") or 1), 1)
    expiry = (datetime.now() + timedelta(days=pro_web.PLAN_DAYS * months)).strftime("%Y-%m-%d")
    uid = req.order_no.strip()
    ticket = pro_web.make_ticket(uid, expiry, "afdian")
    return {"ok": True, **ticket}


@app.post("/api/pro/code")
def site_code(req: SiteCodeReq):
    """作者签发的激活码激活（无需爱发电赞助）。不绑定设备：一码多设备通用。"""
    if not SITE_PAID_MODE:
        raise HTTPException(404, "未启用付费模式")
    from . import pro_web

    # 限频防穷举：全局每分钟最多 N 次激活码校验
    now = time.time()
    hits = [t for t in getattr(site_code, "_hits", []) if now - t < 60]
    if len(hits) >= pro_web._CODE_ACTIVATE_RATE:
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    site_code._hits = hits + [now]

    try:
        expiry, serial = pro_web.parse_web_code(req.code)
    except HTTPException as e:
        return {"ok": False, "error": e.detail}
    if pro_web.is_code_revoked(serial):
        return {"ok": False, "error": "该激活码已作废，请联系作者"}
    # 同一张码在多台设备各自持票；票据不绑设备、可随意复制保存
    ticket = pro_web.make_ticket("CODE-" + serial.upper(), expiry, "code")
    return {"ok": True, **ticket}


@app.get("/api/pro/status")
def site_status(request: Request):
    """前端启动时探测：是否付费模式 + 本机票据是否有效。"""
    if not SITE_PAID_MODE:
        return {"site_paid": False}
    from . import pro_web

    auth = pro_web.check_request_auth(request)
    return {"site_paid": True, "licensed": bool(auth), "expiry": (auth or {}).get("expiry", "")}


# ---------------- 期末冲刺：教师资料上传 + 备考融合 ----------------

DOC_MAX_BYTES = 30 * 1024 * 1024        # 单文件上限
DOC_MAX_COUNT = 5                        # 每份笔记最多关联文件数


def _docs_path(fname: str) -> Path:
    return _output_dir() / (fname + ".docs.json")


def _load_docs(fname: str) -> dict:
    p = _docs_path(fname)
    if not p.exists():
        return {"files": [], "blocks": []}
    try:
        return _json.loads(p.read_text(encoding="utf-8")) or {"files": [], "blocks": []}
    except Exception:
        return {"files": [], "blocks": []}


def _save_docs(fname: str, data: dict):
    _output_dir().mkdir(parents=True, exist_ok=True)
    _docs_path(fname).write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _split_roles(blocks: list) -> tuple[list, list]:
    """blocks 按 role 分成 (内容资料, 标准模板卷)。旧数据无 role 字段视为 content。"""
    content = [b for b in blocks if b.get("role", "content") == "content"]
    template = [b for b in blocks if b.get("role") == "template"]
    return content, template


class StudyPackReq(BaseModel):
    file: str            # 笔记文件名（.md）
    mode: str = "fuse"   # fuse=备考融合包 / exam=押题卷
    count: Optional[int] = 8
    token: Optional[str] = None
    cred: Optional[dict] = None


@app.post("/api/upload_doc")
async def upload_doc(request: Request, file: UploadFile = File(...),
                     note_file: str = Form(...), role: str = Form("content"),
                     api_base: str = Form(""), api_key: str = Form(""), model: str = Form("")):
    """上传教师 PPT/往年卷并解析，追加到笔记的 .docs.json。返回提取概要。

    role: content=综合题卷/资料（题目内容来源） / template=标准模板卷（题型结构参照）
    """
    if PUBLIC_MODE:
        _check_file_access(note_file, request, request.headers.get("x-seeglow-auth", ""))
    require_paid(request)
    if role not in ("content", "template"):
        role = "content"

    suffix = "." + (file.filename or "x").rsplit(".", 1)[-1].lower()
    from .studydoc import allowed_ext

    if not allowed_ext(suffix):
        raise HTTPException(400, f"暂不支持 {suffix} 资料（支持 pptx / docx / pdf / txt / md / 图片）")

    import os as _os
    import tempfile as _tf

    dest = Path(_tf.gettempdir()) / f"seeglow_doc_{int(time.time())}_{_os.getpid()}{suffix}"
    try:
        size = 0
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > DOC_MAX_BYTES:
                    raise HTTPException(400, f"文件超过 {DOC_MAX_BYTES // (1024*1024)}MB 上限")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "文件为空")

        try:
            data = _load_docs(note_file)
            existing_names = [x["name"] for x in data.get("files", [])]
            fname_disp = (file.filename or "资料").rsplit(".", 1)[0]
            if fname_disp in existing_names:
                raise HTTPException(400, "该文件已上传过本笔记（同名）。如需替换请先删除对应记录或改名重传")
            data["files"] = (data.get("files", []) + [{"name": fname_disp, "ext": suffix.lstrip("."), "role": role}])[:DOC_MAX_COUNT]

            # 扫描件转写走视觉模型（与画面理解同链路）
            cfg0 = load_config()
            notice_msgs = []

            def vision_transcribe(jobs, src):
                notice_msgs.append(f"AI 转写 {len(jobs)} 页扫描内容…")
                from .summarize import LLMClient, SYSTEM_PROMPT

                ccfg = resolve_run_cfg({"api_base": api_base, "api_key": api_key,
                                        "model": model}, _auth(request))
                client = LLMClient(ccfg.get("api_base"), ccfg.get("api_key"),
                                   ccfg.get("model"), ccfg.get("temperature", 0.3))
                outs = []
                for page_no, png_bytes in jobs:
                    import base64 as b64

                    b64s = b64.b64encode(png_bytes).decode()
                    msg = {"role": "user", "content": [
                        {"type": "text", "text":
                            "这是试卷/PPT的一页截图。请把页面上所有文字**逐字转写**为纯文本，"
                            "保留题号、选项、公式（用文本近似），不要添加任何解释或评论。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64s}"}},
                    ]}
                    outs.append(client.chat([{"role": "system", "content": "你是精确的文字转录员，只输出原文。"},
                                             msg]))
                return outs

            from .studydoc import load_document

            try:
                blocks = load_document(dest, vision_transcribe=vision_transcribe,
                                       notice=lambda m: notice_msgs.append(m))
            except ValueError as e:
                raise HTTPException(400, str(e))
            except Exception as e:
                raise HTTPException(400, f"解析失败：文件可能损坏或格式异常（{str(e)[:80]}）")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"解析失败：{str(e)[:120]}")

        src_name = fname_disp
        for b in blocks:
            b["source"] = src_name
            b["role"] = role

        # 同名文件重复上传时整体替换其 blocks
        kept_blocks = [b for b in data.get("blocks", []) if b.get("source") != src_name]
        data["blocks"] = (kept_blocks + blocks)[:12000]
        _save_docs(note_file, data)
        kinds = {}
        for b in blocks:
            kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
        return {
            "ok": True,
            "file_count": len(data["files"]),
            "block_count": len(blocks),
            "kinds": kinds,
            "chars": sum(len(b["text"]) for b in blocks),
            "notices": notice_msgs[:6],
        }
    finally:
        try:
            dest.unlink()
        except OSError:
            pass


@app.post("/api/study_pack")
def study_pack(req: StudyPackReq, request: Request):
    """备考融合包 / 押题卷：视频笔记 × 教师资料 → 结构化 Markdown。"""
    from .summarize import LLMClient

    if PUBLIC_MODE:
        _check_file_access(req.file, request, req.token)
    require_paid(request)
    base = _output_dir().resolve()
    md_path = (base / req.file).resolve()
    if md_path.parent != base or not md_path.exists():
        raise HTTPException(404, "未找到该笔记，请先总结视频")
    docs = _load_docs(req.file)
    blocks = docs.get("blocks") or []
    content_blocks, template_blocks = _split_roles(blocks)
    if not content_blocks and not template_blocks:
        raise HTTPException(400, "该笔记还没有上传教师资料：先点「期末冲刺」上传 PPT 或往年卷")

    summary = md_path.read_text(encoding="utf-8")[:7000]

    from .bilibili import fmt_ts
    ctx_path = base / (req.file + ".ctx.json")
    timeline = ""
    if ctx_path.exists():
        try:
            items = (_json.loads(ctx_path.read_text(encoding="utf-8")) or {}).get("items") or []
            timeline = "\n".join(
                f"[{fmt_ts(float(it.get('start', 0)))}-{fmt_ts(float(it.get('end', it.get('start', 0))))}] "
                f"{str(it.get('text',''))[:180]}" for it in items[:80])[:9000]
        except Exception:
            pass

    from .studydoc import render_blocks

    doc_context = render_blocks(content_blocks, budget=(14000 if req.mode == "fuse" else 11000))
    template_context = render_blocks(template_blocks, budget=8000)

    cfg = resolve_run_cfg(req.cred, _auth(request))
    client = LLMClient(cfg.get("api_base"), cfg.get("api_key"), cfg.get("model"),
                       cfg.get("temperature", 0.3))

    meta = f"【视频】《{req.file.replace('.md','')}》"
    if req.mode == "exam":
        n = max(3, min(req.count or 8, 15))
        structure = EXAM_STRUCTURE_TPL if template_blocks else EXAM_STRUCTURE_NO_TPL
        user_prompt = (
            f"{meta}\n\n【视频时间轴摘要】\n{timeline or '（无）'}\n\n"
            + EXAM_PROMPT.format(
                count=n,
                content=doc_context or "（未上传综合题卷）",
                template=template_context or "（未上传标准模板卷）",
                structure_rule=structure,
            )
        )
        system = "你是经验丰富的命题老师，出的模拟题忠于原素材、难度贴近往年卷。直接输出 Markdown 正文。"
    else:
        user_prompt = (
            f"{meta}\n\n【视频总结笔记】\n{summary}\n\n"
            f"【视频时间轴摘要】\n{timeline or '（无）'}\n\n"
            f"【教师提供的 PPT/往年卷资料】\n{doc_context}\n\n{FUSE_PROMPT}"
        )
        system = ("你是严谨的考研辅导老师，擅长把课堂视频与教师讲义做考点比对。"
                  "只依据提供的材料输出，来源标注必须准确，绝不编造出处。直接输出 Markdown 正文。")

    try:
        text = client.chat([{"role": "system", "content": system},
                            {"role": "user", "content": user_prompt}])
    except Exception as e:
        raise HTTPException(500, f"生成失败：{str(e)[:150]}")
    out_name = req.file.replace(".md", "") + ("-押题卷.md" if req.mode == "exam" else "-备考包.md")
    try:
        (base / out_name).write_text(f"# {out_name.replace('.md','')}\n\n{text}\n", encoding="utf-8")
    except OSError:
        pass
    return {"ok": True, "mode": req.mode, "text": text, "output_file": out_name}


@app.post("/api/study_pack_standalone")
async def study_pack_standalone(request: Request, files: list[UploadFile] = File(...),
                                template_files: list[UploadFile] = File(default=[]),
                                mode: str = Form("outline"), count: int = Form(8),
                                api_base: str = Form(""), api_key: str = Form(""),
                                model: str = Form("")):
    """独立期末冲刺：不依赖视频笔记，直接对教师资料出知识梳理或押题卷。

    files=综合题卷/资料（内容来源）；template_files=标准模板卷（可选，仅作题型结构参照）。
    产物保存为一份笔记（含 .docs.json 与最小 ctx.json），可进历史、可继续 AI 问答。
    """
    import datetime as _dt
    import tempfile as _tf
    import os as _os

    require_paid(request)
    files = [f for f in files if f and (f.filename or "").strip()]
    template_files = [f for f in (template_files or []) if f and (f.filename or "").strip()]
    if not files and not template_files:
        raise HTTPException(400, "请至少上传一个资料文件")
    if len(files) + len(template_files) > DOC_MAX_COUNT:
        raise HTTPException(400, f"一次最多 {DOC_MAX_COUNT} 个文件")
    if mode not in ("outline", "exam", "fuse"):
        mode = "outline"

    from .studydoc import allowed_ext, load_document, render_blocks
    from .bilibili import safe_filename

    notice_msgs = []

    def vision_transcribe(jobs, src):
        notice_msgs.append(f"AI 转写 {len(jobs)} 页扫描内容…")
        from .summarize import LLMClient

        ccfg = resolve_run_cfg({"api_base": api_base, "api_key": api_key, "model": model},
                               _auth(request))
        client = LLMClient(ccfg.get("api_base"), ccfg.get("api_key"), ccfg.get("model"),
                           ccfg.get("temperature", 0.3))
        import base64 as b64

        outs = []
        for page_no, png_bytes in jobs:
            b64s = b64.b64encode(png_bytes).decode()
            msg = {"role": "user", "content": [
                {"type": "text", "text": "这是试卷/PPT的一页截图。请把页面上所有文字**逐字转写**为纯文本，"
                                         "保留题号、选项、公式（用文本近似），不要添加任何解释或评论。"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64s}"}},
            ]}
            outs.append(client.chat([{"role": "system", "content": "你是精确的文字转录员，只输出原文。"},
                                     msg]))
        return outs

    all_blocks, names, all_meta = [], [], []
    for f, role in ([(f, "content") for f in files] + [(f, "template") for f in template_files]):
        suffix = "." + f.filename.rsplit(".", 1)[-1].lower()
        if not allowed_ext(suffix):
            raise HTTPException(400, f"暂不支持 {suffix}（支持 pptx / docx / pdf / txt / md / 图片）")
        disp = f.filename.rsplit(".", 1)[0]
        if role == "content":
            names.append(disp)
        all_meta.append({"name": disp, "role": role})
        dest = Path(_tf.gettempdir()) / f"seeglow_sd_{int(time.time()*1000)}_{_os.getpid()}{suffix}"
        try:
            size = 0
            with open(dest, "wb") as fo:
                while True:
                    chunk = await f.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > DOC_MAX_BYTES:
                        raise HTTPException(400, f"{f.filename} 超过 {DOC_MAX_BYTES // (1024*1024)}MB 上限")
                    fo.write(chunk)
            if size == 0:
                raise HTTPException(400, f"{f.filename} 为空")
            try:
                blocks = load_document(dest, vision_transcribe=vision_transcribe,
                                       notice=lambda m: notice_msgs.append(m))
            except ValueError as e:
                raise HTTPException(400, str(e))
            except Exception as e:
                raise HTTPException(400, f"{f.filename} 解析失败：文件可能损坏或格式异常（{str(e)[:80]}）")
            for b in blocks:
                b["source"] = disp
                b["role"] = role
            all_blocks.extend(blocks)
        finally:
            try:
                dest.unlink()
            except OSError:
                pass

    content_blocks, template_blocks = _split_roles(all_blocks)
    doc_context = render_blocks(content_blocks, budget=12000)
    template_context = render_blocks(template_blocks, budget=8000)
    cfg = resolve_run_cfg({"api_base": api_base, "api_key": api_key, "model": model}, _auth(request))

    from .summarize import LLMClient

    client = LLMClient(cfg.get("api_base"), cfg.get("api_key"), cfg.get("model"),
                       cfg.get("temperature", 0.3))

    if mode == "exam":
        n = max(3, min(count or 8, 15))
        structure = EXAM_STRUCTURE_TPL if template_blocks else EXAM_STRUCTURE_NO_TPL
        user_prompt = (
            "（未提供视频，仅依据教师资料出卷）\n\n"
            + EXAM_PROMPT.format(
                count=n,
                content=doc_context or "（未上传综合题卷）",
                template=template_context or "（未上传标准模板卷）",
                structure_rule=structure,
            )
        )
        kind_label, system = "押题卷", (
            "你是经验丰富的命题老师，出的模拟题忠于原素材、难度贴近往年卷。直接输出 Markdown 正文。")
    else:
        user_prompt = f"【教师提供的资料】\n{doc_context}\n\n{OUTLINE_PROMPT}"
        kind_label, system = "知识梳理", (
            "你是严谨的考研辅导老师，擅长从讲义与真题中提炼考点。只依据材料输出，"
            "来源标注必须准确，绝不编造出处。直接输出 Markdown 正文。")

    try:
        text = client.chat([{"role": "system", "content": system},
                            {"role": "user", "content": user_prompt}])
    except Exception as e:
        raise HTTPException(500, f"生成失败：{str(e)[:150]}")

    base = _output_dir()
    base.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().strftime("%Y%m%d")
    out_name = f"{today}-{kind_label}-{safe_filename('、'.join(names[:2]))}.md"
    (base / out_name).write_text(f"# {out_name.replace('.md', '')}\n\n{text}\n", encoding="utf-8")
    _save_docs(out_name, {"files": all_meta[:DOC_MAX_COUNT], "blocks": all_blocks[:12000]})
    (base / (out_name + ".ctx.json")).write_text(
        _json.dumps({"title": out_name.replace(".md", ""), "url": "", "items": []},
                    ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "mode": mode, "text": text, "output_file": out_name,
            "notices": notice_msgs[:6]}


FUSE_PROMPT = """基于「视频总结笔记 + 时间轴」和「教师提供的资料」，生成一份期末备考融合包，结构如下：

## 🎯 重点度调整说明
简述结合教师材料后，哪些考点的重要度需要上调/下调（各列 2~4 条）。

## ✅ 三方对照表
| 考点 | 视频讲到 | 教师PPT强调 | 往年卷考过 | 建议 |
每行一个考点，建议列给出"再看视频 [mm:ss]"或"自学补齐"。（若某类材料缺失，列内写"—"）

## ⚠️ 视频没讲但老师强调的盲区
逐条列出，附 PPT 页码来源；这些是学生自学清单。

## 📌 划重点清单
10 条以内冲刺要点：考点一句话结论 + 出处（[mm:ss] 或 PPT 页码）。

要求：
- 所有标注必须能在给定材料中找到依据，找不到就注明（未定位）
- 表格用标准 Markdown 管道语法"""

OUTLINE_PROMPT = """仅依据「教师提供的资料」（PPT/讲义/往年卷），生成一份结构化知识梳理笔记：

## 📚 资料概览
用 3~5 句话概括这份材料覆盖的主题范围与资料构成。

## 🗂 知识梳理（按资料原有章节/页码顺序组织）
每个知识点：
- ★重要度（★★★必考 / ★★常考 / ★了解，依据往年卷出题情况与老师强调程度判断）
- 一句话讲透定义/公式/结论；术语加粗；标注来源（PPT第X页 / 试卷年份第X题）

## ❓ 往年卷透露的考法
分析资料中试卷的题型分布（选择/计算/论述各多少分），指出每类题型对应的知识模块。

## 🔁 背诵清单
最后给出 10 条以内"考前一晚背这些"的一句话要点。

要求：只依据材料本身，不要脑补材料外的内容；材料自相矛盾处如实指出。"""

EXAM_PROMPT = """请依据下面提供的材料出一套 **{count} 题**的全新模拟卷。

【综合题卷/资料】（题目内容与难度的依据）
{content}

【标准模板卷】（试卷结构的硬性模板）
{template}

## 📄 模拟卷说明
用 2~3 句话说明：题目内容借鉴了综合题卷哪些特征；结构上如何对齐标准模板卷
（无模板卷时，说明如何沿用综合题卷自身的题型分布）。

## 试卷正文
{structure_rule}
题目考查的具体知识内容与难度依据【综合题卷/资料】——考什么它说了算；
考点可以替换组合，但不得照抄原题题干。
每题后紧跟引用格式的答案与解析：

> **答案：** ……
> **解析：** 该考点来自综合题卷第X题 / PPT 第X页 / 视频 [mm:ss]；解题关键步骤……

## 🧭 复习指向
题目→考点→材料位置的对照列表（方便查漏）。

要求：不超纲——只使用材料覆盖的知识点；每道题都须有可核查的出处标注。"""

EXAM_STRUCTURE_TPL = ("⚠️ 试卷结构硬性对齐标准模板卷：题型种类、出现顺序、每种题型的题目数量与分值"
                      "必须与模板卷完全一致（如模板卷为『一、选择10题×3分；二、填空5题×4分；"
                      "三、简答4题×8分』，新卷逐项对齐，一道不多一道不少）。"
                      "标准模板卷中出现而综合题卷没有的题型，也必须按模板卷出题（内容从资料/视频知识点中取）。")

EXAM_STRUCTURE_NO_TPL = ("无标准模板卷：题型种类、数量与分值分布模仿【综合题卷/资料】中试卷自身的结构。")


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
    if PUBLIC_MODE and host in ("127.0.0.1", "localhost"):
        host = "0.0.0.0"  # 公共模式必须监听外网卡
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
