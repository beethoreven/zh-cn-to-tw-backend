"""
Gemini/Claude 呼叫使用量記錄。

預設存在本機 SQLite（usage.db，跟這支程式同一個 repo 根目錄），適合本機
開發。但 Render 免費方案的檔案系統是非持久性的——沒有掛付費 Disk 的話，
每次重新部署本機檔案都會被清空，SQLite 檔案沒辦法跨部署存活。所以正式
環境改用 Neon（免費、永久不過期的 Postgres）：只要有設定 DATABASE_URL
這個環境變數，就會改連 Postgres；沒設定（本機開發預設）就照舊用 SQLite，
兩邊程式碼共用，只有連線/建表那幾行語法不同，查詢邏輯完全一樣。

用「事件日誌」設計（每次呼叫一筆紀錄，含時間戳），不用「每日累加」的
彙總表，原因：
1. 資料量對這個工具的實際用量來說微不足道，累積多年也不會有效能問題。
2. 保留完整時間戳，之後不管想用哪個時區、哪種統計粒度回頭分析都可以，
   不會被彙總表當初選定的時區/粒度綁死。
3. Stage 3 加上登入後，只要用既有的 user_email 欄位就能做到「每人分開
   統計」，不用改表結構、不用做資料遷移。

「今天」（Gemini 用）採用官方文件講的額度重置時區——美西時間
（America/Los_Angeles）；「本月」（Claude 用，Claude 沒有 Gemini 那種
免費 RPD，這裡記的是 token 數/估計費用）用台灣時區的日曆月，比較貼近
使用者自己看帳的直覺。都用 zoneinfo 處理，不必自己手動換算日光節約。

注意：這裡記的是「這個工具打了幾次/用了多少 token」，跟 Google/Anthropic
官方帳務是兩回事——如果同一把 API key 也在別處使用，這裡的數字會跟
官方帳單對不上，只能當作本工具自己的用量參考。
"""

from __future__ import annotations

import calendar
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_DB_PATH = Path(__file__).resolve().parent.parent / "usage.db"
_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_USE_POSTGRES = bool(_DATABASE_URL)
_RESET_TZ = ZoneInfo("America/Los_Angeles")
_TAIWAN_TZ = ZoneInfo("Asia/Taipei")
_UTC = ZoneInfo("UTC")
_lock = threading.Lock()

# 「本月」（Claude 用量統計）的起算日，預設每月 1 號。這個跟 Anthropic
# Console 帳單實際的計費週期不一定一樣（Console 帳號的計費週期要申請
# 之後才看得到），先用日曆月當預設；之後知道實際週期是哪一天，改這個
# 環境變數對齊即可，不用改程式碼。也完全跟 Claude Code/Claude.ai 的
# 訂閱方案週期無關，那是另一套帳務系統。
_BILLING_CYCLE_START_DAY = int(os.environ.get("USAGE_BILLING_CYCLE_START_DAY", "1"))

# schema（建表/補欄位/建索引）只需要在這個 process 生命週期裡成功跑過
# 一次——這些都是冪等操作，跑第二次結果不會變，但每次都重跑會對
# Postgres（尤其 Neon 這種每次連線都有實質延遲的服務）多花將近 1 秒，
# 白白拖慢每一次查詢。第一次呼叫成功後就把這個旗標打開，之後的呼叫
# 直接跳過整段 schema 檢查。呼叫端都在 _lock 保護下呼叫 _get_conn()，
# 不會有多執行緒同時搶著跑這段初始化的問題。
_schema_ready = False


def _sql(query: str) -> str:
    """SQLite 用 `?` 佔位符寫查詢語法(單一事實來源)，這裡在 Postgres 模式
    下轉成 `%s`，避免每條查詢都要維護兩份幾乎一樣的字串。"""
    return query.replace("?", "%s") if _USE_POSTGRES else query


def _ensure_schema(cur) -> None:
    global _schema_ready
    if _schema_ready:
        return

    if _USE_POSTGRES:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_log (
                id SERIAL PRIMARY KEY,
                timestamp_utc TEXT NOT NULL,
                model TEXT NOT NULL,
                user_email TEXT
            )
            """
        )
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'usage_log'")
        existing_columns = {row[0] for row in cur.fetchall()}
        for column in ("input_tokens", "output_tokens", "file_name"):
            col_type = "TEXT" if column == "file_name" else "INTEGER"
            if column not in existing_columns:
                cur.execute(f"ALTER TABLE usage_log ADD COLUMN {column} {col_type}")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_model_time ON usage_log(model, timestamp_utc)")
    else:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                model TEXT NOT NULL,
                user_email TEXT
            )
            """
        )
        existing_columns = {row[1] for row in cur.execute("PRAGMA table_info(usage_log)")}
        if "input_tokens" not in existing_columns:
            cur.execute("ALTER TABLE usage_log ADD COLUMN input_tokens INTEGER")
        if "output_tokens" not in existing_columns:
            cur.execute("ALTER TABLE usage_log ADD COLUMN output_tokens INTEGER")
        if "file_name" not in existing_columns:
            cur.execute("ALTER TABLE usage_log ADD COLUMN file_name TEXT")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_model_time ON usage_log(model, timestamp_utc)"
        )

    _schema_ready = True


def _get_conn():
    """回傳 (conn, cur)。SQLite 模式下 conn 跟 cur 是同一個物件(sqlite3.Connection
    本身就有 execute());Postgres 模式下是分開的 conn/cursor，呼叫端一律用
    cur.execute(...) 查詢、conn.commit()/conn.close() 管理連線，兩種模式都適用。"""
    if _USE_POSTGRES:
        import psycopg2

        conn = psycopg2.connect(_DATABASE_URL)
        cur = conn.cursor()
        _ensure_schema(cur)
        conn.commit()
        return conn, cur

    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    _ensure_schema(cur)
    return conn, cur


def record_usage(
    model: str,
    user_email: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    file_name: str | None = None,
) -> None:
    with _lock:
        conn, cur = _get_conn()
        try:
            cur.execute(
                _sql(
                    "INSERT INTO usage_log "
                    "(timestamp_utc, model, user_email, input_tokens, output_tokens, file_name) "
                    "VALUES (?, ?, ?, ?, ?, ?)"
                ),
                (
                    datetime.now(_UTC).isoformat(),
                    model,
                    user_email,
                    input_tokens,
                    output_tokens,
                    file_name,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_today_usage(model: str) -> int:
    """回傳「今天」（美西時區）這個 model 被呼叫過幾次。"""
    now_local = datetime.now(_RESET_TZ)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(_UTC).isoformat()
    end_utc = end_local.astimezone(_UTC).isoformat()

    with _lock:
        conn, cur = _get_conn()
        try:
            cur.execute(
                _sql(
                    "SELECT COUNT(*) FROM usage_log "
                    "WHERE model = ? AND timestamp_utc >= ? AND timestamp_utc < ?"
                ),
                (model, start_utc, end_utc),
            )
            return cur.fetchone()[0]
        finally:
            conn.close()


def _clamp_day(year: int, month: int, day: int) -> int:
    last_day = calendar.monthrange(year, month)[1]
    return min(day, last_day)


def _cycle_boundaries(now_local: datetime, start_day: int) -> tuple[datetime, datetime]:
    """算出 now_local 落在哪個計費週期，回傳 (週期起點, 下個週期起點)。
    start_day 若超過當月天數（例如設 31 但該月只有 30 天）會自動夾到月底。"""
    year, month = now_local.year, now_local.month
    day = _clamp_day(year, month, start_day)
    candidate_start = now_local.replace(day=day, hour=0, minute=0, second=0, microsecond=0)

    if now_local < candidate_start:
        # 還沒到這個月的起算日，代表現在還在「上個月起算」的週期裡
        prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
        day = _clamp_day(prev_year, prev_month, start_day)
        start_local = candidate_start.replace(year=prev_year, month=prev_month, day=day)
    else:
        start_local = candidate_start

    next_year, next_month = (
        (start_local.year + 1, 1) if start_local.month == 12 else (start_local.year, start_local.month + 1)
    )
    next_day = _clamp_day(next_year, next_month, start_day)
    end_local = start_local.replace(year=next_year, month=next_month, day=next_day)

    return start_local, end_local


def get_month_token_usage(model: str) -> dict:
    """回傳「本月」（台灣時區，週期起算日可設定）這個 model 累積的
    input/output token 數。"""
    now_local = datetime.now(_TAIWAN_TZ)
    start_local, end_local = _cycle_boundaries(now_local, _BILLING_CYCLE_START_DAY)
    start_utc = start_local.astimezone(_UTC).isoformat()
    end_utc = end_local.astimezone(_UTC).isoformat()

    with _lock:
        conn, cur = _get_conn()
        try:
            cur.execute(
                _sql(
                    "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
                    "FROM usage_log WHERE model = ? AND timestamp_utc >= ? AND timestamp_utc < ?"
                ),
                (model, start_utc, end_utc),
            )
            input_tokens, output_tokens = cur.fetchone()
            return {"input_tokens": input_tokens, "output_tokens": output_tokens}
        finally:
            conn.close()
