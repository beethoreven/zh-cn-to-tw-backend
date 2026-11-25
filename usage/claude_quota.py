"""
每人每月 Claude Haiku 使用次數上限（permissions.allowed_haiku）。

「一次」= 用 Claude 開始一份工作：Stage 1 的前置處理、Stage 2 的分析報告、
Stage 3 的校對與重新校對，各算一次，不管那份工作之後分幾批呼叫 API。
用 Gemini 不算；不會呼叫 LLM 的請求（前置處理關掉、沒有字詞修改的分析）
也不算。

上限是角色的設定（管理員介面「權限管理」的「Haiku 使用上限」），每個人
各自計數，以台灣時間的月份為一期，每月 1 日重新計算。

一定要在開始呼叫 Claude 之前扣：用量是先扣再做，扣不到就整份不開始，不會
做到一半才被擋、也不會先花了錢才拒絕。扣了之後工作中途失敗也不退回——
那一次確實開始了，也可能已經花了錢。

扣次數是單一一句 INSERT ... ON CONFLICT DO UPDATE ... WHERE used < 上限：
資料庫在同一列上排隊執行，同時送出好幾份工作也不會一起看到「還剩一次」
然後全部通過（先查再寫會有這個空隙）。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from db_utils.connection import get_ready_conn, sql

_TAIPEI = ZoneInfo("Asia/Taipei")


class ClaudeQuotaExceeded(Exception):
    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(
            f"本月的 Claude Haiku 使用次數已達上限（{limit} 次），請改用 Gemini，或請管理員調整上限"
        )


def is_claude_model(model: str | None) -> bool:
    # 目前唯一的 Claude 模型是 Haiku（見 configs/config.py 的 ALL_MODELS）。之後
    # 加了別的 Claude 模型，要先決定它算不算進這個上限、還是另外一個欄位。
    return bool(model) and model.startswith("claude")


def current_month() -> str:
    return datetime.now(_TAIPEI).strftime("%Y-%m")


def consume(email: str, model: str | None) -> None:
    """不是 Claude 就什麼都不做。是 Claude 就扣一次，超過上限拋
    ClaudeQuotaExceeded（呼叫端回錯誤、不要開始工作）。"""
    if not is_claude_model(model):
        return
    month = current_month()
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql(
                "SELECT permissions.allowed_haiku FROM users "
                "JOIN permissions ON users.role = permissions.id "
                "WHERE LOWER(users.email) = LOWER(?)"
            ),
            (email,),
        )
        row = cur.fetchone()
        limit = row[0] if row else 0
        if limit <= 0:
            raise ClaudeQuotaExceeded(max(limit, 0))
        cur.execute(
            sql(
                "INSERT INTO claude_quota_usage (user_email, month, used) VALUES (LOWER(?), ?, 1) "
                "ON CONFLICT (user_email, month) DO UPDATE SET used = claude_quota_usage.used + 1 "
                "WHERE claude_quota_usage.used < ? RETURNING used"
            ),
            (email, month, limit),
        )
        granted = cur.fetchone() is not None
        conn.commit()
    finally:
        conn.close()
    if not granted:
        raise ClaudeQuotaExceeded(limit)


def used_this_month(email: str) -> int:
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("SELECT used FROM claude_quota_usage WHERE user_email = LOWER(?) AND month = ?"),
            (email, current_month()),
        )
        row = cur.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()
