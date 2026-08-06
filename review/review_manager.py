"""
Stage 2 校對 job 的記憶體內狀態管理，架構上比照 jobs/job_manager.py。

除了 log/status 之外，這裡多存了 source_text 跟 chunk_offsets——
套用使用者勾選的修改時，需要知道每筆建議是從原文的哪一段切出來的，
才能只在那個範圍內做局部替換，避免同樣的詞在別處被誤改。
"""

from __future__ import annotations

import threading
import time
import uuid

_lock = threading.Lock()
_reviews = {}


def create_review(
    source_job_id: str, source_text: str, exclude_fingerprints: list | None = None
) -> str:
    """exclude_fingerprints：從上一輪（或更早幾輪）繼承下來、使用者已經
    明確不想套用的建議指紋（[original, suggested] 這種形式），這一輪
    校對時要濾掉，避免同樣被使用者否決過的建議一直重複出現。"""
    review_id = uuid.uuid4().hex[:12]
    with _lock:
        _reviews[review_id] = {
            "id": review_id,
            "source_job_id": source_job_id,
            "source_text": source_text,
            "status": "pending",  # pending | running | done | failed
            "logs": [],
            "findings": [],  # [{id, batch, original, suggested, context, reason}]
            "chunk_offsets": [],  # [(start, end), ...] 依 batch 順序
            "applied_text": None,  # 套用勾選建議後的文字，還沒套用過就是 None
            "exclude_fingerprints": exclude_fingerprints or [],
            "own_rejected_fingerprints": [],  # 這一輪套用時，使用者沒勾選的建議
            "error": None,
            "created_at": time.time(),
        }
    return review_id


def get_review(review_id: str) -> dict | None:
    with _lock:
        review = _reviews.get(review_id)
        return dict(review) if review else None


def append_log(review_id: str, message: str, level: str = "info"):
    with _lock:
        review = _reviews.get(review_id)
        if review is None:
            return
        review["logs"].append({"time": time.time(), "level": level, "message": message})


def set_status(review_id: str, status: str):
    with _lock:
        review = _reviews.get(review_id)
        if review is None:
            return
        review["status"] = status


def set_chunk_offsets(review_id: str, chunk_offsets: list[tuple[int, int]]):
    with _lock:
        review = _reviews.get(review_id)
        if review is None:
            return
        review["chunk_offsets"] = chunk_offsets


def set_findings(review_id: str, findings: list[dict]):
    with _lock:
        review = _reviews.get(review_id)
        if review is None:
            return
        for idx, finding in enumerate(findings):
            finding["id"] = idx
        review["findings"] = findings
        review["status"] = "done"


def set_error(review_id: str, error: str):
    with _lock:
        review = _reviews.get(review_id)
        if review is None:
            return
        review["error"] = error
        review["status"] = "failed"


def set_applied_text(review_id: str, text: str):
    with _lock:
        review = _reviews.get(review_id)
        if review is None:
            return
        review["applied_text"] = text


def record_rejected_findings(review_id: str, selected_ids: set[int]) -> None:
    """套用時記錄下使用者沒勾選的建議，供之後重新校對時排除用。"""
    with _lock:
        review = _reviews.get(review_id)
        if review is None:
            return
        rejected = [
            [f["original"], f["suggested"]]
            for f in review["findings"]
            if f["id"] not in selected_ids
        ]
        review["own_rejected_fingerprints"] = rejected


def get_combined_exclude_fingerprints(review_id: str) -> list:
    """回傳這個 review 累積到目前為止（含繼承自上一輪）全部被拒絕過的
    建議指紋，重新校對下一輪時要排除的就是這份清單。"""
    review = get_review(review_id)
    if review is None:
        return []
    return list(review["exclude_fingerprints"]) + list(review["own_rejected_fingerprints"])


def apply_selected(review_id: str, selected_ids: set[int]) -> str:
    """回傳套用勾選建議後的新文字，逐 batch 局部替換，找不到就跳過那筆。"""
    review = get_review(review_id)
    if review is None:
        raise ValueError("找不到這個 review")

    source_text = review["source_text"]
    chunk_offsets = review["chunk_offsets"]
    findings_by_batch: dict[int, list[dict]] = {}
    for finding in review["findings"]:
        if finding["id"] not in selected_ids:
            continue
        findings_by_batch.setdefault(finding["batch"], []).append(finding)

    pieces = []
    prev_end = 0
    for batch_no, (start, end) in enumerate(chunk_offsets, start=1):
        pieces.append(source_text[prev_end:start])  # 保留 batch 之間的分隔字元
        segment = source_text[start:end]
        for finding in findings_by_batch.get(batch_no, []):
            if finding["original"] in segment:
                segment = segment.replace(finding["original"], finding["suggested"], 1)
        pieces.append(segment)
        prev_end = end

    pieces.append(source_text[prev_end:])
    return "".join(pieces)
