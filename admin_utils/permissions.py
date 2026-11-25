"""管理員介面「權限管理」頁籤的資料存取層。"""

from __future__ import annotations

from admin_utils import audit_log
from db_utils.connection import get_ready_conn, sql
from db_utils.schema import ADMIN_ROLE_ID


def list_permissions(exclude_admin: bool = True) -> list[dict]:
    """列出角色/額度。exclude_admin=True 時排除管理員（ADMIN_ROLE_ID），給
    「選擇權限」下拉選單用——管理員這個角色的額度不透過這個頁籤調整。"""
    conn, cur = get_ready_conn()
    try:
        if exclude_admin:
            cur.execute(
                sql("SELECT id, role, allowed_haiku FROM permissions WHERE id != ? ORDER BY id"),
                (ADMIN_ROLE_ID,),
            )
        else:
            cur.execute("SELECT id, role, allowed_haiku FROM permissions ORDER BY id")
        return [
            {"id": row[0], "role": row[1], "allowed_haiku": row[2]}
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def get_permission(permission_id: int) -> dict | None:
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("SELECT id, role, allowed_haiku FROM permissions WHERE id = ?"),
            (permission_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"id": row[0], "role": row[1], "allowed_haiku": row[2]}
    finally:
        conn.close()


def update_permission(permission_id: int, role: str, allowed_haiku: int, *, actor_email: str) -> bool:
    """回傳這次呼叫是否真的改到任何欄位。有改到的話，跟 UPDATE 同一個交易
    記進管理操作紀錄；actor_email 是執行這個操作的管理員。"""
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("SELECT role, allowed_haiku FROM permissions WHERE id = ?"),
            (permission_id,),
        )
        current = cur.fetchone()
        if current is None:
            raise ValueError(f"找不到 id={permission_id} 的權限")

        changed = current != (role, allowed_haiku)
        if changed:
            cur.execute(
                sql("UPDATE permissions SET role = ?, allowed_haiku = ? WHERE id = ?"),
                (role, allowed_haiku, permission_id),
            )
            audit_log.record(cur, actor_email, "permission.update", permission_id, audit_log.RESULT_SUCCESS)
            conn.commit()
        return changed
    finally:
        conn.close()
