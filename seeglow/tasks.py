"""后台任务管理：线程执行 + 状态查询 + 取消。"""

import threading
import time
import uuid

_lock = threading.Lock()
_tasks = {}
_stop_flags = set()


def create_task() -> str:
    tid = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[tid] = {
            "id": tid,
            "status": "running",
            "stage": "queue",
            "percent": 0,
            "message": "排队中…",
            "created": time.time(),
            "result": None,
            "error": None,
        }
    return tid


def update_task(tid: str, **kw):
    with _lock:
        t = _tasks.get(tid)
        if t:
            t.update(kw)


def get_task(tid: str):
    with _lock:
        return dict(_tasks.get(tid) or {}) or None


def request_stop(tid: str):
    with _lock:
        _stop_flags.add(tid)


def is_stopped(tid: str) -> bool:
    with _lock:
        return tid in _stop_flags


def run_in_background(tid: str, fn):
    def worker():
        try:
            result = fn(lambda d: update_task(tid, **d))
            update_task(tid, status="done", percent=100, result=result, message="完成")
        except Exception as e:
            msg = str(e)
            status = "stopped" if ("取消" in msg) else "error"
            update_task(tid, status=status, error=msg, message=msg)

    threading.Thread(target=worker, daemon=True).start()
