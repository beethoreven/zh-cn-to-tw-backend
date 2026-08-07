"""管理員介面「專案管理」頁籤的資料存取層。"""

from __future__ import annotations

from db_utils.connection import LOCK, get_ready_conn, sql

VALID_STATUSES = ("pending", "ongoing", "delayed", "done_not_paid", "paid", "closed")


def list_projects() -> list[dict]:
    """給「選擇專案」下拉選單用。"""
    with LOCK:
        conn, cur = get_ready_conn()
        try:
            cur.execute("SELECT id, name FROM projects ORDER BY id")
            return [{"id": row[0], "name": row[1]} for row in cur.fetchall()]
        finally:
            conn.close()


def list_projects_by_owner(owner_id: int) -> list[dict]:
    """列出某個使用者名下的專案，給主介面「本案處理劇本」下拉選單用
    （不限管理員——一般使用者查詢自己負責的專案）。"""
    with LOCK:
        conn, cur = get_ready_conn()
        try:
            cur.execute(sql("SELECT id, name FROM projects WHERE owner = ? ORDER BY id"), (owner_id,))
            return [{"id": row[0], "name": row[1]} for row in cur.fetchall()]
        finally:
            conn.close()


def get_project(project_id: int) -> dict | None:
    with LOCK:
        conn, cur = get_ready_conn()
        try:
            cur.execute(
                sql(
                    "SELECT projects.id, projects.name, projects.owner, users.name, "
                    "projects.status FROM projects LEFT JOIN users ON projects.owner = users.id "
                    "WHERE projects.id = ?"
                ),
                (project_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "id": row[0],
                "name": row[1],
                "owner": row[2],
                "owner_name": row[3],
                "status": row[4],
            }
        finally:
            conn.close()


def create_project(name: str, owner: int, status: str) -> dict:
    with LOCK:
        conn, cur = get_ready_conn()
        try:
            cur.execute(
                sql("INSERT INTO projects (name, owner, status) VALUES (?, ?, ?)"),
                (name, owner, status),
            )
            conn.commit()
            # 專案名稱沒有唯一性限制，不能像 users.email 那樣拿來反查剛
            # 建立的那筆——改用「這個 process 剛剛插入的那一列」來查
            if hasattr(cur, "lastrowid") and cur.lastrowid:
                new_id = cur.lastrowid
            else:
                cur.execute("SELECT lastval()")
                new_id = cur.fetchone()[0]
        finally:
            conn.close()
    return get_project(new_id)


def update_project(project_id: int, name: str, owner: int, status: str) -> bool:
    """回傳這次呼叫是否真的改到任何欄位。"""
    with LOCK:
        conn, cur = get_ready_conn()
        try:
            cur.execute(
                sql("SELECT name, owner, status FROM projects WHERE id = ?"), (project_id,)
            )
            current = cur.fetchone()
            if current is None:
                raise ValueError(f"找不到 id={project_id} 的專案")

            changed = current != (name, owner, status)
            if changed:
                cur.execute(
                    sql("UPDATE projects SET name = ?, owner = ?, status = ? WHERE id = ?"),
                    (name, owner, status, project_id),
                )
                conn.commit()
            return changed
        finally:
            conn.close()
