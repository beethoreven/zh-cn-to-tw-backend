"""管理員介面「專案管理」頁籤的資料存取層。"""

from __future__ import annotations

from configs import config
from db_utils.connection import get_ready_conn, sql

VALID_STATUSES = ("pending", "ongoing", "delayed", "done_not_paid", "paid", "closed")


def list_projects() -> list[dict]:
    """給「選擇專案」下拉選單用（管理員介面-專案管理）。不包含所有人
    共用的「個人專案」（config.PERSONAL_PROJECT_ID）——那個不透過這個
    頁籤管理，也不該出現在這裡。刻意用固定 id 判斷，不是 owner IS
    NULL——owner IS NULL 理論上還可能對到其他情況，見
    config.PERSONAL_PROJECT_ID 的說明。"""
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("SELECT id, name FROM projects WHERE id != ? ORDER BY id"),
            (config.PERSONAL_PROJECT_ID,),
        )
        return [{"id": row[0], "name": row[1]} for row in cur.fetchall()]
    finally:
        conn.close()


def list_projects_by_owner(owner_id: int) -> list[dict]:
    """列出某個使用者可以選的專案，給主介面「本案處理劇本」下拉選單用：
    自己名下的專案，加上所有人都能選的共用「個人專案」
    （config.PERSONAL_PROJECT_ID）。回傳的 owner 欄位是 None 就代表是
    這種共用專案——前端要用這個判斷要不要套用「個人專案」的 model／
    額度限制（前端目前用 owner === null 判斷，跟後端這裡用固定 id
    判斷不完全是同一套邏輯，只是剛好對得上，因為個人專案目前是唯一
    owner 為 NULL 的專案）。"""
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("SELECT id, name, owner FROM projects WHERE owner = ? OR id = ? ORDER BY id"),
            (owner_id, config.PERSONAL_PROJECT_ID),
        )
        return [{"id": row[0], "name": row[1], "owner": row[2]} for row in cur.fetchall()]
    finally:
        conn.close()


def list_owner_projects_excluding_closed(owner_id: int) -> list[dict]:
    """列出某個使用者名下、狀態不是 closed 的專案（含狀態），給使用者
    管理頁籤讀取某個使用者之後，順便顯示他手上專案清單用。"""
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("SELECT id, name, status FROM projects WHERE owner = ? AND status != ? ORDER BY id"),
            (owner_id, "closed"),
        )
        return [{"id": row[0], "name": row[1], "status": row[2]} for row in cur.fetchall()]
    finally:
        conn.close()


def get_project(project_id: int) -> dict | None:
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
