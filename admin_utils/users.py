"""
管理員介面「使用者管理」頁籤的資料存取層。

「有沒有真的改到東西」的判斷放在這裡（不是前端自己比對）：save 之前
先把資料庫目前的值撈出來，跟前端送來的新值逐欄位比對，只有真的有差異
才會真的下 UPDATE，並回傳 changed=True/False，讓前端決定要跳「已儲存」
還是「無任何修改」的 toast。
"""

from __future__ import annotations

from auth_utils import sessions, whitelist
from db_utils.connection import get_ready_conn, sql


def list_users(role_name: str | None = None) -> list[dict]:
    """列出使用者，role_name 給的話只回傳對應角色的（例如 "user"）。"""
    conn, cur = get_ready_conn()
    try:
        if role_name is None:
            cur.execute(
                "SELECT users.id, users.email, users.name, users.role, "
                "permissions.role AS role_name, users.status "
                "FROM users JOIN permissions ON users.role = permissions.id "
                "ORDER BY users.id"
            )
        else:
            cur.execute(
                sql(
                    "SELECT users.id, users.email, users.name, users.role, "
                    "permissions.role AS role_name, users.status "
                    "FROM users JOIN permissions ON users.role = permissions.id "
                    "WHERE permissions.role = ? ORDER BY users.id"
                ),
                (role_name,),
            )
        rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "email": row[1],
                "name": row[2],
                "role": row[3],
                "role_name": row[4],
                "status": row[5],
            }
            for row in rows
        ]
    finally:
        conn.close()


def list_active_user_names() -> list[dict]:
    """給專案管理的「負責人」下拉選單用：只列出 status=active 的使用者。"""
    conn, cur = get_ready_conn()
    try:
        cur.execute(sql("SELECT id, name FROM users WHERE status = ? ORDER BY id"), ("active",))
        return [{"id": row[0], "name": row[1]} for row in cur.fetchall()]
    finally:
        conn.close()


def get_user(user_id: int) -> dict | None:
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql(
                "SELECT users.id, users.email, users.name, users.role, "
                "permissions.role AS role_name, users.status "
                "FROM users JOIN permissions ON users.role = permissions.id "
                "WHERE users.id = ?"
            ),
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "email": row[1],
            "name": row[2],
            "role": row[3],
            "role_name": row[4],
            "status": row[5],
        }
    finally:
        conn.close()


def create_user(email: str, name: str, role: int, status: str) -> dict:
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("INSERT INTO users (email, name, role, status) VALUES (?, ?, ?, ?)"),
            (email, name, role, status),
        )
        conn.commit()
        cur.execute(sql("SELECT id FROM users WHERE email = ?"), (email,))
        new_id = cur.fetchone()[0]
    finally:
        conn.close()
    return get_user(new_id)


def update_user(user_id: int, email: str, name: str, role: int, status: str) -> bool:
    """回傳這次呼叫是否真的改到任何欄位(跟目前 DB 裡的值逐一比對)。"""
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("SELECT email, name, role, status FROM users WHERE id = ?"), (user_id,)
        )
        current = cur.fetchone()
        if current is None:
            raise ValueError(f"找不到 id={user_id} 的使用者")

        changed = current != (email, name, role, status)
        if changed:
            cur.execute(
                sql(
                    "UPDATE users SET email = ?, name = ?, role = ?, status = ? WHERE id = ?"
                ),
                (email, name, role, status, user_id),
            )

            # 帳號從啟用轉成停用時，主動把它名下所有 session 撤銷掉。
            #
            # 不做這件事的話，停用要等授權快取的 TTL（預設 30 秒）自然
            # 過期才生效。30 秒聽起來很短，但那正好是「管理員發現有人
            # 在亂搞、緊急停用」的情境，而在那 30 秒內對方還能啟動一個
            # 跑好幾分鐘、燒 LLM 額度的工作——job 一旦啟動就跑到完，
            # 執行期間完全不再檢查授權（見 pipeline/orchestrator.py）。
            # 所以真正的成本不是「多看 30 秒畫面」，是「還能再燒一次錢」。
            #
            # 兩件事一定要一起做，缺一個就無效：
            #   1. 刪 session —— 把人踢下線
            #   2. 清授權快取 —— 否則他可以立刻重新登入，/auth/login 走
            #      的是同一支有快取的 is_permitted_user()，命中時仍然回
            #      「active」，於是又拿到一個新 session，繞回原點
            #
            # 刪除跟上面那句 UPDATE 共用同一個 cursor、同一個交易，不會
            # 出現「已停用但 session 還在」的中間狀態。
            #
            # 只在「active -> deactive」這個轉換上做，不是「只要 status
            # 是 deactive 就做」：後者會讓管理員每次編輯一個早就停用的
            # 帳號（改個名字之類）都重跑一次無意義的 DELETE。也不對所有
            # update_user 做——改名字或調角色不該把人踢下線。
            old_status = current[3]
            if old_status == "active" and status == "deactive":
                revoked = sessions.delete_sessions_for_email(cur, email)
                # 舊 email 跟新 email 都要清：這次呼叫如果同時改了 email，
                # 快取是用 email 當鍵的，只清一個會漏掉另一個。
                whitelist.invalidate_cache(email)
                whitelist.invalidate_cache(current[0])
                if revoked:
                    print(f"[admin] 停用 {email}，已撤銷 {revoked} 個 session", flush=True)

            conn.commit()
        return changed
    finally:
        conn.close()
