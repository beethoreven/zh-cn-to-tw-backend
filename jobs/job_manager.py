"""
簡單的記憶體內 job 狀態管理。

目前是本機單人使用，所以用 process 內的 dict 存放狀態即可，
不需要引入 Redis/資料庫。如果之後要多人共用或跨 process，
這裡是唯一需要換掉的地方。
"""

from __future__ import annotations

import threading
import time
import uuid

_lock = threading.Lock()
_jobs = {}


def create_job(original_filename: str | None = None, user_email: str | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "pending",  # pending | running | done | failed
            "logs": [],
            "result_text": None,
            "error": None,
            "original_filename": original_filename,
            "created_at": time.time(),
            "user_email": user_email,
        }
    return job_id


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def append_log(job_id: str, message: str, level: str = "info"):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["logs"].append(
            {"time": time.time(), "level": level, "message": message}
        )


def set_status(job_id: str, status: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = status


def set_result(job_id: str, text: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["result_text"] = text
        job["status"] = "done"


def set_error(job_id: str, error: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["error"] = error
        job["status"] = "failed"
