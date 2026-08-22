"""
提交者：串接（可選）封面偵測 -> OCR -> OpenCC 簡轉繁 -> LLM 潤飾（Gemini 或
Claude）-> OpenCC 校正 -> 驗證 -> 組裝。

每一步都透過 job_manager 寫 log，前端可以輪詢看到即時進度。

簡轉繁的正確性由 OpenCC 這個決定性規則引擎保證，不依賴 LLM：LLM
潤飾完之後，會再跑一次 OpenCC 做校正，確保萬一 LLM 不小心弄髒了文字
也會被自動修正，不需要為此重打一次 API。retry 只用在 API 呼叫
本身失敗（網路、暫時性錯誤）的情況，因為那才是重試真正能解決的問題；
輸出被截斷（MAX_TOKENS / max_tokens）是同一個輸入必然會重現的問題，重試沒有意義，
直接視為這個批次失敗、保留 OpenCC 結果並提示使用者調小批次頁數。

每個批次都有兩層防護，保證單一批次不管在哪個環節出錯，都不會讓整份
文件的輸出報銷：
1. 潤飾/校正階段（LLM 呼叫、OpenCC 校正、驗證）本身包一層，出錯就
   退回「只做過決定性簡轉繁、沒有潤飾」的版本。
2. 最外層再包一層保底，就算連最初的簡轉繁都出了非預期狀況，也會退回
   OCR 原始文字，而不是讓整個 job 直接失敗、什麼都下載不到。
"""

from __future__ import annotations

import itertools
import time

from configs import config
from convert_utils.opencc_convert import convert_to_traditional
from jobs import job_manager
from llm_utils import claude_client, gemini_client
from llm_utils.errors import OutputTruncatedError, QuotaExceededError, TransientAPIError
from llm_utils.prompts import resolve_refine_items
from output_utils.text_normalize import normalize_paragraph_breaks
from validators.simplified_check import find_residual_simplified


def _batches(pages: list[str], batch_size: int):
    for i in range(0, len(pages), batch_size):
        yield i, pages[i : i + batch_size]


def _resolve_batch_size(batch_pages_setting, total_pages: int) -> int:
    if batch_pages_setting == config.WHOLE_BOOK_SENTINEL:
        return max(total_pages, 1)
    return int(batch_pages_setting)


def _refine_and_correct(
    job_id: str,
    batch_no: int,
    traditional_text: str,
    model: str,
    max_retry: int,
    user_email,
    file_name: str | None,
    project: int | None,
    items: frozenset[int],
) -> tuple[str, bool]:
    """呼叫 LLM 潤飾並用 OpenCC 校正，回傳 (最終文字, 這個 model 的額度是否
    已用盡)；絕不拋出例外，任何失敗都退回 traditional_text（純 OpenCC
    結果）。額度用盡的旗標讓呼叫端可以決定要不要乾脆放棄剩下的批次，不用
    每批次都重新撞一次已經確定用盡的額度、白白浪費重試等待時間。"""
    refine_fn = claude_client.refine_text if model.startswith("claude") else gemini_client.refine_text

    refined = None
    truncated = False
    quota_exceeded = False
    transient_error = False
    for attempt in range(1, max_retry + 1):
        try:
            job_manager.append_log(
                job_id, f"批次 {batch_no} 第 {attempt} 次呼叫 {model} 處理"
            )
            refined = refine_fn(
                traditional_text,
                model=model,
                user_email=user_email,
                file_name=file_name,
                project=project,
                items=items,
            )
            break
        except OutputTruncatedError as exc:
            # 同一份輸入重打一次還是會被截斷，重試沒有意義，直接跳出
            job_manager.append_log(job_id, f"批次 {batch_no} {exc}", level="warning")
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
                job_manager.append_log(
                    job_id,
                    f"批次 {batch_no} {exc}，等待 {wait_seconds:.0f} 秒後重試（{attempt}/{max_retry}）",
                    level="warning",
                )
                time.sleep(wait_seconds)
            else:
                job_manager.append_log(job_id, f"批次 {batch_no} {exc}", level="error")
        except TransientAPIError as exc:
            # 對方伺服器暫時性問題（不是額度用盡），通常幾秒到幾十秒內
            # 會恢復，用比 429 短的等待時間重試。
            transient_error = True
            if attempt < max_retry:
                wait_seconds = config.TRANSIENT_RETRY_BASE_DELAY_SECONDS * attempt
                job_manager.append_log(
                    job_id,
                    f"批次 {batch_no} {exc}，等待 {wait_seconds:.0f} 秒後重試（{attempt}/{max_retry}）",
                    level="warning",
                )
                time.sleep(wait_seconds)
            else:
                job_manager.append_log(job_id, f"批次 {batch_no} {exc}", level="error")
        except Exception as exc:  # noqa: BLE001
            quota_exceeded = False
            transient_error = False
            job_manager.append_log(
                job_id,
                f"批次 {batch_no} 第 {attempt} 次呼叫失敗：{exc}（{attempt}/{max_retry}）",
                level="warning",
            )

    if refined is None:
        if quota_exceeded:
            reason = "額度已用盡"
        elif transient_error:
            reason = "對方伺服器暫時無法處理"
        elif truncated:
            reason = "輸出被截斷"
        else:
            reason = f"重試 {max_retry} 次後仍呼叫失敗"
        job_manager.append_log(
            job_id,
            f"批次 {batch_no} {reason}，保留 OpenCC 決定性轉換結果（未套用 LLM 處理）"
            + ("，建議調小批次頁數重新處理這本書" if truncated else "")
            + ("，建議換一個 model 或等額度重置後再處理這本書" if quota_exceeded else "")
            + ("，建議稍後重新處理這本書" if transient_error else ""),
            level="warning",
        )
        return traditional_text, quota_exceeded

    try:
        # LLM 的工作只是潤飾格式，不負責簡轉繁本身；不管它做得好不好，
        # 都用 OpenCC 再校正一次，確保絕對不會有殘留簡體字，這一步是
        # 本機決定性運算，不需要也不會再多打一次 API
        corrected = convert_to_traditional(refined)

        # 潤飾前後的長度比對。跟上面「不管 LLM 做得好不好都再跑一次
        # OpenCC」同一個思路：prompt 寫得再清楚都只是要求，這裡用決定性
        # 運算實際驗證它有沒有照做。比較基準兩邊都是 OpenCC 處理過的狀態
        # （traditional_text 是 LLM 前、corrected 是 LLM 後），長度可比。
        #
        # 分兩層是因為兩種情況的嚴重度差很多：整批被摘要/截斷掉是永久性
        # 的資料損失，寧可退回沒潤飾的版本；掉個幾 % 則可能是正常移除頁碼
        # 標頭的結果，不該因此丟掉已經付費算出來的潤飾，但要留下痕跡讓
        # 使用者自己判斷（job log 前端看得到）。
        #
        # 要講清楚這層檢查抓得到什麼：它抓的是災難性損失。一個批次掉個
        # 幾行可能只少 5%，低於門檻但不會被攔下來——那種情況真正的防線是
        # prompt 本身（見 llm_utils/prompts.py 第 2/5 條的說明），這裡是
        # 補「整段被吃掉」這個原本完全沒有防護的情況。
        before_len = len(traditional_text.strip())
        after_len = len(corrected.strip())
        if before_len:
            ratio = after_len / before_len
            if ratio < config.REFINE_MIN_OUTPUT_RATIO:
                job_manager.append_log(
                    job_id,
                    f"批次 {batch_no} 潤飾後只剩原文的 {ratio:.0%}"
                    f"（{before_len} 字 -> {after_len} 字），像是被摘要或截斷了，"
                    "不予採用，保留 OpenCC 決定性轉換結果（未套用 LLM 潤飾）",
                    level="error",
                )
                return traditional_text, False
            if ratio < config.REFINE_WARN_OUTPUT_RATIO:
                job_manager.append_log(
                    job_id,
                    f"批次 {batch_no} 潤飾後剩原文的 {ratio:.0%}"
                    f"（{before_len} 字 -> {after_len} 字），比預期少，"
                    "如果不是移除頁碼標頭造成的，建議人工檢查這個批次",
                    level="warning",
                )

        residual = find_residual_simplified(corrected)
        if residual:
            job_manager.append_log(
                job_id,
                f"批次 {batch_no} 經 OpenCC 校正後仍偵測到異常字元：{residual}，"
                f"建議人工檢查這個批次",
                level="warning",
            )
        else:
            job_manager.append_log(job_id, f"批次 {batch_no} 處理完成")
        return corrected, False
    except Exception as exc:  # noqa: BLE001
        job_manager.append_log(
            job_id,
            f"批次 {batch_no} 處理後的校正階段發生錯誤：{exc}，改用未處理的簡轉繁結果",
            level="warning",
        )
        return traditional_text, False


def run_ocr_stage(job_id: str, pdf_path: str, dpi: int, detect_cover: bool) -> tuple[list[str], int]:
    """封面偵測（可選）+ 逐頁 OCR，回傳 (每頁文字, 實際頁數)。桌面版 App
    會在使用者本機端做這一段（見 zh-cn-to-tw-ocr-service），伺服器端的
    正常上傳流程則由 run_pipeline 呼叫這裡，兩條路徑共用同一份邏輯。

    ocr_utils 這幾個 import 刻意放在函式裡面、不放模組最上面：它們會把
    PaddleOCR/PaddlePaddle/PyMuPDF 整包拉進來（光 paddle 就約 600MB）。
    桌面版 App 把這支 backend 一起打包進 .app 時，OCR 是由旁邊獨立的
    zh-cn-to-tw-ocr-service 負責、這個函式永遠不會被呼叫到，模組層級的
    import 會讓 PyInstaller 把這些完全用不到的東西也打包進去，白白多出
    數百 MB。放在函式內就只有真的走伺服器端上傳流程時才會載入。"""
    from ocr_utils.cover_detect import evaluate_cover_page
    from ocr_utils.ocr_engine import ocr_page
    from ocr_utils.pdf_to_images import get_pdf_page_count, render_pdf_pages

    job_manager.append_log(job_id, "開始將 PDF 轉成頁面圖片")
    # total_pages 用便宜的方式單獨查（不渲染任何一頁），page_images
    # 則是逐頁 yield 的產生器——不要把整份 PDF 的每一頁圖片同時留在
    # 記憶體裡，頁數多、DPI 高的劇本很容易讓記憶體用量衝到超過
    # Render 免費方案的上限，把整個 process OOM 砍掉（連帶讓所有還
    # 在進行中的請求，包括前端輪詢，一起失敗，且因為 job 狀態只存
    # 在記憶體裡，process 重啟後那個 job 就徹底消失，前端看到的會是
    # 「找不到這個 job」）。
    total_pages = get_pdf_page_count(pdf_path)
    page_images = render_pdf_pages(pdf_path, dpi=dpi)
    job_manager.append_log(job_id, f"PDF 共 {total_pages} 頁，開始逐頁轉圖並辨識")

    if not detect_cover:
        job_manager.append_log(job_id, "「偵測首頁是否為封面」已關閉，略過封面偵測")
    elif total_pages < 2:
        job_manager.append_log(job_id, f"PDF 只有 {total_pages} 頁，略過封面偵測")
    else:
        # 封面偵測需要先看過第 1、2 頁，只好先把這兩頁從產生器拉出來；
        # 不管判定結果如何、甚至中途出錯，lookahead 裡拉到的頁面最後
        # 都會用 itertools.chain 接回產生器最前面，不會漏掉任何一頁
        lookahead = []
        try:
            lookahead.append(next(page_images))
            lookahead.append(next(page_images))
            result = evaluate_cover_page(
                lookahead[0],
                lookahead[1],
                config.COVER_DETECT_DARK_RATIO_THRESHOLD,
                config.COVER_DETECT_SATURATION_THRESHOLD,
                config.COVER_DETECT_RELATIVE_MARGIN,
            )
            job_manager.append_log(
                job_id,
                f"封面偵測：首頁墨水覆蓋率={result.dark_ratio:.2f}、飽和度={result.saturation:.1f}"
                f"（門檻分別為 {config.COVER_DETECT_DARK_RATIO_THRESHOLD}、"
                f"{config.COVER_DETECT_SATURATION_THRESHOLD}）；"
                f"參考頁（第 2 頁）墨水覆蓋率={result.reference_dark_ratio:.2f}、"
                f"飽和度={result.reference_saturation:.1f}"
                f"（相對倍數門檻 {config.COVER_DETECT_RELATIVE_MARGIN}）"
                f" → 判定{'為封面' if result.is_cover else '不是封面'}",
            )
            if result.is_cover:
                lookahead = lookahead[1:]
                total_pages -= 1
                job_manager.append_log(
                    job_id,
                    "首頁已自動移除，不列入 OCR 與後續處理範圍；"
                    "如果判斷錯誤，請關閉「偵測首頁是否為封面」開關重新處理",
                )
        except Exception as exc:  # noqa: BLE001
            # 封面偵測本身只是加分項，出錯就當作沒偵測到，繼續正常處理
            # 全部頁面，不能因為這個非必要的判斷讓整個 job 失敗
            job_manager.append_log(
                job_id, f"封面偵測發生錯誤：{exc}，跳過偵測，正常處理全部頁面", level="warning"
            )
        page_images = itertools.chain(lookahead, page_images)

    raw_pages = []
    for idx, image in enumerate(page_images, start=1):
        job_manager.append_log(job_id, f"OCR 辨識第 {idx}/{total_pages} 頁")
        text = ocr_page(image)
        raw_pages.append(text)

    job_manager.append_log(job_id, "全部頁面 OCR 完成，開始分批處理")
    return raw_pages, total_pages


def run_refine_stage(job_id: str, raw_pages: list[str], total_pages: int, settings: dict | None = None):
    """OpenCC 簡轉繁 -> LLM 潤飾 -> OpenCC 校正 -> 驗證 -> 組裝，不含 OCR。
    桌面版 App 呼叫的新路徑（本機已經 OCR 完）跟一般上傳路徑（run_pipeline）
    都會走到這裡，確保兩條路徑的潤飾/校對規則完全一致，不會分岔維護。"""
    settings = settings or {}
    model = settings.get("model") or config.GEMINI_MODEL
    batch_pages_setting = settings.get("batch_pages", config.REFINE_BATCH_PAGES)
    max_retry = settings.get("max_retry", config.REFINE_MAX_RETRY)
    user_email = settings.get("user_email")
    file_name = settings.get("file_name")
    project = settings.get("project")
    # 兩個獨立開關，各自對應一組潤飾項目（見 llm_utils/prompts.py）：
    # - enable_preprocess：移除頁碼與標頭、接合斷句、全形標點與引號（項目 1/2/3）
    # - enable_llm_refine：修正 OCR 誤植錯字（項目 4/5），有改寫內容的風險
    # 兩個都關就完全不呼叫 LLM，只保留決定性的 OpenCC 簡轉繁。
    #
    # enable_llm_refine 預設 True 維持舊行為；enable_preprocess 預設「跟
    # enable_llm_refine 一樣」而不是寫死 True——舊版前端/桌面殼不會帶
    # enable_preprocess，如果寫死 True，那些明確關掉潤飾的舊請求會突然
    # 開始呼叫 LLM（使用者沒要求、還要付費），跟著 enable_llm_refine 走
    # 才是兩種舊情況都完全不變。
    enable_llm_refine = settings.get("enable_llm_refine", True)
    enable_preprocess = settings.get("enable_preprocess", enable_llm_refine)
    refine_items = resolve_refine_items(enable_preprocess, enable_llm_refine)

    batch_size = _resolve_batch_size(batch_pages_setting, total_pages)
    if batch_pages_setting == config.WHOLE_BOOK_SENTINEL:
        job_manager.append_log(job_id, f"批次模式：整本丟（{total_pages} 頁當一批）")
    if not refine_items:
        job_manager.append_log(job_id, "前置處理與 LLM 潤飾都已關閉，全程只做 OpenCC 簡轉繁")
    else:
        job_manager.append_log(
            job_id,
            "本次處理項目："
            + ("前置處理（移除頁碼與標頭、接合斷句、全形標點、整理引號）" if enable_preprocess else "")
            + ("＋" if enable_preprocess and enable_llm_refine else "")
            + ("LLM 潤飾（修正 OCR 誤植錯字）" if enable_llm_refine else ""),
        )

    refined_chunks = []
    batch_list = list(_batches(raw_pages, batch_size))
    total_batches = len(batch_list)
    # 這個 model 的額度一旦確定用盡（某一批重試 max_retry 次後仍是
    # QuotaExceededError），後面的批次不可能突然又有額度，繼續一批一批
    # 重試只是浪費時間（每批都要等完整的退避重試才會放棄）——確定用盡後
    # 直接跳過 LLM 呼叫，剩下的批次只做決定性的 OpenCC 簡轉繁，把已經
    # 做出來的成果盡快組起來給使用者，而不是繼續卡在注定失敗的重試上。
    stop_calling_llm = not refine_items

    for batch_no, (start_idx, page_group) in enumerate(batch_list, start=1):
        page_range = f"{start_idx + 1}-{start_idx + len(page_group)}"
        job_manager.append_log(
            job_id, f"處理批次 {batch_no}/{total_batches}（第 {page_range} 頁）"
        )

        raw_batch_text = "\n\n".join(page_group)

        try:
            traditional_text = convert_to_traditional(raw_batch_text)
            if stop_calling_llm:
                if refine_items:
                    job_manager.append_log(
                        job_id, f"批次 {batch_no} 額度已用盡，跳過 LLM 處理，只做簡轉繁"
                    )
                final_text = traditional_text
            else:
                final_text, quota_exhausted = _refine_and_correct(
                    job_id,
                    batch_no,
                    traditional_text,
                    model,
                    max_retry,
                    user_email,
                    file_name,
                    project,
                    refine_items,
                )
                if quota_exhausted:
                    stop_calling_llm = True
                    job_manager.append_log(
                        job_id,
                        f"批次 {batch_no} 額度已用盡，重試 {max_retry} 次仍失敗，"
                        "後續批次直接改用簡轉繁、不再重試 LLM 處理",
                        level="warning",
                    )
        except Exception as exc:  # noqa: BLE001
            # 保底：這個批次不管在哪個環節出了非預期狀況，都不該讓
            # 整份文件的輸出報銷，退回完全沒處理過的 OCR 原始文字
            job_manager.append_log(
                job_id,
                f"批次 {batch_no} 發生未預期錯誤：{exc}，改用原始 OCR 文字（未簡轉繁）",
                level="error",
            )
            final_text = raw_batch_text

        # 不管這個批次走了哪條路徑，統一把殘留的單一換行（OCR 逐行
        # 造成、或 LLM 潤飾後沒完全合併的斷行）正規化成段落分隔，
        # 確保輸出（txt/docx）換行方式一致
        final_text = normalize_paragraph_breaks(final_text)
        refined_chunks.append(final_text)

        # 每批完成就把「目前為止的成果」存起來（會一路寫進 Neon）。
        # 伺服器如果在下一批中途重啟，使用者至少救得回這裡為止的結果，
        # 不用把已經花掉的 LLM 費用整份重跑一次。
        job_manager.set_partial_result(job_id, "\n\n".join(refined_chunks))

        # 批次之間固定停一下，降低短時間內連續打太密集撞到 RPM 上限
        # 的機率；最後一批不用等（後面沒有下一次呼叫了），額度已確定
        # 用盡、後面都不再呼叫 LLM 的情況也不用等（這個延遲本來就是為了
        # 保護 API 呼叫，沒有呼叫就沒有必要空等）
        if batch_no < total_batches and not stop_calling_llm:
            time.sleep(config.BATCH_DELAY_SECONDS)

    result_text = "\n\n".join(refined_chunks)
    job_manager.append_log(job_id, "所有批次處理完成，輸出結果")
    job_manager.set_result(job_id, result_text)


def run_pipeline(job_id: str, pdf_path: str, settings: dict | None = None):
    """一般網頁上傳路徑：OCR + 潤飾兩段都在這裡（通常是伺服器端）做完。"""
    settings = settings or {}
    dpi = settings.get("dpi", config.PDF_RENDER_DPI)
    detect_cover = settings.get("detect_cover", config.COVER_DETECT_DEFAULT)

    job_manager.set_status(job_id, "running")
    try:
        raw_pages, total_pages = run_ocr_stage(job_id, pdf_path, dpi, detect_cover)
        run_refine_stage(job_id, raw_pages, total_pages, settings)
    except Exception as exc:  # noqa: BLE001
        job_manager.append_log(job_id, f"發生錯誤：{exc}", level="error")
        job_manager.set_error(job_id, str(exc))


def run_refine_only_pipeline(job_id: str, raw_pages: list[str], total_pages: int, settings: dict | None = None):
    """桌面版 App 路徑：OCR 已經在使用者本機的 zh-cn-to-tw-ocr-service 做完，
    這裡只跑潤飾那一半，跟 run_pipeline 共用同一層錯誤保底邏輯。"""
    job_manager.set_status(job_id, "running")
    try:
        run_refine_stage(job_id, raw_pages, total_pages, settings)
    except Exception as exc:  # noqa: BLE001
        job_manager.append_log(job_id, f"發生錯誤：{exc}", level="error")
        job_manager.set_error(job_id, str(exc))
