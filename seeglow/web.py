"""本地 Web 服务：FastAPI + 单页前端。"""

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
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
    """解析链接，返回视频信息与分P列表（供前端选择）。"""
    from . import bilibili
    from .config import load_config

    try:
        bvid, _page = bilibili.parse_bvid(req.url)
        info = bilibili.get_video_info(bvid, load_config().get("sessdata", ""))
    except Exception as e:
        raise HTTPException(400, str(e))
    return {
        "bvid": info["bvid"],
        "title": info["title"],
        "owner": info["owner"],
        "duration": info["duration"],
        "pages": info["pages"] or [{"page": 1, "cid": info["cid_first"], "part": ""}],
    }


@app.post("/api/start")
def start(req: StartReq):
    if not req.url.strip():
        raise HTTPException(400, "请输入视频链接")
    tid = tasks.create_task()

    def job(progress_cb):
        return pipeline.run_pipeline(
            req.url,
            {"page": req.page, "all_pages": req.all_pages},
            progress_cb,
            stop_check=lambda: tasks.is_stopped(tid),
        )

    tasks.run_in_background(tid, job)
    return {"task_id": tid}


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
    return {"name": name, "content": path.read_text(encoding="utf-8")}


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
