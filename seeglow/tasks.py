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
        # 顺手清理 6 小时前的旧任务记录，公共部署下防内存无限增长
        cutoff = time.time() - 6 * 3600
        stale = [k for k, v in _tasks.items() if v.get("created", 0) < cutoff]
        for k in stale:
            _tasks.pop(k, None)
    return tid


def running_count() -> int:
    """当前 running 状态的任务数（并发限额用）。"""
    with _lock:
        return sum(1 for t in _tasks.values() if t.get("status") == "running")


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
