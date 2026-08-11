"""
管理員介面「使用者管理」頁籤的資料存取層。

「有沒有真的改到東西」的判斷放在這裡（不是前端自己比對）：save 之前
先把資料庫目前的值撈出來，跟前端送來的新值逐欄位比對，只有真的有差異
才會真的下 UPDATE，並回傳 changed=True/False，讓前端決定要跳「已儲存」
還是「無任何修改」的 toast。
"""

from __future__ import annotations

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
        cur.execute(sql("SELECT id, name FROM users WHERE status = ? ORDER BY name"), ("active",))
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
            conn.commit()
        return changed
    finally:
        conn.close()
