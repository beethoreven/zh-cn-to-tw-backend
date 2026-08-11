"""
應用程式自己的 session token 層——跟 Google ID Token 完全脫鉤。

背景：Google ID Token 效期固定約 1 小時。早期實作直接把它當成前端打
每一支 API 都要帶的長效憑證，等於整個工作階段每小時就會被打斷一次，
逼使用者重新登入，長時間的 Stage 1/校對（尤其遇到額度限制、重試+
指數退避時）常常一小時內跑不完，導致算好的結果因為登入過期而存不下來
——市面上正常「用 Google 登入」的產品不是這樣做的：Google 只在登入
當下驗證一次身份，之後靠應用程式自己簽發、自己管理生命週期的 session
才是真正撐住整個使用期間的憑證，這裡補上這一層。

設計：
- token 是隨機不透明字串（secrets.token_hex，不是 JWT），存進 sessions
  表，用 token 反查 email；前端不需要、也不應該嘗試解析它。
- 「滑動式」有效期：每次成功驗證都會把 last_seen_at 更新成現在，只要
  使用者還有在用（不管多久用一次），session 就不會過期；連續
  SESSION_IDLE_DAYS 天完全沒有任何請求才會真的過期——這是合理的
  「太久沒用」邊界，不是工作到一半被打斷。
- 撤銷（管理員停用帳號）完全不靠這一層：auth_utils/whitelist.py 的
  is_permitted_user() 在每支 require_auth 保護的路由都會即時查（30 秒
  快取），跟 session token 本身是否還「有效」是兩件事——session 有效
  只代表「這確實是我們自己發出去、還沒過期的憑證」，不代表這個帳號
  現在還有權限。兩層各司其職，管理員後台停用帳號一樣是 30 秒內生效，
  不會因為多了這一層而變慢或被繞過。
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime, timezone

from db_utils.connection import get_ready_conn, sql

SESSION_IDLE_DAYS = int(os.environ.get("SESSION_IDLE_DAYS", "90"))

# last_seen_at 沒必要每次請求都真的寫一次 DB——前端輪詢 job/review 進度
# 每 1.5 秒打一次 API，Neon 單次連線+查詢實測要 1.2-1.6 秒，不節流的話
# 光是「順便更新一下時間戳記」就會把輪詢拖垮。跟 whitelist.py 的讀取
# 快取是同樣的考量，這裡反過來對寫入做節流。
_TOUCH_MIN_INTERVAL_SECONDS = 60
_touch_lock = threading.Lock()
_last_touched: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(email: str) -> str:
    """驗證過 Google ID Token、確認過白名單之後才呼叫——這裡不重複做
    這兩件事，呼叫端（/auth/login）負責。"""
    token = secrets.token_hex(32)
    now = _now_iso()
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql(
                "INSERT INTO sessions (token, email, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?)"
            ),
            (token, email, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def resolve_session(token: str | None) -> str | None:
    """token 有效（存在且未超過滑動視窗）就回傳對應的 email，並視情況
    更新 last_seen_at；無效（不存在或已逾期）回傳 None，逾期的話順便
    刪掉這筆記錄。"""
    if not token:
        return None

    conn, cur = get_ready_conn()
    try:
        cur.execute(sql("SELECT email, last_seen_at FROM sessions WHERE token = ?"), (token,))
        row = cur.fetchone()
        if row is None:
            return None

        email, last_seen_at = row
        last_seen = datetime.fromisoformat(last_seen_at)
        idle_days = (datetime.now(timezone.utc) - last_seen).total_seconds() / 86400
        if idle_days > SESSION_IDLE_DAYS:
            cur.execute(sql("DELETE FROM sessions WHERE token = ?"), (token,))
            conn.commit()
            return None

        _maybe_touch(cur, conn, token)
        return email
    finally:
        conn.close()


def _maybe_touch(cur, conn, token: str) -> None:
    now = time.time()
    with _touch_lock:
        last = _last_touched.get(token, 0)
        if now - last < _TOUCH_MIN_INTERVAL_SECONDS:
            return
        _last_touched[token] = now
    cur.execute(sql("UPDATE sessions SET last_seen_at = ? WHERE token = ?"), (_now_iso(), token))
    conn.commit()


def delete_session(token: str | None) -> None:
    if not token:
        return
    conn, cur = get_ready_conn()
    try:
        cur.execute(sql("DELETE FROM sessions WHERE token = ?"), (token,))
        conn.commit()
    finally:
        conn.close()
    with _touch_lock:
        _last_touched.pop(token, None)
