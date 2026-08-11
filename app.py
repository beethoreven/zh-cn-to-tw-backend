"""繁化助手後端 API：上傳 PDF -> 背景執行繁化 pipeline -> 輪詢進度 -> 下載結果。"""

from __future__ import annotations

import io
import os
import socket
import sys
import threading
import time
from functools import wraps

from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

from admin_utils import permissions as admin_permissions
from admin_utils import projects as admin_projects
from admin_utils import users as admin_users
from auth_utils import auth, sessions, whitelist
from configs import config
from db_utils import job_store
from jobs import job_manager
from output_utils.docx_export import docx_file_to_text, text_to_docx_bytes
from pipeline.orchestrator import run_pipeline, run_refine_only_pipeline
from review import review_manager
from review.reviewer import run_review
from usage.fx_rate import get_usd_twd_rate
from usage.usage_log import (
    get_project_token_usage,
    get_today_project_model_count,
    get_today_usage,
    get_total_token_usage,
)

app = Flask(__name__)

# 前端（GitHub Pages）透過瀏覽器 fetch 呼叫這裡，需要 CORS 允許來源網域。
# 本機開發（任意 port 的 localhost/127.0.0.1）也一併放行方便測試。
# allow_headers 要明確帶 Authorization——登入後每次 API 呼叫都會帶
# `Authorization: Bearer <session token>`（見 auth_utils/sessions.py：
# 這是應用程式自己簽發的長效憑證，不是 Google ID Token 本身，Google
# 那個只在 /auth/login 換發 session 的當下用一次），沒有這行，瀏覽器的
# CORS 預檢（preflight）會擋下這個 header，請求根本送不到後端。
# expose_headers 帶 Content-Disposition——下載端點靠這個 header 告訴
# 前端檔名，預設不在瀏覽器允許 JS 讀取的安全清單裡，要明確曝露出來，
# 前端才能在改用 fetch + blob 下載（而不是直接 <a href> 導覽，因為
# 那樣沒辦法附加 Authorization header）時，還原出正確的下載檔名。
CORS(
    app,
    origins=[
        "https://beethoreven.github.io",
        r"http://localhost:\d+",
        r"http://127\.0\.0\.1:\d+",
    ],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["Content-Disposition"],
)

os.makedirs(config.UPLOAD_DIR, exist_ok=True)


def _startup_recover_jobs() -> None:
    """process 啟動時，把資料庫裡還停在 pending/running 的工作標成
    interrupted。這個 process 是新起來的，照定義不可能還有執行緒在跑
    那些工作——不標記的話，前端會永遠看到「執行中」而不會跳出重試
    對話窗，變成無聲卡死。詳見 db_utils/job_store.py 的說明。

    在背景執行緒做：這一步要連 Neon（冷啟動可能近 10 秒），不能擋住
    web server 開始接受連線。"""
    def run():
        count = job_store.mark_running_as_interrupted()
        if count:
            print(f"[startup] 已將 {count} 筆未完成的工作標記為中斷", flush=True)
        while True:
            removed = job_store.delete_expired()
            if removed:
                print(f"[cleanup] 已清除 {removed} 筆超過保留期的工作紀錄", flush=True)
            time.sleep(3600)

    threading.Thread(target=run, daemon=True).start()


_startup_recover_jobs()

STATUS_LABELS = {
    "pending": "等待中",
    "running": "執行中",
    "done": "已完成",
    "failed": "失敗",
    # 伺服器重啟（部署或平台搬機器）導致工作中途被打斷。前端看到這個
    # 狀態會跳出重試／結束對話窗，見 db_utils/job_store.py 的說明。
    "interrupted": "已中斷",
}


def require_auth(view_func):
    """
    裝飾器：檢查請求有沒有帶合法的 session token，且對應的 email 在
    白名單裡。

    從 `Authorization: Bearer <token>` header 讀取前端登入後拿到的
    session token（見 auth_utils/sessions.py——這是應用程式自己簽發的
    長效憑證，不是 Google ID Token 本身），反查對應的 email，再比對
    whitelist.py 的白名單。token 無效或 email 不在白名單，一律回 401，
    不會進到實際的路由邏輯。

    前端會對應把整頁鎖住，但那只是給使用者看的——真正擋掉未授權存取
    的是這裡，就算有人用 devtools 把前端的 disabled 拔掉，這一層一樣會擋。
    """

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        token = auth.extract_bearer_token(request)
        email = sessions.resolve_session(token)
        if email is None or not whitelist.is_permitted_user(email):
            return jsonify({"error": "未登入或此帳號未獲授權"}), 401
        request.user_email = email
        return view_func(*args, **kwargs)

    return wrapper


def require_admin(view_func):
    """
    裝飾器：跟 require_auth 一樣先驗證登入/授權，另外還要求這個帳號
    的角色是管理員，不然一律 401。用在管理員介面（使用者/權限/專案
    管理）的所有 API 上。
    """

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        token = auth.extract_bearer_token(request)
        email = sessions.resolve_session(token)
        if email is None or not whitelist.is_admin_user(email):
            return jsonify({"error": "未登入或此帳號未獲管理員授權"}), 401
        request.user_email = email
        return view_func(*args, **kwargs)

    return wrapper


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/jobs/active")
def active_jobs():
    """目前有幾個工作正在跑。刻意不要求登入：這支是給「部署前先確認
    沒有人的工作跑到一半」用的，部署腳本／CI 拿不到使用者的 session
    token，而這裡只回一個數字，不洩漏任何內容。"""
    return jsonify({"active": job_store.count_active()})


@app.post("/auth/login")
def auth_login():
    """
    前端登入流程的唯一入口：拿 Google ID Token 換一個應用程式自己的
    session token。

    Google ID Token 只在這裡驗證這一次（簽章/過期時間/audience），驗證
    通過、且 email 在白名單裡，就簽發一個 session token 存進 sessions
    表，回傳給前端存起來，之後所有 API 呼叫都改帶這個 session token，
    不再是 Google ID Token 本身——Google 那個效期固定約 1 小時，拿它
    當長效憑證會讓工作階段每小時被打斷一次，這是這個 session token
    層存在的原因，細節說明見 auth_utils/sessions.py 開頭的註解。

    body 需要 { "id_token": "<Google ID Token>" }。
    """
    body = request.get_json(silent=True) or {}
    id_token = body.get("id_token")
    email = auth.verify_google_id_token(id_token)
    if email is None:
        return jsonify({"error": "Google 登入憑證無效或已過期，請重新登入"}), 401

    if not whitelist.is_permitted_user(email):
        return jsonify({"error": "此帳號尚未獲得授權", "email": email}), 403

    token = sessions.create_session(email)
    return jsonify(
        {
            "session_token": token,
            "email": email,
            "is_admin": whitelist.is_admin_user(email),
        }
    ), 200


@app.post("/auth/logout")
def auth_logout():
    """登出：把 session token 從資料庫刪掉，之後這個 token 就不再有效。
    找不到/已經失效也視為成功——登出的目的（這個 token 不能再用）本來
    就已經達成，不需要因為它「本來就已經沒了」而回錯誤。"""
    token = auth.extract_bearer_token(request)
    sessions.delete_session(token)
    return jsonify({"status": "ok"}), 200


@app.get("/auth/status")
def auth_status():
    """
    給前端在登入後（或每次重新整理頁面時）確認用：這個 session token
    還有效嗎？對應的帳號在白名單裡嗎？是不是管理員（決定要不要顯示
    「管理員介面」按鈕）？

    刻意獨立於 require_auth 之外（不共用那個裝飾器）——這支本身的
    用途就是「檢查授權狀態」，token 無效或未授權不算這支 API 本身失敗，
    是正常會發生的查詢結果，所以用 200 + authorized:false 表示，
    只有真的沒帶 token / token 完全解不開，才回 401。
    """
    token = auth.extract_bearer_token(request)
    email = sessions.resolve_session(token)

    if email is None:
        return jsonify({"authorized": False, "error": "登入已逾期或已登出，請重新登入"}), 401

    return jsonify(
        {
            "authorized": whitelist.is_permitted_user(email),
            "is_admin": whitelist.is_admin_user(email),
            "email": email,
        }
    ), 200


@app.get("/api/options")
@require_auth
def get_options():
    """前端用來畫出設定欄位：可選 model、各欄位上下限與預設值。"""
    return jsonify(
        {
            "models": config.ALL_MODELS,
            "default_model": config.GEMINI_MODEL,
            "batch_pages": {
                "min": config.BATCH_PAGES_MIN,
                "max": config.BATCH_PAGES_MAX,
                "default": config.REFINE_BATCH_PAGES,
                "whole_book_value": config.WHOLE_BOOK_SENTINEL,
                "description": (
                    "每批送給 LLM 潤飾的頁數；批次切太細，Gemini 模型會浪費每日呼叫次數額度，"
                    "切太大則有機會超過模型單次輸出上限被截斷。也可以選「整本丟」，"
                    "整本書只呼叫一次，最省額度，但大型劇本較容易被截斷。"
                ),
            },
            "max_retry": {
                "min": config.MAX_RETRY_MIN,
                "max": config.MAX_RETRY_MAX,
                "default": config.REFINE_MAX_RETRY,
                "description": (
                    "API 呼叫失敗（網路或暫時性錯誤）時最多重試幾次，"
                    f"範圍 {config.MAX_RETRY_MIN}-{config.MAX_RETRY_MAX} 次。"
                ),
            },
            "dpi": {
                "min": config.DPI_MIN,
                "max": config.DPI_MAX,
                "default": config.PDF_RENDER_DPI,
                "description": (
                    f"PDF 轉圖片的解析度，範圍 {config.DPI_MIN}-{config.DPI_MAX}，"
                    "越高辨識越準但轉檔與 OCR 越慢。"
                ),
            },
        }
    )


@app.get("/api/usage")
@require_auth
def get_usage():
    """回傳每個 model 今天（美西時區）已用次數與每日上限。"""
    return jsonify(
        {
            model: {
                "used": get_today_usage(model),
                "limit": config.RPD_LIMITS.get(model),
            }
            for model in config.AVAILABLE_MODELS
        }
    )


def _claude_usage_response(usage_by_model_fn) -> dict:
    """共用邏輯：把「查出來的 token 數」轉成 {model: {tokens, usd_cost, twd_cost}}
    的回應格式。usage_by_model_fn(model) 要回傳 {"input_tokens":.., "output_tokens":..}。

    費用是本工具自己記的 token 數乘上目前查到的官方牌價換算出來的參考值，
    不是去查 Anthropic 帳戶的真實帳單金額。
    """
    twd_rate = get_usd_twd_rate()
    result = {}
    for model, pricing in config.CLAUDE_PRICING.items():
        usage = usage_by_model_fn(model)
        usd_cost = (
            usage["input_tokens"] / 1_000_000 * pricing["input"]
            + usage["output_tokens"] / 1_000_000 * pricing["output"]
        )
        result[model] = {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "usd_cost": round(usd_cost, 4),
            "twd_cost": round(usd_cost * twd_rate, 2),
        }
    return {"models": result, "usd_twd_rate": twd_rate}


@app.get("/api/usage/project/<int:project_id>")
@require_auth
def get_project_usage(project_id):
    """回傳這個劇本案累積至今的 Claude token 用量與估計台幣費用，
    給主介面「本案使用 Claude token 狀況」用。只有這個專案的負責人或
    管理員能查——不然任何登入者都能靠猜連續整數 ID（1, 2, 3...）看到
    別人專案的用量／費用資料，等於跨客戶資料外洩。"""
    project = admin_projects.get_project(project_id)
    if project is None:
        return jsonify({"error": "找不到這個專案"}), 404
    user_id = whitelist.get_user_id(request.user_email)
    if project["owner"] != user_id and not whitelist.is_admin_user(request.user_email):
        return jsonify({"error": "沒有權限查看這個專案的用量"}), 403
    return jsonify(_claude_usage_response(lambda model: get_project_token_usage(project_id, model)))


@app.get("/api/my-projects")
@require_auth
def get_my_projects():
    """給主介面「本案處理劇本」下拉選單用：只列出目前登入者自己名下的專案。"""
    user_id = whitelist.get_user_id(request.user_email)
    if user_id is None:
        return jsonify({"projects": []})
    return jsonify({"projects": admin_projects.list_projects_by_owner(user_id)})


def _parse_whole_or_bounded_int(raw_value, default, lo, hi, field_name):
    if raw_value is None or raw_value == "":
        return default
    if raw_value == config.WHOLE_BOOK_SENTINEL:
        return raw_value
    try:
        n = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必須是整數或「{config.WHOLE_BOOK_SENTINEL}」")
    if not (lo <= n <= hi):
        raise ValueError(f"{field_name} 必須介於 {lo}-{hi} 之間")
    return n


def _parse_batch_pages(raw_value):
    return _parse_whole_or_bounded_int(
        raw_value, config.REFINE_BATCH_PAGES, config.BATCH_PAGES_MIN, config.BATCH_PAGES_MAX,
        "batch_pages",
    )


def _parse_bounded_int(raw_value, default, lo, hi, field_name):
    if raw_value is None or raw_value == "":
        return default
    try:
        n = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必須是整數")
    if not (lo <= n <= hi):
        raise ValueError(f"{field_name} 必須介於 {lo}-{hi} 之間")
    return n


def _parse_model(raw_value):
    if raw_value is None or raw_value == "":
        return config.GEMINI_MODEL
    if raw_value not in config.ALL_MODELS:
        raise ValueError(f"未知的 model：{raw_value}")
    return raw_value


def _parse_bool(raw_value, default: bool) -> bool:
    if raw_value is None or raw_value == "":
        return default
    return raw_value.lower() in ("true", "1", "on")


def _require_project_id(raw_value) -> int:
    """每次呼叫 model 的請求都要帶上目前選定的劇本案 ID，寫進 usage_log.project，
    才能在主介面顯示「本案使用 Claude token 狀況」。前端在使用者確認劇本案之前
    會鎖住整個下方區塊，理論上不會漏帶，這裡仍當作必要欄位驗證。"""
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        raise ValueError("缺少或無效的 project（劇本案）ID")


def _validate_project_and_model(project_id: int, model: str) -> None:
    """檢查這次呼叫的 project/model 組合合不合規則，不合規就拋
    ValueError（呼叫端都已經在 try/except ValueError 裡，會被接住轉成
    400）。目前只有「個人專案」（projects.owner 是 NULL、所有人共用的
    那個特殊專案）有這些額外限制：不能用 Claude model，Gemini 3.6
    Flash 每天有獨立的低額度——這個專案沒有負責人可以追蹤是誰在用，
    這些限制是為了不讓它變成白嫖 Claude/洗爆 Gemini 額度的後門。
    前端在選到這個專案時也會把 Claude 選項從選單拿掉，這裡是後端這層
    真正擋掉未授權存取的地方（前端只是視覺提示）。"""
    project = admin_projects.get_project(project_id)
    if project is None:
        raise ValueError(f"找不到 id={project_id} 的專案")
    if project["owner"] is not None:
        return  # 一般專案（有負責人）沒有這些額外限制

    if model.startswith("claude"):
        raise ValueError("個人專案不能使用 Claude Model")
    if model == "gemini-3.6-flash":
        used_today = get_today_project_model_count(project_id, model)
        if used_today >= config.PERSONAL_PROJECT_GEMINI_FLASH_DAILY_LIMIT:
            raise ValueError("個人專案使用量達上限")


@app.post("/api/jobs")
@require_auth
def create_job():
    if "file" not in request.files:
        return jsonify({"error": "缺少檔案，請用 file 欄位上傳 PDF"}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "只接受 PDF 檔案"}), 400

    try:
        settings = {
            "model": _parse_model(request.form.get("model")),
            "batch_pages": _parse_batch_pages(request.form.get("batch_pages")),
            "max_retry": _parse_bounded_int(
                request.form.get("max_retry"),
                config.REFINE_MAX_RETRY,
                config.MAX_RETRY_MIN,
                config.MAX_RETRY_MAX,
                "max_retry",
            ),
            "dpi": _parse_bounded_int(
                request.form.get("dpi"),
                config.PDF_RENDER_DPI,
                config.DPI_MIN,
                config.DPI_MAX,
                "dpi",
            ),
            "file_name": file.filename,
            "detect_cover": _parse_bool(request.form.get("detect_cover"), config.COVER_DETECT_DEFAULT),
            "user_email": request.user_email,
            "project": _require_project_id(request.form.get("project")),
        }
        _validate_project_and_model(settings["project"], settings["model"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = job_manager.create_job(original_filename=file.filename, user_email=request.user_email)
    pdf_path = os.path.join(config.UPLOAD_DIR, f"{job_id}.pdf")
    file.save(pdf_path)

    thread = threading.Thread(
        target=run_pipeline, args=(job_id, pdf_path, settings), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id}), 202


@app.post("/api/jobs/from-ocr-text")
@require_auth
def create_job_from_ocr_text():
    """桌面版 App 專用路徑：PDF 已經在使用者本機的 zh-cn-to-tw-ocr-service
    做完封面偵測 + PaddleOCR，這裡只接手做 OpenCC 簡轉繁 + LLM 潤飾那一半
    （run_refine_only_pipeline），跟一般網頁上傳路徑共用同一套潤飾/校對
    邏輯與 project/model 規則檢查，只是不重跑 OCR。"""
    data = request.get_json(silent=True) or {}
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages or not all(isinstance(p, str) for p in pages):
        return jsonify({"error": "缺少或格式錯誤的 pages（應為文字陣列）"}), 400

    try:
        settings = {
            "model": _parse_model(data.get("model")),
            "batch_pages": _parse_batch_pages(data.get("batch_pages")),
            "max_retry": _parse_bounded_int(
                data.get("max_retry"),
                config.REFINE_MAX_RETRY,
                config.MAX_RETRY_MIN,
                config.MAX_RETRY_MAX,
                "max_retry",
            ),
            "file_name": data.get("file_name"),
            "user_email": request.user_email,
            "project": _require_project_id(data.get("project")),
        }
        _validate_project_and_model(settings["project"], settings["model"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = job_manager.create_job(original_filename=settings["file_name"], user_email=request.user_email)

    thread = threading.Thread(
        target=run_refine_only_pipeline, args=(job_id, pages, len(pages), settings), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id}), 202


@app.post("/api/jobs/direct-upload")
@require_auth
def create_direct_job():
    """直接上傳已經是繁體的 .docx/.txt，跳過 Stage 1，產生一個
    「已完成」的 job，讓 Stage 2 可以照平常的流程去校對。"""
    if "file" not in request.files:
        return jsonify({"error": "缺少檔案，請用 file 欄位上傳"}), 400

    file = request.files["file"]
    filename_lower = file.filename.lower()

    try:
        if filename_lower.endswith(".txt"):
            text = file.read().decode("utf-8")
        elif filename_lower.endswith(".docx"):
            text = docx_file_to_text(file)
        else:
            return jsonify({"error": "只接受 .docx 或 .txt 檔案"}), 400
    except UnicodeDecodeError:
        return jsonify({"error": ".txt 檔案不是 UTF-8 編碼，請另存成 UTF-8 後再上傳"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"讀取檔案內容失敗：{exc}"}), 400

    job_id = job_manager.create_job(original_filename=file.filename, user_email=request.user_email)
    job_manager.set_result(job_id, text)

    return jsonify({"job_id": job_id}), 202


def _get_owned_job(job_id: str):
    """回傳 (job, None) 或 (None, 錯誤回應)。job_id 是 uuid4 前 12 碼的
    亂數，本身就很難猜，但只擋「猜不到」不夠——只要曾經被別人看過這個
    ID（截圖、支援訊息、瀏覽器歷史紀錄…），任何登入帳號都能拿去查別人
    的 job，包括下載劇本內容。這裡加一層「一定要是自己建立的 job」才
    給資料，找不到或不是自己的都回同一個 404，不特別區分，避免讓對方
    知道「這個 ID 其實存在，只是不是你的」。管理員也不例外——管理員
    介面本來就沒有瀏覽 job 的功能，不需要為了不存在的需求開這個後門。"""
    job = job_manager.get_job(job_id)
    if job is None or job.get("user_email") != request.user_email:
        return None, (jsonify({"error": "找不到這個 job"}), 404)
    return job, None


def _get_owned_review(review_id: str):
    """回傳 (review, None) 或 (None, 錯誤回應)，邏輯跟 _get_owned_job
    一樣：review_id 難猜，但不是猜不到就等於安全，一定要是自己建立的
    review 才給資料。"""
    review = review_manager.get_review(review_id)
    if review is None or review.get("user_email") != request.user_email:
        return None, (jsonify({"error": "找不到這個 review"}), 404)
    return review, None


@app.get("/api/jobs/<job_id>")
@require_auth
def get_job(job_id):
    job, err = _get_owned_job(job_id)
    if err:
        return err

    return jsonify(
        {
            "id": job["id"],
            "status": job["status"],
            "status_label": STATUS_LABELS.get(job["status"], job["status"]),
            "logs": job["logs"],
            "error": job["error"],
            "has_result": job["result_text"] is not None,
            # 工作被中斷時，前端要知道還有沒有部分成果可以收尾
            "has_partial": bool(job.get("partial_text")),
        }
    )


@app.post("/api/jobs/<job_id>/finalize")
@require_auth
def finalize_job(job_id):
    """工作被伺服器重啟打斷後，使用者選「結束此階段工作」時呼叫：
    把中斷前已經完成的批次成果當成最終結果收尾，讓他至少能把做到一半
    （而且已經付費）的東西下載出來，不必整份重跑。"""
    job, err = _get_owned_job(job_id)
    if err:
        return err
    if not job_manager.finalize_with_partial(job_id):
        return jsonify({"error": "這個工作沒有任何已完成的部分可以保留"}), 409
    return jsonify({"status": "done"})


def _basename_from_original(original_filename: str | None, fallback: str) -> str:
    """給 usage_log 的 file_name 欄位用：Stage 1 直接帶過來的內容（原始
    檔名是 .pdf）記錄時不帶副檔名，直接上傳的 .docx/.txt 才是真的檔案、
    副檔名要照實記錄——這裡只特意去掉 .pdf 是配合這條紀錄規則，不要
    為了下載檔名的用途改掉這支，改成通用去副檔名會弄壞這個規則。"""
    if not original_filename:
        return fallback
    if original_filename.lower().endswith(".pdf"):
        return original_filename[: -len(".pdf")]
    return original_filename


def _download_basename(original_filename: str | None, fallback: str) -> str:
    """給下載檔名用：不管原始檔案是 .pdf/.docx/.txt，一律去掉副檔名，
    避免後面重新接上 .txt/.docx 時變成雙重副檔名（例如直接上傳
    「朱懿安先行本.txt」，下載時卻變成「朱懿安先行本.txt.txt」）。跟
    _basename_from_original 是不同用途，不要共用。"""
    if not original_filename:
        return fallback
    return os.path.splitext(original_filename)[0] or fallback


def _send_text_as(text: str, fmt: str, basename: str):
    if fmt == "docx":
        docx_bytes = text_to_docx_bytes(text)
        buffer = io.BytesIO(docx_bytes)
        return send_file(
            buffer,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=f"{basename}.docx",
        )

    if fmt != "txt":
        return jsonify({"error": "format 只接受 txt 或 docx"}), 400

    buffer = io.BytesIO(text.encode("utf-8"))
    return send_file(
        buffer,
        mimetype="text/plain; charset=utf-8",
        as_attachment=True,
        download_name=f"{basename}.txt",
    )


@app.get("/api/jobs/<job_id>/download")
@require_auth
def download_job(job_id):
    job, err = _get_owned_job(job_id)
    if err:
        return err
    if job["result_text"] is None:
        return jsonify({"error": "這個 job 還沒有結果"}), 409

    fmt = request.args.get("format", "txt").lower()
    basename = _download_basename(job.get("original_filename"), job_id)
    return _send_text_as(job["result_text"], fmt, basename)


# --- Stage 2：二次校對 ---


@app.get("/api/review-options")
@require_auth
def get_review_options():
    return jsonify(
        {
            "models": config.ALL_MODELS,
            "default_model": config.DEFAULT_REVIEW_MODEL,
            "batch_chars": {
                "min": config.REVIEW_BATCH_CHARS_MIN,
                "max": config.REVIEW_BATCH_CHARS_MAX,
                "default": config.REVIEW_BATCH_CHARS,
                "whole_book_value": config.WHOLE_BOOK_SENTINEL,
                "description": (
                    "字數越少召回率越高，但對 Gemini 模型會增加 RPD 使用率，"
                    "Claude 模型不受影響建議壓低。"
                ),
            },
            "max_retry": {
                "min": config.MAX_RETRY_MIN,
                "max": config.MAX_RETRY_MAX,
                "default": config.REVIEW_MAX_RETRY,
                "description": (
                    "API 呼叫失敗（網路或暫時性錯誤）時最多重試幾次，"
                    f"範圍 {config.MAX_RETRY_MIN}-{config.MAX_RETRY_MAX} 次。"
                ),
            },
        }
    )


def _parse_review_model(raw_value):
    if raw_value is None or raw_value == "":
        return config.DEFAULT_REVIEW_MODEL
    if raw_value not in config.ALL_MODELS:
        raise ValueError(f"未知的 model：{raw_value}")
    return raw_value


def _parse_review_settings(form):
    model = _parse_review_model(form.get("model"))
    project = _require_project_id(form.get("project"))
    _validate_project_and_model(project, model)
    return {
        "model": model,
        "batch_chars": _parse_whole_or_bounded_int(
            form.get("batch_chars"),
            config.REVIEW_BATCH_CHARS,
            config.REVIEW_BATCH_CHARS_MIN,
            config.REVIEW_BATCH_CHARS_MAX,
            "batch_chars",
        ),
        "max_retry": _parse_bounded_int(
            form.get("max_retry"),
            config.REVIEW_MAX_RETRY,
            config.MAX_RETRY_MIN,
            config.MAX_RETRY_MAX,
            "max_retry",
        ),
        # request 是 Flask 的 context-local proxy，這支函式只會在
        # request-handling 的路由裡被呼叫，直接拿 request.user_email
        # 是安全的（require_auth 裝飾器已經驗證過並存進去）
        "user_email": request.user_email,
        "project": project,
    }


def _launch_review(
    source_job_id: str,
    text: str,
    settings: dict,
    exclude_fingerprints: list | None = None,
    file_name: str | None = None,
) -> str:
    review_id = review_manager.create_review(
        source_job_id,
        text,
        exclude_fingerprints=exclude_fingerprints,
        user_email=settings.get("user_email"),
    )
    run_settings = dict(settings, exclude_fingerprints=exclude_fingerprints or [], file_name=file_name)
    thread = threading.Thread(
        target=run_review, args=(review_id, text, run_settings), daemon=True
    )
    thread.start()
    return review_id


@app.post("/api/jobs/<job_id>/review")
@require_auth
def start_review(job_id):
    job, err = _get_owned_job(job_id)
    if err:
        return err
    if job["result_text"] is None:
        return jsonify({"error": "這個 job 還沒有繁化結果，無法校對"}), 409

    try:
        settings = _parse_review_settings(request.form)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Stage 1 直接帶過來的內容不對應任何實際檔案，記錄用量時不帶副檔名；
    # 直接上傳的 .docx/.txt 才是真的檔案，副檔名照實記錄
    # （_basename_from_original 剛好只會去掉 .pdf，符合這個規則）
    file_name = _basename_from_original(job.get("original_filename"), job_id)

    review_id = _launch_review(job_id, job["result_text"], settings, file_name=file_name)
    return jsonify({"review_id": review_id}), 202


@app.post("/api/reviews/<review_id>/rerun")
@require_auth
def rerun_review(review_id):
    """對這次 review 套用勾選建議後的文字，重新再校對一輪（手動觸發，
    不做自動無限遞迴，避免無預警燒額度）。"""
    review, err = _get_owned_review(review_id)
    if err:
        return err

    text = review["applied_text"] or review["source_text"]
    exclude_fingerprints = review_manager.get_combined_exclude_fingerprints(review_id)

    try:
        settings = _parse_review_settings(request.form)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    source_job = job_manager.get_job(review["source_job_id"])
    file_name = _basename_from_original(
        source_job.get("original_filename") if source_job else None, review_id
    )

    new_review_id = _launch_review(
        review["source_job_id"],
        text,
        settings,
        exclude_fingerprints=exclude_fingerprints,
        file_name=file_name,
    )
    return jsonify({"review_id": new_review_id}), 202


@app.get("/api/reviews/<review_id>")
@require_auth
def get_review(review_id):
    review, err = _get_owned_review(review_id)
    if err:
        return err

    return jsonify(
        {
            "id": review["id"],
            "status": review["status"],
            "status_label": STATUS_LABELS.get(review["status"], review["status"]),
            "logs": review["logs"],
            "error": review["error"],
            "findings": review["findings"],
            "has_applied": review["applied_text"] is not None,
            "has_partial": bool(review.get("partial_findings")),
        }
    )


@app.post("/api/reviews/<review_id>/finalize")
@require_auth
def finalize_review(review_id):
    """校對被伺服器重啟打斷後，使用者選「結束此階段工作」時呼叫：
    把中斷前已經校對出來的建議當成最終結果收尾（比照 finalize_job）。"""
    review, err = _get_owned_review(review_id)
    if err:
        return err
    if not review_manager.finalize_with_partial(review_id):
        return jsonify({"error": "這次校對沒有任何已完成的部分可以保留"}), 409
    return jsonify({"status": "done"})


@app.post("/api/reviews/<review_id>/apply")
@require_auth
def apply_review(review_id):
    review, err = _get_owned_review(review_id)
    if err:
        return err
    if review["status"] != "done":
        return jsonify({"error": "這個校對還沒完成，無法套用"}), 409

    body = request.get_json(silent=True) or {}
    selected_ids = body.get("selected_ids")
    if not isinstance(selected_ids, list):
        return jsonify({"error": "selected_ids 必須是陣列"}), 400

    try:
        selected_ids_set = {int(i) for i in selected_ids}
    except (TypeError, ValueError):
        return jsonify({"error": "selected_ids 裡的元素必須是整數"}), 400

    applied_text = review_manager.apply_selected(review_id, selected_ids_set)
    review_manager.set_applied_text(review_id, applied_text)
    review_manager.record_rejected_findings(review_id, selected_ids_set)

    return jsonify({"text": applied_text})


@app.get("/api/reviews/<review_id>/download")
@require_auth
def download_review(review_id):
    review, err = _get_owned_review(review_id)
    if err:
        return err

    text = review["applied_text"] or review["source_text"]
    fmt = request.args.get("format", "txt").lower()

    source_job = job_manager.get_job(review["source_job_id"])
    original_filename = source_job.get("original_filename") if source_job else None
    basename = _download_basename(original_filename, review_id)
    return _send_text_as(text, fmt, basename)


# --- 管理員介面 ---
#
# 這一整段都掛 require_admin，只有 users.role 對應到 permissions.role
# = "admin" 的帳號能呼叫。「有沒有真的改到東西」的判斷都在 admin_utils
# 那幾支模組裡做（比對 DB 現有值 vs 送進來的新值），這裡只負責把
# request 的資料轉成呼叫參數、把結果包成 API 回應。


def _require_status(raw_value: str, valid: tuple, field_name: str) -> str:
    if raw_value not in valid:
        raise ValueError(f"{field_name} 必須是 {', '.join(valid)} 其中之一")
    return raw_value


def _require_role_id(raw_value) -> int:
    try:
        role_id = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError("權限必須是數字")
    if admin_permissions.get_permission(role_id) is None:
        raise ValueError(f"找不到 id={role_id} 的權限")
    return role_id


def _require_owner_id(raw_value) -> int:
    """驗證負責人 ID 指向一個真的存在的使用者，不要只驗證「是不是數字」
    就直接寫進 DB——不然一個不存在的 owner id 會一路送到資料庫，才因為
    外鍵違反而噴錯，而且噴出來的是沒被接住的 500，不是清楚的錯誤訊息。"""
    try:
        owner_id = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError("負責人必須是數字")
    if admin_users.get_user(owner_id) is None:
        raise ValueError(f"找不到 id={owner_id} 的使用者")
    return owner_id


@app.get("/admin/users")
@require_admin
def admin_list_users():
    return jsonify({"users": admin_users.list_users(role_name=request.args.get("role"))})


@app.get("/admin/users/active-names")
@require_admin
def admin_list_active_user_names():
    """給專案管理頁籤的「負責人」下拉選單用。"""
    return jsonify({"users": admin_users.list_active_user_names()})


@app.get("/admin/users/<int:user_id>")
@require_admin
def admin_get_user(user_id):
    user = admin_users.get_user(user_id)
    if user is None:
        return jsonify({"error": "找不到這個使用者"}), 404
    return jsonify(user)


@app.get("/admin/users/<int:user_id>/projects")
@require_admin
def admin_get_user_projects(user_id):
    """給使用者管理頁籤讀取某個使用者之後，順便顯示他名下專案清單用
    （排除 closed 狀態，closed 的專案對管理成員這件事已經沒有意義）。"""
    return jsonify({"projects": admin_projects.list_owner_projects_excluding_closed(user_id)})


@app.post("/admin/users")
@require_admin
def admin_create_user():
    body = request.get_json(silent=True) or {}
    try:
        email = str(body.get("email") or "").strip()
        if not email:
            raise ValueError("信箱不能空白")
        name = str(body.get("name") or "").strip()
        role = _require_role_id(body.get("role"))
        status = _require_status(body.get("status"), ("active", "deactive"), "狀態")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        user = admin_users.create_user(email, name, role, status)
    except Exception as exc:  # noqa: BLE001
        # 最常見的失敗原因是 email 已經存在(UNIQUE 限制)
        return jsonify({"error": f"建立失敗：{exc}"}), 400
    return jsonify(user), 201


@app.put("/admin/users/<int:user_id>")
@require_admin
def admin_update_user(user_id):
    body = request.get_json(silent=True) or {}
    try:
        email = str(body.get("email") or "").strip()
        if not email:
            raise ValueError("信箱不能空白")
        name = str(body.get("name") or "").strip()
        role = _require_role_id(body.get("role"))
        status = _require_status(body.get("status"), ("active", "deactive"), "狀態")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        changed = admin_users.update_user(user_id, email, name, role, status)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"changed": changed, "user": admin_users.get_user(user_id)})


@app.get("/admin/permissions")
@require_admin
def admin_list_permissions():
    # 使用者管理頁籤的「權限」下拉需要看到管理員選項，權限管理頁籤的
    # 「選擇權限」下拉則要排除管理員——用查詢參數區分這兩種情境
    include_admin = request.args.get("include_admin", "").lower() == "true"
    return jsonify({"permissions": admin_permissions.list_permissions(exclude_admin=not include_admin)})


@app.get("/admin/permissions/<int:permission_id>")
@require_admin
def admin_get_permission(permission_id):
    permission = admin_permissions.get_permission(permission_id)
    if permission is None:
        return jsonify({"error": "找不到這個權限"}), 404
    return jsonify(permission)


@app.put("/admin/permissions/<int:permission_id>")
@require_admin
def admin_update_permission(permission_id):
    body = request.get_json(silent=True) or {}
    try:
        role = str(body.get("role") or "").strip()
        if not role:
            raise ValueError("權限名稱不能空白")
        allowed_haiku = int(body.get("allowed_haiku"))
        if allowed_haiku < 0:
            raise ValueError("使用上限不能是負數")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "使用上限必須是數字"}), 400

    try:
        changed = admin_permissions.update_permission(permission_id, role, allowed_haiku)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"changed": changed, "permission": admin_permissions.get_permission(permission_id)})


@app.get("/admin/projects")
@require_admin
def admin_list_projects():
    return jsonify({"projects": admin_projects.list_projects()})


@app.get("/admin/projects/<int:project_id>")
@require_admin
def admin_get_project(project_id):
    project = admin_projects.get_project(project_id)
    if project is None:
        return jsonify({"error": "找不到這個專案"}), 404
    return jsonify(project)


@app.post("/admin/projects")
@require_admin
def admin_create_project():
    body = request.get_json(silent=True) or {}
    try:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("名稱不能空白")
        owner = _require_owner_id(body.get("owner"))
        status = _require_status(body.get("status"), admin_projects.VALID_STATUSES, "狀態")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "負責人必須是數字"}), 400

    try:
        project = admin_projects.create_project(name, owner, status)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"建立失敗：{exc}"}), 400
    return jsonify(project), 201


@app.get("/admin/usage/totals")
@require_admin
def admin_get_usage_totals():
    """回傳 Claude 系列 model 從有紀錄以來累積的總 token 用量與估計台幣費用，
    顯示在管理員介面頁籤上方（取代舊版主介面的「本月使用量」——正式上線後
    對一般使用者已無意義，改成只有管理員看得到的全站總量）。"""
    return jsonify(_claude_usage_response(get_total_token_usage))


@app.put("/admin/projects/<int:project_id>")
@require_admin
def admin_update_project(project_id):
    body = request.get_json(silent=True) or {}
    try:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("名稱不能空白")
        owner = _require_owner_id(body.get("owner"))
        status = _require_status(body.get("status"), admin_projects.VALID_STATUSES, "狀態")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "負責人必須是數字"}), 400

    try:
        changed = admin_projects.update_project(project_id, name, owner, status)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"changed": changed, "project": admin_projects.get_project(project_id)})


# --- 前端靜態檔案 ---
# 桌面版 App 把整份 zh-cn-to-tw-web 前端包進 .app，由這支 backend 自己
# 一併供應，不再從 GitHub Pages 線上抓（見 config.WEB_STATIC_DIR 說明）。
# 這樣網頁跟 API 變成同一個 origin，連 CORS 都不需要了。
# WEB_STATIC_DIR 沒設定時這兩支路由一律 404，純 API 模式行為不變。


@app.get("/")
def serve_index():
    if not config.WEB_STATIC_DIR:
        return jsonify({"error": "這個部署沒有供應前端檔案"}), 404
    return send_from_directory(config.WEB_STATIC_DIR, "index.html")


@app.get("/<path:filename>")
def serve_web_asset(filename):
    """前端的 script.js/style.css/favicon.png/teacher-notice.txt 等等。
    這條路由帶 <path:> 轉換器，是所有路由裡最不精確的一條，Werkzeug 會
    優先比對其他明確寫死的路由（/api/..., /auth/..., /admin/...），不會
    把 API 請求吃掉。send_from_directory 本身會擋掉 ../ 這類路徑穿越。"""
    if not config.WEB_STATIC_DIR:
        return jsonify({"error": "這個部署沒有供應前端檔案"}), 404
    return send_from_directory(config.WEB_STATIC_DIR, filename)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    # 預設關閉 debug 模式——Werkzeug 的互動除錯器一旦在正式環境被觸發，
    # 會把完整 stack trace、原始碼路徑暴露給任何看得到錯誤頁面的人，
    # 嚴重一點甚至是可利用的 RCE 介面。本機開發需要的話可以另外設
    # FLASK_DEBUG=true，不需要動程式碼。
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    # threaded=True 是關鍵：Werkzeug 預設單執行緒，一次只能處理一個
    # HTTP 連線。Stage 1/2 送出後會在背景執行緒跑 OCR/PDF/LLM 呼叫，
    # 這段期間如果 Werkzeug 不能同時接其他連線，前端每 1.5 秒一次的
    # 輪詢就會在處理中被卡住、逾時，瀏覽器端看到的就是 net::ERR_FAILED
    # （連 CORS header 都沒有，因為連線根本沒被處理完）。程式碼裡本來
    # 就已經對所有共用可變狀態加了鎖（db_utils.connection.LOCK、
    # job_manager/review_manager 各自的 _lock、ocr_engine 的
    # _ocr_lock），本來就是為了支援並行存取設計的，只是少了這個
    # threaded=True 讓 Werkzeug 真的並行處理請求，之前一直沒發現。
    #
    # 桌面版 App（DESKTOP_SERVICE=1）：跟 zh-cn-to-tw-ocr-service 同一套
    # 做法——跟作業系統要一個沒人用的空 port、只綁 127.0.0.1（不對外網
    # 開放，這是使用者自己機器上的服務）、把實際拿到的 port 印到 stdout
    # 給 Swift 殼讀取。絕不寫死 port，這個專案吃過太多次「舊 process 卡住
    # 固定 port」的虧。其他情況（例如 Render 部署）維持原本的 0.0.0.0:5001。
    desktop_service = os.environ.get("DESKTOP_SERVICE", "").lower() in ("1", "true")
    if desktop_service:
        port = int(os.environ.get("BACKEND_PORT", "0")) or _find_free_port()
        print(f"BACKEND_PORT={port}", flush=True)
        sys.stdout.flush()
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)
    else:
        app.run(host="0.0.0.0", port=5001, debug=debug_mode, use_reloader=False, threaded=True)
