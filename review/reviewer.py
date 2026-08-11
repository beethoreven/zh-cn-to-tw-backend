"""
Stage 2 校對者：把文字依字數分批送審，彙整成結構化建議清單。

跟 Stage 1 的提交者一樣的資安/穩健性原則：
- 分批處理，避免單次輸出過長被截斷（同一份輸入重打也還是會截斷，
  截斷就直接放棄這批，不做無意義重試）
- 任何一批不管在哪個環節出錯，都只影響那一批（那批沒有校對結果），
  不會讓整個校對任務失敗、拿不到任何結果
"""

from __future__ import annotations

import json
import time

from configs import config
from llm_utils import claude_client, gemini_client
from llm_utils.errors import OutputTruncatedError, QuotaExceededError, TransientAPIError
from review import review_manager


def _chunk_text(text: str, max_chars: int) -> list[tuple[str, int, int]]:
    """依段落（\\n\\n 分隔）切批，回傳 (chunk文字, 起始offset, 結束offset)。"""
    if not text:
        return [("", 0, 0)]

    paragraphs = text.split("\n\n")
    chunks = []
    cursor = 0
    current_len = 0
    batch_start = 0

    for i, para in enumerate(paragraphs):
        para_start = cursor
        para_end = para_start + len(para)
        is_last = i == len(paragraphs) - 1
        sep_len = 0 if is_last else len("\n\n")

        if current_len > 0 and current_len + len(para) > max_chars:
            chunks.append((text[batch_start:cursor], batch_start, cursor))
            batch_start = para_start
            current_len = 0

        current_len += len(para) + sep_len
        cursor = para_end + sep_len

    chunks.append((text[batch_start:len(text)], batch_start, len(text)))
    return chunks


def _parse_findings(raw: str) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("回傳內容不是 JSON 陣列")

    required_keys = {"original", "suggested", "context", "reason"}
    findings = []
    for item in data:
        if not isinstance(item, dict) or not required_keys.issubset(item.keys()):
            continue
        findings.append(
            {
                "original": str(item["original"]),
                "suggested": str(item["suggested"]),
                "context": str(item["context"]),
                "reason": str(item["reason"]),
            }
        )
    return findings


def _call_review_with_retry(
    review_id: str,
    batch_no: int,
    chunk_text: str,
    model: str,
    max_retry: int,
    user_email,
    file_name: str | None,
    project: int | None,
) -> tuple[list[dict], bool]:
    """回傳 (這一批的校對建議, 這個 model 的額度是否已用盡)，額度用盡的
    旗標讓呼叫端可以決定要不要放棄剩下的批次，不用每批次都重新撞一次
    已經確定用盡的額度。"""
    review_fn = claude_client.review_text if model.startswith("claude") else gemini_client.review_text

    raw = None
    truncated = False
    quota_exceeded = False
    transient_error = False
    for attempt in range(1, max_retry + 1):
        try:
            review_manager.append_log(
                review_id, f"批次 {batch_no} 第 {attempt} 次呼叫 {model} 校對"
            )
            raw = review_fn(
                chunk_text, model=model, user_email=user_email, file_name=file_name, project=project
            )
            break
        except OutputTruncatedError as exc:
            review_manager.append_log(review_id, f"批次 {batch_no} {exc}", level="warning")
            truncated = True
            break
        except QuotaExceededError as exc:
            # RPM（每分鐘請求數）是每分鐘重置的滾動視窗，跟每日額度 RPD
            # 是兩回事，等一下再試很可能就會恢復，不像日額度真的用盡
            # 那樣重試永遠沒用——用遞增的等待時間重試，最後一次還是
            # 失敗才真的放棄這個批次
            quota_exceeded = True
            if attempt < max_retry:
                wait_seconds = config.QUOTA_RETRY_BASE_DELAY_SECONDS * attempt
                review_manager.append_log(
                    review_id,
                    f"批次 {batch_no} {exc}，等待 {wait_seconds:.0f} 秒後重試（{attempt}/{max_retry}）",
                    level="warning",
                )
                time.sleep(wait_seconds)
            else:
                review_manager.append_log(review_id, f"批次 {batch_no} {exc}", level="error")
        except TransientAPIError as exc:
            # 對方伺服器暫時性問題（不是額度用盡），通常幾秒到幾十秒內
            # 會恢復，用比 429 短的等待時間重試。
            transient_error = True
            if attempt < max_retry:
                wait_seconds = config.TRANSIENT_RETRY_BASE_DELAY_SECONDS * attempt
                review_manager.append_log(
                    review_id,
                    f"批次 {batch_no} {exc}，等待 {wait_seconds:.0f} 秒後重試（{attempt}/{max_retry}）",
                    level="warning",
                )
                time.sleep(wait_seconds)
            else:
                review_manager.append_log(review_id, f"批次 {batch_no} {exc}", level="error")
        except Exception as exc:  # noqa: BLE001
            quota_exceeded = False
            transient_error = False
            review_manager.append_log(
                review_id,
                f"批次 {batch_no} 第 {attempt} 次呼叫失敗：{exc}（{attempt}/{max_retry}）",
                level="warning",
            )

    if raw is None:
        if quota_exceeded:
            reason = "額度已用盡"
        elif transient_error:
            reason = "對方伺服器暫時無法處理"
        elif truncated:
            reason = "輸出被截斷"
        else:
            reason = f"重試 {max_retry} 次後仍呼叫失敗"
        review_manager.append_log(
            review_id,
            f"批次 {batch_no} {reason}，這批文字沒有校對結果"
            + ("，建議調小批次字數重新校對" if truncated else "")
            + ("，建議換一個 model 或等額度重置後再試" if quota_exceeded else "")
            + ("，建議稍後重新校對" if transient_error else ""),
            level="warning",
        )
        return [], quota_exceeded

    try:
        findings = _parse_findings(raw)
        review_manager.append_log(
            review_id, f"批次 {batch_no} 校對完成，找到 {len(findings)} 筆建議"
        )
        return findings, False
    except Exception as exc:  # noqa: BLE001
        review_manager.append_log(
            review_id,
            f"批次 {batch_no} 回傳內容解析失敗：{exc}，這批文字沒有校對結果",
            level="warning",
        )
        return [], False


def run_review(review_id: str, text: str, settings: dict | None = None):
    settings = settings or {}
    model = settings.get("model") or config.DEFAULT_REVIEW_MODEL
    batch_chars = settings.get("batch_chars", config.REVIEW_BATCH_CHARS)
    max_retry = settings.get("max_retry", config.REVIEW_MAX_RETRY)
    user_email = settings.get("user_email")
    file_name = settings.get("file_name")
    project = settings.get("project")
    # 上一輪（或更早幾輪）使用者明確不想套用的建議，這輪要濾掉，
    # 不要重複拿同樣被否決過的東西煩使用者
    exclude_fingerprints = {tuple(fp) for fp in settings.get("exclude_fingerprints", [])}

    review_manager.set_status(review_id, "running")

    try:
        if batch_chars == config.WHOLE_BOOK_SENTINEL:
            effective_max_chars = max(len(text), 1)
            review_manager.append_log(review_id, "批次模式：全文（整份文字當一批）")
        else:
            effective_max_chars = batch_chars

        chunks = _chunk_text(text, effective_max_chars)
        review_manager.set_chunk_offsets(review_id, [(s, e) for _, s, e in chunks])
        review_manager.append_log(
            review_id, f"校對文字共切成 {len(chunks)} 批（每批約 {effective_max_chars} 字，model={model}）"
        )

        all_findings = []
        # 這個 model 的額度一旦確定用盡，後面的批次不可能突然又有額度，
        # 繼續一批一批重試只是浪費時間——確定用盡後直接跳過剩下批次的
        # LLM 呼叫，把已經校對出來的結果盡快回給使用者。
        stop_calling_llm = False
        for batch_no, (chunk_text, _start, _end) in enumerate(chunks, start=1):
            review_manager.append_log(review_id, f"處理批次 {batch_no}/{len(chunks)}")

            if stop_calling_llm:
                review_manager.append_log(
                    review_id, f"批次 {batch_no} 額度已用盡，跳過這批的校對"
                )
                continue

            try:
                findings, quota_exhausted = _call_review_with_retry(
                    review_id, batch_no, chunk_text, model, max_retry, user_email, file_name, project
                )
                if quota_exhausted:
                    stop_calling_llm = True
                    review_manager.append_log(
                        review_id,
                        f"批次 {batch_no} 額度已用盡，重試 {max_retry} 次仍失敗，"
                        "後續批次不再校對",
                        level="warning",
                    )
            except Exception as exc:  # noqa: BLE001
                # 保底：這一批不管出了什麼非預期狀況，都只讓這批沒有結果，
                # 不能讓整個校對任務失敗、什麼建議都拿不到
                review_manager.append_log(
                    review_id, f"批次 {batch_no} 發生未預期錯誤：{exc}", level="error"
                )
                findings = []

            for finding in findings:
                finding["batch"] = batch_no
            all_findings.extend(findings)

            # 每批完成就存目前為止的建議（會一路寫進 Neon），伺服器中途
            # 重啟時使用者才救得回已經花錢算出來的部分
            review_manager.set_partial_findings(review_id, list(all_findings))

            # 批次之間固定停一下，降低短時間內連續打太密集撞到 RPM 上限
            # 的機率；最後一批不用等（後面沒有下一次呼叫了），額度已確定
            # 用盡、後面都不再呼叫 LLM 的情況也不用等
            if batch_no < len(chunks) and not stop_calling_llm:
                time.sleep(config.BATCH_DELAY_SECONDS)

        if exclude_fingerprints:
            before_count = len(all_findings)
            all_findings = [
                f for f in all_findings if (f["original"], f["suggested"]) not in exclude_fingerprints
            ]
            skipped = before_count - len(all_findings)
            if skipped:
                review_manager.append_log(
                    review_id, f"濾掉 {skipped} 筆你之前已經明確拒絕過的建議"
                )

        review_manager.append_log(review_id, f"全部批次校對完成，共找到 {len(all_findings)} 筆建議")
        review_manager.set_findings(review_id, all_findings)

    except Exception as exc:  # noqa: BLE001
        review_manager.append_log(review_id, f"發生錯誤：{exc}", level="error")
        review_manager.set_error(review_id, str(exc))
