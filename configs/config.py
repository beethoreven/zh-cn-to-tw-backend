"""
專案設定值，全部從環境變數讀取，不寫死在程式碼裡。
本機開發用 .env（跟這支程式同一個 repo 根目錄，不進版控），正式部署則在主機環境變數面板設定。

這裡的數值是「後端預設值」，前端介面上的可調欄位會覆蓋掉這些值
（每個 job 各自可以指定），這裡的值只在前端沒有指定時當 fallback。
"""

import os

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# --- Google 登入 ---

# 在 Google Cloud Console 申請的 OAuth Client ID，前端初始化 Google
# Identity Services、後端驗證 ID Token 的 audience 都要用同一組
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

# 授權白名單/管理員身份都改成完全走 users/permissions 資料表管理
# （見 auth_utils/whitelist.py），不再用 PERMITTED_USER/ADMIN_ACCOUNT
# 這種環境變數白名單——改環境變數要重新部署才生效，改 DB 透過管理員
# 介面立刻生效，而且不會有「環境變數」跟「DB」兩份資料兜不起來的風險。

# LLM API 呼叫的逾時秒數（Gemini/Claude 共用）。曾經實際發生過呼叫卡住
# 永遠沒回應、也沒拋錯的情況，加這個確保最差情況下還是會在有限時間內
# 失敗、讓既有的 retry/fallback 機制接手，不會讓整個批次卡死
LLM_CALL_TIMEOUT_SECONDS = int(os.environ.get("LLM_CALL_TIMEOUT_SECONDS", "90"))

# 每個 refine 批次涵蓋幾頁一起送給 LLM 潤飾；"whole" 代表整本書只當一批
REFINE_BATCH_PAGES = os.environ.get("REFINE_BATCH_PAGES", "20")

# 單一批次 API 呼叫失敗（網路/暫時性錯誤）最多重試幾次
REFINE_MAX_RETRY = int(os.environ.get("REFINE_MAX_RETRY", "3"))

# PDF 轉圖片時的解析度，越高辨識越準但越慢
PDF_RENDER_DPI = int(os.environ.get("PDF_RENDER_DPI", "200"))

UPLOAD_DIR = os.environ.get(
    "UPLOAD_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
)

# 「阿舍老師的叮嚀」內容來源：純文字檔，跟著程式碼一起進版控，要改內容
# 直接編輯這個檔案再 push，不需要動到程式碼或另外做後台介面
TEACHER_NOTICE_PATH = os.environ.get(
    "TEACHER_NOTICE_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "content", "tutor_arthur_announce.txt"
    ),
)

# 封面頁偵測的門檻，純本機影像統計（PIL），不用任何付費 API。這是啟發式
# 規則會有誤判，門檻先抓一個合理的初始值，之後看實際誤判狀況再調整
COVER_DETECT_DEFAULT = os.environ.get("COVER_DETECT_DEFAULT", "true").lower() == "true"
COVER_DETECT_DARK_RATIO_THRESHOLD = float(os.environ.get("COVER_DETECT_DARK_RATIO_THRESHOLD", "0.35"))
COVER_DETECT_SATURATION_THRESHOLD = float(os.environ.get("COVER_DETECT_SATURATION_THRESHOLD", "20"))
COVER_DETECT_RELATIVE_MARGIN = float(os.environ.get("COVER_DETECT_RELATIVE_MARGIN", "1.5"))

# --- 前端可調欄位的上下限，後端也會用同一組邊界做驗證，不能只信任前端 ---
BATCH_PAGES_MIN = 1
BATCH_PAGES_MAX = 50
MAX_RETRY_MIN = 1
MAX_RETRY_MAX = 10
DPI_MIN = 100
DPI_MAX = 400

# "整本丟"：批次頁數欄位如果收到這個特殊值，代表不分批，整本書只當一批
WHOLE_BOOK_SENTINEL = "whole"

# 各 model 每日呼叫次數上限，介面顯示「已用/上限」用；可用環境變數覆蓋
RPD_LIMITS = {
    "gemini-3.6-flash": int(os.environ.get("GEMINI_3_6_FLASH_RPD_LIMIT", "20")),
    "gemini-3.5-flash-lite": int(os.environ.get("GEMINI_3_5_FLASH_LITE_RPD_LIMIT", "500")),
}

# 「個人專案」（projects.owner 是 NULL、所有人共用的那個特殊專案）底下
# Gemini 3.6 Flash 每天可以打幾次，比一般專案嚴格很多——這個專案沒有
# 負責人可以追蹤是誰在用，用這個低額度避免被拿來當白嫖/洗爆額度的後門。
# 特意不做成 DB 可調設定，就是不希望它跟一般的權限額度混在一起管理。
PERSONAL_PROJECT_GEMINI_FLASH_DAILY_LIMIT = int(
    os.environ.get("PERSONAL_PROJECT_GEMINI_FLASH_DAILY_LIMIT", "6")
)

# Gemini model 清單，附上要顯示在介面上的說明文字；也是 /api/usage
# 每日呼叫次數（RPD）統計會遍歷的對象，只有 Gemini 有這種每日額度限制，
# 所以這份清單刻意只保留 Gemini（Claude 是照 token 計費，不適用 RPD）
AVAILABLE_MODELS = {
    "gemini-3.6-flash": {
        "label": "Gemini 3.6 Flash",
        "description": "品質較佳、語意判斷較細緻，適合需要精準校對的場合；但每日額度較少。",
    },
    "gemini-3.5-flash-lite": {
        "label": "Gemini 3.5 Flash Lite",
        "description": (
            "速度快、額度較高，適合大量／整本處理；但語意判斷較粗略，"
            "細節校對可能不如 3.6 Flash 精準。"
        ),
    },
}

# --- Stage 2：二次校對 ---

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MAX_OUTPUT_TOKENS = int(os.environ.get("CLAUDE_MAX_OUTPUT_TOKENS", "8192"))

# Claude 每百萬 token 的價格（美元），只用來換算「本月使用量」的參考費用；
# Anthropic 之後如果調整價格，這裡也要跟著更新（可用環境變數覆蓋，不用改程式碼）
CLAUDE_PRICING = {
    "claude-haiku-4-5-20251001": {
        "input": float(os.environ.get("CLAUDE_HAIKU_PRICE_INPUT", "1.0")),
        "output": float(os.environ.get("CLAUDE_HAIKU_PRICE_OUTPUT", "5.0")),
    },
    "claude-opus-5": {
        "input": float(os.environ.get("CLAUDE_OPUS_PRICE_INPUT", "5.0")),
        "output": float(os.environ.get("CLAUDE_OPUS_PRICE_OUTPUT", "25.0")),
    },
}

# 每個 review 批次涵蓋幾個字送去校對，用字數而非頁數（Stage 1 輸出已經不保留頁界）
REVIEW_BATCH_CHARS = int(os.environ.get("REVIEW_BATCH_CHARS", "1000"))
REVIEW_BATCH_CHARS_MIN = 500
REVIEW_BATCH_CHARS_MAX = 20000

REVIEW_MAX_RETRY = int(os.environ.get("REVIEW_MAX_RETRY", "3"))

DEFAULT_REVIEW_MODEL = os.environ.get("DEFAULT_REVIEW_MODEL", "gemini-3.6-flash")

# Stage 1、Stage 2 共用的完整 model 清單（Gemini 2 個 + Claude 2 個）。
# 各自呼叫互不影響；Claude 這兩個選項會實際計費（沒有 Gemini 那種
# 持續性免費額度），說明文字裡有明講，避免使用者不小心選到。
ALL_MODELS = {
    "gemini-3.6-flash": AVAILABLE_MODELS["gemini-3.6-flash"],
    "gemini-3.5-flash-lite": AVAILABLE_MODELS["gemini-3.5-flash-lite"],
    "claude-haiku-4-5-20251001": {
        "label": "Claude Haiku 4.5",
        "description": (
            "會實際計費（無持續性免費額度），按 token 計費，速度快、成本較低；"
            "適合大量處理，但語意判斷相對淺層，細膩度可能不如 Opus。"
        ),
    },
    "claude-opus-5": {
        "label": "Claude Opus 5",
        "description": (
            "會實際計費（無持續性免費額度），按 token 計費，是 Claude 最高階、"
            "語意判斷最細膩的模型，但價格也最高（輸出約為 Haiku 的 5 倍）。"
        ),
    },
}
