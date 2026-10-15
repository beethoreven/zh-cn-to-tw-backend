"""Stage 2 分析報告：把範本規則造成的修改分批交給 LLM，挑出需要人看的。

只讀、只提醒：這裡完全不碰文字，套用早就由 pattern/engine.py 做完了。
穩健性原則跟 review/reviewer.py 一樣：任何一批出錯只影響那一批，不讓
整份分析失敗；額度確定用盡就不再呼叫，盡快把已經算出來的結果回給使用者。
"""

from __future__ import annotations

import json
import time

from configs import config
from llm_utils.errors import OutputTruncatedError, QuotaExceededError, TransientAPIError
from pattern import analysis_manager
from pattern.prompts import format_items

# 一次呼叫送幾筆。每筆約 200 字（規則＋三段截短的前後文），40 筆約 8000 字，
# 輸出只有「有問題的那幾筆」，不會撞到輸出上限。
BATCH_ITEMS = 40


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _parse(raw: str, batch: list[dict]) -> list[dict]:
    data = json.loads(_strip_code_fence(raw))
    if not isinstance(data, list):
        raise ValueError("回傳內容不是 JSON 陣列")
    by_no = {item["no"]: item for item in batch}
    results = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            no = int(entry.get("no"))
        except (TypeError, ValueError):
            continue
        item = by_no.get(no)
        issue = str(entry.get("issue") or "").strip()
        if item is None or not issue:
            # 編號對不上（LLM 自己編了一個不存在的項目）就丟掉，不要顯示
            # 一則找不到出處的提醒
            continue
        results.append(
            {
                "no": no,
                "para_no": item["para_no"],
                "source": item["source"],
                "target": item["target"],
                "excerpt": item["excerpt"],
                "issue": issue,
                "suggestion": str(entry.get("suggestion") or "").strip(),
            }
        )
    return results


def _call_with_retry(analysis_id, batch_no, prompt_items, model, max_retry, settings):
    from llm_utils import claude_client, gemini_client  # 理由同 review/reviewer.py

    fn = claude_client.analyze_pattern if model.startswith("claude") else gemini_client.analyze_pattern
    quota_exceeded = False
    for attempt in range(1, max_retry + 1):
        try:
            analysis_manager.append_log(analysis_id, f"批次 {batch_no} 第 {attempt} 次呼叫 {model}")
            return fn(
                prompt_items,
                model=model,
                user_email=settings.get("user_email"),
                file_name=settings.get("file_name"),
                project=settings.get("project"),
            ), False
        except OutputTruncatedError as exc:
            analysis_manager.append_log(analysis_id, f"批次 {batch_no} {exc}", level="warning")
            return None, False
        except QuotaExceededError as exc:
            quota_exceeded = True
            if attempt < max_retry:
                wait = config.QUOTA_RETRY_BASE_DELAY_SECONDS * attempt
                analysis_manager.append_log(
                    analysis_id, f"批次 {batch_no} {exc}，等待 {wait:.0f} 秒後重試", level="warning"
                )
                time.sleep(wait)
            else:
                analysis_manager.append_log(analysis_id, f"批次 {batch_no} {exc}", level="error")
        except TransientAPIError as exc:
            if attempt < max_retry:
                wait = config.TRANSIENT_RETRY_BASE_DELAY_SECONDS * attempt
                analysis_manager.append_log(
                    analysis_id, f"批次 {batch_no} {exc}，等待 {wait:.0f} 秒後重試", level="warning"
                )
                time.sleep(wait)
            else:
                analysis_manager.append_log(analysis_id, f"批次 {batch_no} {exc}", level="error")
        except Exception as exc:  # noqa: BLE001
            analysis_manager.append_log(
                analysis_id, f"批次 {batch_no} 第 {attempt} 次呼叫失敗：{exc}", level="warning"
            )
    return None, quota_exceeded


def run_analysis(analysis_id: str, items: list[dict], settings: dict) -> None:
    model = settings["model"]
    max_retry = settings.get("max_retry", config.REVIEW_MAX_RETRY)
    analysis_manager.set_status(analysis_id, "running")
    try:
        if not items:
            analysis_manager.append_log(analysis_id, "這次沒有範本規則造成的修改，不需要分析")
            analysis_manager.set_result(analysis_id, [])
            return

        batches = [items[i:i + BATCH_ITEMS] for i in range(0, len(items), BATCH_ITEMS)]
        analysis_manager.append_log(
            analysis_id, f"共 {len(items)} 筆字詞修改，分成 {len(batches)} 批分析（model={model}）"
        )
        results: list[dict] = []
        failed_batches = 0
        for batch_no, batch in enumerate(batches, start=1):
            raw, quota_exhausted = _call_with_retry(
                analysis_id, batch_no, format_items(batch), model, max_retry, settings
            )
            if raw is None:
                failed_batches += 1
                analysis_manager.append_log(
                    analysis_id, f"批次 {batch_no} 沒有分析結果", level="warning"
                )
                if quota_exhausted:
                    analysis_manager.append_log(
                        analysis_id, "額度已用盡，後續批次不再分析", level="warning"
                    )
                    failed_batches += len(batches) - batch_no
                    break
                continue
            try:
                found = _parse(raw, batch)
            except Exception as exc:  # noqa: BLE001
                failed_batches += 1
                analysis_manager.append_log(
                    analysis_id, f"批次 {batch_no} 回傳內容解析失敗：{exc}", level="warning"
                )
                continue
            results.extend(found)
            analysis_manager.set_partial_items(analysis_id, results)
            analysis_manager.append_log(analysis_id, f"批次 {batch_no} 完成，{len(found)} 筆需要注意")
            if batch_no < len(batches):
                time.sleep(config.BATCH_DELAY_SECONDS)

        results.sort(key=lambda r: (r["para_no"], r["no"]))
        summary = f"分析完成，共 {len(results)} 筆需要注意"
        if failed_batches:
            summary += f"（有 {failed_batches} 批沒有分析到，那些修改請自己看報告）"
        analysis_manager.append_log(analysis_id, summary, level="warning" if failed_batches else "info")
        analysis_manager.set_result(analysis_id, results)
    except Exception as exc:  # noqa: BLE001
        analysis_manager.append_log(analysis_id, f"發生錯誤：{exc}", level="error")
        analysis_manager.set_error(analysis_id, str(exc))
