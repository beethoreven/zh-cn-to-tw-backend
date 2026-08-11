"""
Gemini/Claude 呼叫使用量記錄。

資料存在 Neon（免費、永久不過期的 Postgres），本機開發跟桌面版 App 都是
連同一個資料庫，只有這一種資料來源——見 db_utils/connection.py。

用「事件日誌」設計（每次呼叫一筆紀錄，含時間戳），不用「每日累加」的
彙總表，原因：
1. 資料量對這個工具的實際用量來說微不足道，累積多年也不會有效能問題。
2. 保留完整時間戳，之後不管想用哪個時區、哪種統計粒度回頭分析都可以，
   不會被彙總表當初選定的時區/粒度綁死。
3. Stage 3 加上登入後，只要用既有的 user_email 欄位就能做到「每人分開
   統計」，不用改表結構、不用做資料遷移。

「今天」（Gemini 用）採用官方文件講的額度重置時區——美西時間
（America/Los_Angeles）。Claude 沒有 Gemini 那種免費 RPD，這裡記的是
token 數/估計費用，統計範圍是「整個專案累積至今」或「全站累積至今」，
不分時間週期，都用 zoneinfo 處理「今天」這部分，不必自己手動換算日光節約。

注意：這裡記的是「這個工具打了幾次/用了多少 token」，跟 Google/Anthropic
官方帳務是兩回事——如果同一把 API key 也在別處使用，這裡的數字會跟
官方帳單對不上，只能當作本工具自己的用量參考。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db_utils.connection import LOCK, get_ready_conn, sql

_RESET_TZ = ZoneInfo("America/Los_Angeles")
_UTC = ZoneInfo("UTC")


def _sql(query: str) -> str:
    return sql(query)


def _get_conn():
    return get_ready_conn()


def record_usage(
    model: str,
    user_email: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    file_name: str | None = None,
    project: int | None = None,
) -> None:
    with LOCK:
        conn, cur = _get_conn()
        try:
            cur.execute(
                _sql(
                    "INSERT INTO usage_log "
                    "(timestamp_utc, model, user_email, input_tokens, output_tokens, file_name, project) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    datetime.now(_UTC).isoformat(),
                    model,
                    user_email,
                    input_tokens,
                    output_tokens,
                    file_name,
                    project,
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

    with LOCK:
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


def get_today_project_model_count(project_id: int, model: str) -> int:
    """回傳「今天」（美西時區，跟 get_today_usage 同一套時區規則）某個
    專案底下，這個 model 被呼叫過幾次——給「個人專案」的 Gemini 3.6
    Flash 每日額度限制用，跟 get_today_usage 的差別只在多加了 project
    這個過濾條件。"""
    now_local = datetime.now(_RESET_TZ)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(_UTC).isoformat()
    end_utc = end_local.astimezone(_UTC).isoformat()

    with LOCK:
        conn, cur = _get_conn()
        try:
            cur.execute(
                _sql(
                    "SELECT COUNT(*) FROM usage_log "
                    "WHERE model = ? AND project = ? AND timestamp_utc >= ? AND timestamp_utc < ?"
                ),
                (model, project_id, start_utc, end_utc),
            )
            return cur.fetchone()[0]
        finally:
            conn.close()


def get_project_token_usage(project_id: int, model: str) -> dict:
    """回傳某個劇本案（project id）累積至今、這個 model 的 input/output
    token 數，不分時間範圍——給主介面「本案使用 Claude token 狀況」用。"""
    with LOCK:
        conn, cur = _get_conn()
        try:
            cur.execute(
                _sql(
                    "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
                    "FROM usage_log WHERE model = ? AND project = ?"
                ),
                (model, project_id),
            )
            input_tokens, output_tokens = cur.fetchone()
            return {"input_tokens": input_tokens, "output_tokens": output_tokens}
        finally:
            conn.close()


def get_total_token_usage(model: str) -> dict:
    """回傳這個 model 從有紀錄以來累積的 input/output token 數，不分時間、
    不分專案——給管理員介面的總使用量用。"""
    with LOCK:
        conn, cur = _get_conn()
        try:
            cur.execute(
                _sql(
                    "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
                    "FROM usage_log WHERE model = ?"
                ),
                (model,),
            )
            input_tokens, output_tokens = cur.fetchone()
            return {"input_tokens": input_tokens, "output_tokens": output_tokens}
        finally:
            conn.close()
