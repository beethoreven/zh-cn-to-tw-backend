"""
授權檢查，完全走資料庫（users/permissions 表），不再用 PERMITTED_USER/
ADMIN_ACCOUNT 環境變數白名單。

原因：環境變數改了要重新部署才生效，DB 改了透過管理員介面立刻生效；
而且兩份資料同時存在遲早會兜不起來（這個專案已經因為類似的問題踩過
好幾次坑）。要新增/停用使用者、調整管理員身份，一律透過管理員介面的
「使用者管理」頁籤操作，不需要碰 Render 的環境變數設定。

效能考量：這裡的查詢結果會被短時間快取。前端輪詢 job/review 進度是
每 5 秒打一次 API，每支保護路由都要驗證身份，如果每次都真的去打
一次 Neon（單次連線+查詢實測要 1.2-1.6 秒），整個介面會變得幾乎無法
使用。快取 TTL 設短（預設 30 秒），管理員在後台改了誰的權限，最多
30 秒內生效，對這種個人規模的工具來說這個延遲完全可以接受，換來的是
一般操作不會被這個查詢拖慢。
"""

from __future__ import annotations

import os
import threading
import time

from db_utils.connection import get_ready_conn, sql

_CACHE_TTL_SECONDS = int(os.environ.get("AUTH_CACHE_TTL_SECONDS", "30"))
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict | None]] = {}


def _query_user_auth_row(email: str):
    conn, cur = get_ready_conn()
    try:
        # 用 LOWER() 比對，不要求大小寫完全一致——管理員在後台輸入
        # email 時打的大小寫，不一定跟 Google 這個帳號實際回傳的
        # email claim 完全一樣，大小寫對不上會讓使用者莫名其妙被
        # 擋在授權白名單外面，而且從後台看起來像是有把人加進去了
        cur.execute(
            sql(
                "SELECT users.status, permissions.role FROM users "
                "JOIN permissions ON users.role = permissions.id "
                "WHERE LOWER(users.email) = LOWER(?)"
            ),
            (email,),
        )
        return cur.fetchone()
    finally:
        conn.close()


def get_user_auth_info(email: str | None) -> dict | None:
    """一次查出這個 email 的授權狀態(帶快取)，回傳 None 代表這個 email
    不在 users 表裡；否則回傳 {"active": bool, "is_admin": bool}。"""
    if not email:
        return None

    now = time.time()
    with _cache_lock:
        cached = _cache.get(email)
        if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

    row = _query_user_auth_row(email)
    if row is None:
        result = None
    else:
        status, role = row
        result = {"active": status == "active", "is_admin": role == "admin"}

    with _cache_lock:
        _cache[email] = (now, result)
    return result


def is_permitted_user(email: str | None) -> bool:
    """檢查這個 email 是不是 users 表裡的啟用中(active)使用者。"""
    info = get_user_auth_info(email)
    return info is not None and info["active"]


def is_admin_user(email: str | None) -> bool:
    """檢查這個 email 對應的角色是不是管理員，且帳號本身是啟用中。"""
    info = get_user_auth_info(email)
    return info is not None and info["active"] and info["is_admin"]


def get_user_id(email: str | None) -> int | None:
    """查出這個 email 對應的 users.id，給「本案處理劇本」下拉選單查詢
    自己名下專案用。呼叫頻率低（每次登入後才查一次），不像
    get_user_auth_info 那樣需要 TTL 快取。"""
    if not email:
        return None
    conn, cur = get_ready_conn()
    try:
        cur.execute(sql("SELECT id FROM users WHERE LOWER(email) = LOWER(?)"), (email,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()
