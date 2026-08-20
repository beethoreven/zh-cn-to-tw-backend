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


# 前置處理後把各批接回一份完整文字時用的分隔符。跟 _chunk_text 的切批
# 依據（段落以 \n\n 分隔）一致，且每一批本來就是切在段落邊界上。
_PIECE_SEPARATOR = "\n\n"

# 前置處理後的文字相對於原文的最低長度比例。這一步只做「合併換行」跟
# 「換標點」，長度頂多少掉幾個換行字元，正常會落在 95% 以上；掉到這個
# 比例以下代表 LLM 沒照指示做（自己摘要、節錄、或中途停掉），這種內容
# 一旦寫回 source_text 就是永久性的整段消失，寧可退回這批原文。
_MIN_PROCESSED_RATIO = 0.7


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


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _coerce_findings(data) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("findings 不是 JSON 陣列")

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


def _parse_findings(raw: str) -> list[dict]:
    return _coerce_findings(json.loads(_strip_code_fence(raw)))


def _rebuild_source(processed_pieces: list[str], chunks: list[tuple[str, int, int]]) -> tuple[str, list]:
    """把已處理的批次 + 還沒處理到的批次原文，接成一份完整文字與對應的
    chunk_offsets。

    「還沒處理到的用原文佔位」是關鍵：這支會在每一批做完後就被呼叫一次
    （不是只在全部跑完後呼叫），所以任何一個中途存檔的時間點，
    source_text 都必須是完整的一整份文件，不能只有做到一半的前半段——
    伺服器如果在這時重啟，使用者按「結束此階段工作」收尾拿到的就是這份
    文字，少了後半段等於內容永久消失。"""
    pieces = [piece.strip() for piece in processed_pieces]
    pieces += [chunk[0].strip() for chunk in chunks[len(processed_pieces):]]

    offsets = []
    cursor = 0
    for i, piece in enumerate(pieces):
        offsets.append((cursor, cursor + len(piece)))
        cursor += len(piece) + (len(_PIECE_SEPARATOR) if i < len(pieces) - 1 else 0)
    return _PIECE_SEPARATOR.join(pieces), offsets


def _parse_preprocess_response(raw: str, original: str) -> tuple[str, list[dict]]:
    """「進行前置處理」開啟時的回傳格式：{"processed_text", "findings"}。

    processed_text 會直接覆寫成這個 review 的 source_text，是後續套用/
    下載的唯一基準，所以這裡對它特別嚴格：空的、或明顯比原文短一大截
    （代表 LLM 自作主張摘要/節錄，prompt 有明講不可以但不保證它會聽）
    一律視為解析失敗，讓呼叫端退回這批原文——校對建議少一批只是少一批
    建議，內容被摘要掉卻是永久性的資料損失，兩者的嚴重度差很多。"""
    data = json.loads(_strip_code_fence(raw))
    if not isinstance(data, dict):
        raise ValueError("回傳內容不是 JSON 物件")

    processed = data.get("processed_text")
    if not isinstance(processed, str) or not processed.strip():
        raise ValueError("回傳內容缺少可用的 processed_text")

    processed = processed.strip()
    original_len = len(original.strip())
    if original_len and len(processed) < original_len * _MIN_PROCESSED_RATIO:
        raise ValueError(
            f"前置處理後的文字只剩原文的 {len(processed) / original_len:.0%}"
            f"（{original_len} 字 -> {len(processed)} 字），像是被摘要或截斷了，不予採用"
        )

    return processed, _coerce_findings(data.get("findings", []))


def _truncation_advice(effective_max_chars: int) -> str:
    """輸出被截斷時要給的建議。批次字數已經是最小檔（1000）還被截斷，代表
    不是使用者設定的問題，叫他調小沒有意義，改成請他回報。"""
    if effective_max_chars <= config.REVIEW_BATCH_CHARS:
        return "，這批竟然太大，請跟阿舍老師回報處理"
    return "，這批太大，建議調小批次字數"


def _call_review_with_retry(
    review_id: str,
    batch_no: int,
    chunk_text: str,
    model: str,
    max_retry: int,
    user_email,
    file_name: str | None,
    project: int | None,
    enable_preprocess: bool,
    effective_max_chars: int,
) -> tuple[str | None, list[dict], bool]:
    """回傳 (前置處理後的文字, 這一批的校對建議, 這個 model 的額度是否已用盡)。

    前置處理沒開、或這一批失敗時，第一個元素是 None，呼叫端要自己退回這批
    原文。額度用盡的旗標讓呼叫端可以決定要不要放棄剩下的批次，不用每批次
    都重新撞一次已經確定用盡的額度。"""
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
                chunk_text,
                model=model,
                user_email=user_email,
                file_name=file_name,
                project=project,
                enable_preprocess=enable_preprocess,
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

    # 前置處理有開時，這批連「整理後的文字」也一起沒了，呼叫端會退回原文，
    # 訊息要講清楚，不然使用者只會看到「沒有校對結果」，不知道前置處理也
    # 一併沒生效
    lost = "這批文字沒有前置處理與校對結果" if enable_preprocess else "這批文字沒有校對結果"

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
            f"批次 {batch_no} {reason}，{lost}"
            + (_truncation_advice(effective_max_chars) if truncated else "")
            + ("，建議換一個 model 或等額度重置後再試" if quota_exceeded else "")
            + ("，建議稍後重新校對" if transient_error else ""),
            level="warning",
        )
        return None, [], quota_exceeded

    try:
        if enable_preprocess:
            processed_text, findings = _parse_preprocess_response(raw, chunk_text)
            review_manager.append_log(
                review_id, f"批次 {batch_no} 前置處理與校對完成，找到 {len(findings)} 筆建議"
            )
            return processed_text, findings, False

        findings = _parse_findings(raw)
        review_manager.append_log(
            review_id, f"批次 {batch_no} 校對完成，找到 {len(findings)} 筆建議"
        )
        return None, findings, False
    except Exception as exc:  # noqa: BLE001
        review_manager.append_log(
            review_id,
            f"批次 {batch_no} 回傳內容解析失敗：{exc}，{lost}",
            level="warning",
        )
        return None, [], False


def run_review(review_id: str, text: str, settings: dict | None = None):
    settings = settings or {}
    model = settings.get("model") or config.DEFAULT_REVIEW_MODEL
    batch_chars = settings.get("batch_chars", config.REVIEW_BATCH_CHARS)
    max_retry = settings.get("max_retry", config.REVIEW_MAX_RETRY)
    user_email = settings.get("user_email")
    file_name = settings.get("file_name")
    project = settings.get("project")
    # 「進行前置處理」：同一次 LLM 呼叫裡先把文字的斷句與標點整理過
    # （直接生效、不進使用者勾選清單），再對整理後的結果挑校對建議。
    # 預設 False；「重新校對（用套用後的文字）」一律強制 False，同一份
    # 文字不需要整理第二次（見 app.py 的 rerun_review）。
    enable_preprocess = settings.get("enable_preprocess", False)
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
        if enable_preprocess:
            review_manager.append_log(
                review_id, "已開啟前置處理：接合斷句、全形標點符號、整理上下引號（直接套用，不列入勾選清單）"
            )

        all_findings = []
        # 前置處理開啟時，每一批都要往這裡放一段文字（整理後的，或失敗時
        # 退回這批原文），一批都不能漏——最後這些會接起來覆寫成 review 的
        # source_text，是套用勾選與下載的唯一基準，漏一批等於整段內容消失
        processed_pieces: list[str] = []
        any_preprocessed = False
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
                processed_pieces.append(chunk_text)
                continue

            try:
                processed_text, findings, quota_exhausted = _call_review_with_retry(
                    review_id,
                    batch_no,
                    chunk_text,
                    model,
                    max_retry,
                    user_email,
                    file_name,
                    project,
                    enable_preprocess,
                    effective_max_chars,
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
                processed_text = None
                findings = []

            if processed_text is None:
                processed_pieces.append(chunk_text)
            else:
                processed_pieces.append(processed_text)
                any_preprocessed = True

            for finding in findings:
                finding["batch"] = batch_no
            all_findings.extend(findings)

            # 每批完成就存目前為止的建議（會一路寫進 Neon），伺服器中途
            # 重啟時使用者才救得回已經花錢算出來的部分
            review_manager.set_partial_findings(review_id, list(all_findings))

            # source_text 也要跟著每一批一起更新，不能等全部跑完才寫一次：
            # 已經存起來的 partial_findings 裡的 original 引用的是「整理後」
            # 的文字，如果伺服器在這時重啟，finalize_with_partial() 會把這些
            # 建議收尾成正式結果，但 source_text 還停在整理前的原文，
            # apply_selected 的字串比對就會全部落空——使用者勾了、按了套用，
            # 卻什麼都沒變，而且完全不會報錯。
            if enable_preprocess and any_preprocessed:
                review_manager.set_preprocessed_source(
                    review_id, *_rebuild_source(processed_pieces, chunks)
                )

            # 批次之間固定停一下，降低短時間內連續打太密集撞到 RPM 上限
            # 的機率；最後一批不用等（後面沒有下一次呼叫了），額度已確定
            # 用盡、後面都不再呼叫 LLM 的情況也不用等
            if batch_no < len(chunks) and not stop_calling_llm:
                time.sleep(config.BATCH_DELAY_SECONDS)

        # 收尾再寫一次完整版本。迴圈裡每批都寫過了，這裡多半是同樣的內容，
        # 但額度用盡而 continue 掉的批次不會經過迴圈裡那段，靠這裡補齊。
        # 覆寫 source_text 是前置處理成果進到最終文字的唯一管道——只產生
        # findings 而不覆寫的話，使用者下載到的會是沒整理過的版本。
        if enable_preprocess and any_preprocessed:
            review_manager.set_preprocessed_source(
                review_id, *_rebuild_source(processed_pieces, chunks)
            )
            review_manager.append_log(review_id, "前置處理結果已套用為校對基準文字")

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
