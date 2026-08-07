"""繁化助手後端 API：上傳 PDF -> 背景執行繁化 pipeline -> 輪詢進度 -> 下載結果。"""

from __future__ import annotations

import io
import os
import threading
from functools import wraps

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

from admin_utils import permissions as admin_permissions
from admin_utils import projects as admin_projects
from admin_utils import users as admin_users
from auth_utils import auth, whitelist
from configs import config
from jobs import job_manager
from output_utils.docx_export import docx_file_to_text, text_to_docx_bytes
from pipeline.orchestrator import run_pipeline
from review import review_manager
from review.reviewer import run_review
from usage.fx_rate import get_usd_twd_rate
from usage.usage_log import get_month_token_usage, get_today_usage

app = Flask(__name__)

# 前端（GitHub Pages）透過瀏覽器 fetch 呼叫這裡，需要 CORS 允許來源網域。
# 本機開發（任意 port 的 localhost/127.0.0.1）也一併放行方便測試。
# allow_headers 要明確帶 Authorization——登入後每次 API 呼叫都會帶
# `Authorization: Bearer <Google ID Token>`，沒有這行，瀏覽器的
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

STATUS_LABELS = {
    "pending": "等待中",
    "running": "執行中",
    "done": "已完成",
    "failed": "失敗",
}


def require_auth(view_func):
    """
    裝飾器：檢查請求有沒有帶合法的 Google 登入憑證，且對應的 email 在白名單裡。

    從 `Authorization: Bearer <token>` header 讀取前端登入後拿到的
    Google ID Token，交給 auth.verify_google_id_token 驗證簽章/過期時間/
    audience，拿到「Google 保證過的」email，再比對 whitelist.py 的白名單。
    token 無效或 email 不在白名單，一律回 401，不會進到實際的路由邏輯。

    前端會對應把整頁鎖住，但那只是給使用者看的——真正擋掉未授權存取
    的是這裡，就算有人用 devtools 把前端的 disabled 拔掉，這一層一樣會擋。
    """

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        token = auth.extract_bearer_token(request)
        email = auth.verify_google_id_token(token)
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
        email = auth.verify_google_id_token(token)
        if email is None or not whitelist.is_admin_user(email):
            return jsonify({"error": "未登入或此帳號未獲管理員授權"}), 401
        request.user_email = email
        return view_func(*args, **kwargs)

    return wrapper


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/auth/status")
def auth_status():
    """
    給前端在登入後（或每次重新整理頁面時）確認用：這個 Google 帳號的
    登入憑證有效嗎？這個 email 在白名單裡嗎？是不是管理員（決定要不要
    顯示「管理員介面」按鈕）？

    刻意獨立於 require_auth 之外（不共用那個裝飾器）——這支本身的
    用途就是「檢查授權狀態」，token 無效或未授權不算這支 API 本身失敗，
    是正常會發生的查詢結果，所以用 200 + authorized:false 表示，
    只有真的沒帶 token / token 完全解不開，才回 401。
    """
    token = auth.extract_bearer_token(request)
    email = auth.verify_google_id_token(token)

    if email is None:
        return jsonify({"authorized": False, "error": "登入憑證無效或已過期"}), 401

    return jsonify(
        {
            "authorized": whitelist.is_permitted_user(email),
            "is_admin": whitelist.is_admin_user(email),
            "email": email,
        }
    ), 200


@app.get("/api/teacher-notice")
@require_auth
def get_teacher_notice():
    """讀取「阿舍老師的叮嚀」內容檔；檔案不存在或讀取失敗就回空字串，
    不要因為這個非必要的裝飾性內容讓整個介面掛掉。"""
    try:
        with open(config.TEACHER_NOTICE_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    return jsonify({"text": text})


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


@app.get("/api/usage/monthly")
@require_auth
def get_monthly_usage():
    """回傳 Claude 系列 model 本月（台灣時區日曆月）的 token 用量與估計台幣費用。

    費用是本工具自己記的 token 數乘上目前查到的官方牌價換算出來的參考值，
    不是去查 Anthropic 帳戶的真實帳單金額。
    """
    twd_rate = get_usd_twd_rate()
    result = {}
    for model, pricing in config.CLAUDE_PRICING.items():
        usage = get_month_token_usage(model)
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
    return jsonify({"models": result, "usd_twd_rate": twd_rate})


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
        }
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = job_manager.create_job(original_filename=file.filename)
    pdf_path = os.path.join(config.UPLOAD_DIR, f"{job_id}.pdf")
    file.save(pdf_path)

    thread = threading.Thread(
        target=run_pipeline, args=(job_id, pdf_path, settings), daemon=True
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

    if filename_lower.endswith(".txt"):
        text = file.read().decode("utf-8")
    elif filename_lower.endswith(".docx"):
        text = docx_file_to_text(file)
    else:
        return jsonify({"error": "只接受 .docx 或 .txt 檔案"}), 400

    job_id = job_manager.create_job(original_filename=file.filename)
    job_manager.set_result(job_id, text)

    return jsonify({"job_id": job_id}), 202


@app.get("/api/jobs/<job_id>")
@require_auth
def get_job(job_id):
    job = job_manager.get_job(job_id)
    if job is None:
        return jsonify({"error": "找不到這個 job"}), 404

    return jsonify(
        {
            "id": job["id"],
            "status": job["status"],
            "status_label": STATUS_LABELS.get(job["status"], job["status"]),
            "logs": job["logs"],
            "error": job["error"],
            "has_result": job["result_text"] is not None,
        }
    )


def _basename_from_original(original_filename: str | None, fallback: str) -> str:
    if not original_filename:
        return fallback
    if original_filename.lower().endswith(".pdf"):
        return original_filename[: -len(".pdf")]
    return original_filename


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
    job = job_manager.get_job(job_id)
    if job is None:
        return jsonify({"error": "找不到這個 job"}), 404
    if job["result_text"] is None:
        return jsonify({"error": "這個 job 還沒有結果"}), 409

    fmt = request.args.get("format", "txt").lower()
    basename = _basename_from_original(job.get("original_filename"), job_id)
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
    return {
        "model": _parse_review_model(form.get("model")),
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
    }


def _launch_review(
    source_job_id: str,
    text: str,
    settings: dict,
    exclude_fingerprints: list | None = None,
    file_name: str | None = None,
) -> str:
    review_id = review_manager.create_review(
        source_job_id, text, exclude_fingerprints=exclude_fingerprints
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
    job = job_manager.get_job(job_id)
    if job is None:
        return jsonify({"error": "找不到這個 job"}), 404
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
    review = review_manager.get_review(review_id)
    if review is None:
        return jsonify({"error": "找不到這個 review"}), 404

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
    review = review_manager.get_review(review_id)
    if review is None:
        return jsonify({"error": "找不到這個 review"}), 404

    return jsonify(
        {
            "id": review["id"],
            "status": review["status"],
            "status_label": STATUS_LABELS.get(review["status"], review["status"]),
            "logs": review["logs"],
            "error": review["error"],
            "findings": review["findings"],
            "has_applied": review["applied_text"] is not None,
        }
    )


@app.post("/api/reviews/<review_id>/apply")
@require_auth
def apply_review(review_id):
    review = review_manager.get_review(review_id)
    if review is None:
        return jsonify({"error": "找不到這個 review"}), 404
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
    review = review_manager.get_review(review_id)
    if review is None:
        return jsonify({"error": "找不到這個 review"}), 404

    text = review["applied_text"] or review["source_text"]
    fmt = request.args.get("format", "txt").lower()

    source_job = job_manager.get_job(review["source_job_id"])
    original_filename = source_job.get("original_filename") if source_job else None
    basename = _basename_from_original(original_filename, review_id)
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
        allowed_opus = int(body.get("allowed_opus"))
        allowed_haiku = int(body.get("allowed_haiku"))
        if allowed_opus < 0 or allowed_haiku < 0:
            raise ValueError("使用上限不能是負數")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "使用上限必須是數字"}), 400

    try:
        changed = admin_permissions.update_permission(permission_id, role, allowed_opus, allowed_haiku)
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
        owner = int(body.get("owner"))
        status = _require_status(body.get("status"), admin_projects.VALID_STATUSES, "狀態")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "負責人必須是數字"}), 400

    try:
        project = admin_projects.create_project(name, owner, status)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"建立失敗：{exc}"}), 400
    return jsonify(project), 201


@app.put("/admin/projects/<int:project_id>")
@require_admin
def admin_update_project(project_id):
    body = request.get_json(silent=True) or {}
    try:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("名稱不能空白")
        owner = int(body.get("owner"))
        status = _require_status(body.get("status"), admin_projects.VALID_STATUSES, "狀態")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "負責人必須是數字"}), 400

    try:
        changed = admin_projects.update_project(project_id, name, owner, status)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"changed": changed, "project": admin_projects.get_project(project_id)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True, use_reloader=False)
