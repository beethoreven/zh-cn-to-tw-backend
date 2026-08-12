"""
job 狀態管理：記憶體是主要讀取路徑，Neon 是重啟後的復原路徑。

前端每 5 秒輪詢一次進度，而 Neon 單次查詢要 1.2-1.6 秒，所以正常
情況一律讀記憶體；只有記憶體裡沒有（代表這是重啟後的新 process）才
回頭查資料庫。寫入則是每次狀態轉換、每批部分成果都同步寫進 Neon，
純 log 更新會被節流。完整設計說明見 db_utils/job_store.py。
"""

from __future__ import annotations

import threading
import time
import uuid

from db_utils import job_store

_lock = threading.Lock()
_jobs = {}

_KIND = "job"


def _persist(job_id: str, *, force: bool = True) -> None:
    with _lock:
        job = _jobs.get(job_id)
        snapshot = dict(job) if job else None
    if snapshot is None:
        return
    job_store.save(
        job_id, _KIND, snapshot["status"], snapshot.get("user_email"), snapshot, force=force
    )


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
            # 每批處理完就更新，讓工作中途被打斷時還撈得回已經花錢算出來
            # 的部分成果（見 set_partial_result）
            "partial_text": None,
        }
    _persist(job_id)
    return job_id


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            return dict(job)
    # 記憶體裡沒有：這個 process 可能是重啟後才起來的，回頭查資料庫。
    # 啟動時已經把未完成的工作標成 interrupted，所以這裡撈回來的狀態
    # 會讓前端知道要跳重試對話窗，而不是永遠停在「執行中」。
    return job_store.load(job_id)


def append_log(job_id: str, message: str, level: str = "info"):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["logs"].append(
            {"time": time.time(), "level": level, "message": message}
        )
    # 純 log 更新頻率很高（一個批次好幾次），交給 job_store 節流，
    # 不要每次都真的寫一次 Neon
    _persist(job_id, force=False)


def set_status(job_id: str, status: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = status
    _persist(job_id)


def set_partial_result(job_id: str, text: str):
    """每個批次處理完就呼叫一次。這是「工作被中斷時還能救回多少」的
    關鍵——不存的話，重啟後只知道這個 job 死了，但前面幾個批次已經
    付費算出來的結果全部拿不回來。"""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["partial_text"] = text
    _persist(job_id)


def set_result(job_id: str, text: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["result_text"] = text
        job["status"] = "done"
    _persist(job_id)


def set_error(job_id: str, error: str):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["error"] = error
        job["status"] = "failed"
    _persist(job_id)


def finalize_with_partial(job_id: str) -> bool:
    """使用者在中斷對話窗選「結束此階段工作」時呼叫：把目前已經完成的
    部分成果當成最終結果收尾，讓他至少能把做到一半的東西下載出來。
    回傳是否真的有東西可以收。"""
    job = get_job(job_id)
    if job is None:
        return False
    partial = job.get("partial_text") or job.get("result_text")
    if not partial:
        return False

    with _lock:
        if job_id not in _jobs:
            # 重啟後記憶體是空的，把從資料庫撈回來的狀態放回記憶體，
            # 後續輪詢/下載才走得下去
            _jobs[job_id] = job
        _jobs[job_id]["result_text"] = partial
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["logs"].append({
            "time": time.time(),
            "level": "warning",
            "message": "工作中途被伺服器重啟打斷，已改用中斷前完成的部分成果收尾",
        })
    _persist(job_id)
    return True
