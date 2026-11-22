"""
授權檢查，完全走資料庫（users/permissions 表），不再用 PERMITTED_USER/
ADMIN_ACCOUNT 環境變數白名單。

原因：環境變數改了要重新部署才生效，DB 改了透過管理員介面立刻生效；
而且兩份資料同時存在遲早會兜不起來（這個專案已經因為類似的問題踩過
好幾次坑）。要新增/停用使用者、調整管理員身份，一律透過管理員介面的
「使用者管理」頁籤操作，不需要碰 Render 的環境變數設定。

效能考量：這裡的查詢結果會被短時間快取。前端輪詢 job/review 進度是
每 5 秒打一次 API，每支保護路由都要驗證身份，如果每次都真的去打
一次 Neon，整個介面會變得幾乎無法使用。快取 TTL 設短（預設 30 秒），
管理員在後台改了誰的權限，最多 30 秒內生效，對這種個人規模的工具來說
這個延遲完全可以接受，換來的是一般操作不會被這個查詢拖慢。

註：這段快取當初的量化理由是「單次連線+查詢實測 1.2-1.6 秒」。那個
數字在 2026-08-25 之後已經不成立——它有 99% 來自 Render(新加坡) 與
Neon(俄亥俄) 跨區部署，資料庫搬到新加坡同區之後實測降到約 30ms（見
db_utils/connection.py 的歷史教訓四）。快取本身沒有被拿掉，因為它
除了省延遲之外還在做別的事（減少 Neon 免費方案的連線數與 CU 用量、
避免每個請求都佔用連線名額）——但「這個機制現在還需不需要、TTL 該
不該調」沒有重新評估過，要動它之前請先量一次現況，不要再引用那個
已經過期的數字當理由。
"""

from __future__ import annotations

import os
import threading
import time

from db_utils.connection import get_ready_conn, sql
from db_utils.schema import ADMIN_ROLE_ID

_CACHE_TTL_SECONDS = int(os.environ.get("AUTH_CACHE_TTL_SECONDS", "30"))
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict | None]] = {}
# invalidate_cache 每呼叫一次就加一。查資料庫之前先記下當時的值，查完要
# 寫回快取時如果已經變了，代表查詢期間有人清過快取（例如管理員剛停用
# 帳號），這次查到的可能是改動之前的資料，不能寫回去
_generation = 0


def normalize_email(email: str) -> str:
    """email 一律去掉前後空白、轉小寫再存或比對。

    Google 帳號的 email 不分大小寫，但管理員在後台輸入時打的大小寫、Google
    回傳的 email claim 不保證一致。只要有一個地方用原樣比對，就會出現
    「後台看起來有加進去，卻登入不了」或「停用了卻沒撤銷到 session」這種
    錯。users 表從這一版起一律存小寫，唯一限制也改成不分大小寫（見
    db_utils/schema.py 的 _ensure_users）。"""
    return email.strip().lower()


def _query_user_auth_row(email: str):
    conn, cur = get_ready_conn()
    try:
        # 仍然用 LOWER() 比對：小寫化之前就存在的資料，在遷移完成前可能
        # 還有大寫（見 db_utils/schema.py 的 _ensure_users）
        cur.execute(
            sql("SELECT status, role FROM users WHERE LOWER(email) = LOWER(?)"),
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
    # 快取的鍵也要小寫：停用帳號時 invalidate_cache 拿的是後台存的 email，
    # 登入時查的是 Google 給的 email，大小寫不同就清不到這一筆
    email = normalize_email(email)

    now = time.time()
    with _cache_lock:
        cached = _cache.get(email)
        if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        generation = _generation

    row = _query_user_auth_row(email)
    if row is None:
        result = None
    else:
        status, role_id = row
        # 看固定的角色 id，不看角色名稱：名稱可以在管理介面的「權限管理」
        # 改，把任何一個角色改名成 admin，就會讓那個角色的所有人都變成
        # 管理員
        result = {"active": status == "active", "is_admin": role_id == ADMIN_ROLE_ID}

    with _cache_lock:
        # 查詢期間快取被清過：結果照樣回給這一個請求（它在改動之前就開始
        # 了），但不寫回快取，否則停用要等 TTL 過期才生效
        if _generation == generation:
            _cache[email] = (now, result)
    return result


def invalidate_cache(email: str | None) -> None:
    """把這個 email 的授權快取立刻失效，不等 TTL 自然過期。

    管理員停用帳號時一定要呼叫，否則「刪掉 session」這個動作是無效的：
    使用者被登出後可以立刻重新登入，而 /auth/login 走的是同一支
    is_permitted_user()，快取命中時仍然回傳「active」，於是又拿到一個
    全新的 session——繞了一圈，停用還是要等 TTL 到期才真的生效。

    這個快取是行程內的。目前 Render 是單一 process（Start Command 是
    `python3 app.py`，Flask 內建伺服器 threaded=True），所以清一次就
    夠了；哪天改成多 worker（gunicorn 之類），每個 process 各有一份
    快取，這裡就只清得掉自己那一份，屆時要改成走共用儲存或縮短 TTL。

    一定要在資料庫的改動 commit 之後才呼叫：先清的話，commit 之前進來的
    請求會查到改動前的資料、重新寫回快取。正在查詢中、結果還沒寫回的
    請求，由 get_user_auth_info 的 _generation 比對擋下。
    """
    global _generation
    if not email:
        return
    with _cache_lock:
        _generation += 1
        _cache.pop(normalize_email(email), None)


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
