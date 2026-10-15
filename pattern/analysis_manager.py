"""Stage 2 分析報告的狀態管理，架構比照 review/review_manager.py：記憶體是
讀取路徑，Neon（jobs 表，kind="pattern_analysis"）是重啟後的復原路徑。

刻意做成獨立的一筆紀錄、有自己的 status，而不是掛在套用結果那個 job
上的一個欄位：這樣伺服器重啟時，db_utils/job_store.mark_running_as_interrupted
會把還在跑的分析自動標成 interrupted，前端就知道要提示使用者重新分析，
不會永遠停在「分析中」（見 known-issue-check 第 14 條）。

寫入只會發生在同一個 process 的背景執行緒裡（建立後馬上開始跑），沒有
「做完之後隔很久才寫」的情況，所以不需要 review_manager 那種 _rehydrate
（見 known-issue-check 第 28 條）。
"""

from __future__ import annotations

import threading
import time
import uuid

from db_utils import job_store

_lock = threading.Lock()
_analyses: dict[str, dict] = {}

_KIND = "pattern_analysis"


def _persist(analysis_id: str, *, force: bool = True) -> None:
    with _lock:
        record = _analyses.get(analysis_id)
        snapshot = dict(record) if record else None
    if snapshot is None:
        return
    job_store.save(
        analysis_id, _KIND, snapshot["status"], snapshot.get("user_email"), snapshot, force=force
    )


def create_analysis(source_job_id: str, user_email: str | None, model: str) -> str:
    analysis_id = uuid.uuid4().hex[:12]
    with _lock:
        _analyses[analysis_id] = {
            "id": analysis_id,
            "source_job_id": source_job_id,
            "model": model,
            "status": "pending",  # pending | running | done | failed（重啟後 interrupted）
            "logs": [],
            # [{no, para_no, source, target, excerpt, issue, suggestion}]
            "items": [],
            "error": None,
            "created_at": time.time(),
            "user_email": user_email,
        }
    _persist(analysis_id)
    return analysis_id


def get_analysis(analysis_id: str) -> dict | None:
    with _lock:
        record = _analyses.get(analysis_id)
        if record is not None:
            return dict(record)
    return job_store.load(analysis_id)


def append_log(analysis_id: str, message: str, level: str = "info") -> None:
    with _lock:
        record = _analyses.get(analysis_id)
        if record is None:
            return
        record["logs"].append({"time": time.time(), "level": level, "message": message})
    _persist(analysis_id, force=False)


def set_status(analysis_id: str, status: str) -> None:
    with _lock:
        record = _analyses.get(analysis_id)
        if record is None:
            return
        record["status"] = status
    _persist(analysis_id)


def set_partial_items(analysis_id: str, items: list[dict]) -> None:
    """每批做完就存一次，伺服器中途重啟時已經付費算出來的提醒還在。"""
    with _lock:
        record = _analyses.get(analysis_id)
        if record is None:
            return
        record["items"] = list(items)
    _persist(analysis_id)


def set_result(analysis_id: str, items: list[dict]) -> None:
    with _lock:
        record = _analyses.get(analysis_id)
        if record is None:
            return
        record["items"] = list(items)
        record["status"] = "done"
    _persist(analysis_id)


def set_error(analysis_id: str, error: str) -> None:
    with _lock:
        record = _analyses.get(analysis_id)
        if record is None:
            return
        record["error"] = error
        record["status"] = "failed"
    _persist(analysis_id)
