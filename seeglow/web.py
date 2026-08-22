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


class StartReq(BaseModel):
    url: str


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


@app.post("/api/start")
def start(req: StartReq):
    if not req.url.strip():
        raise HTTPException(400, "请输入视频链接")
    tid = tasks.create_task()

    def job(progress_cb):
        return pipeline.run_pipeline(
            req.url,
            {},
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
