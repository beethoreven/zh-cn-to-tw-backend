"""管理員介面「專案管理」頁籤的資料存取層。"""

from __future__ import annotations

from configs import config
from db_utils.connection import get_ready_conn, sql

VALID_STATUSES = ("pending", "ongoing", "delayed", "done_not_paid", "paid", "closed")


def list_projects() -> list[dict]:
    """給「選擇專案」下拉選單用（管理員介面-專案管理）。不包含所有人
    共用的「個人專案」（config.PERSONAL_PROJECT_ID）——那個不透過這個
    頁籤管理，也不該出現在這裡。刻意用固定 id 判斷，不是看負責人欄位
    是不是 NULL——那理論上還可能對到其他情況，見
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
    自己是負責人之一（owner_1/owner_2/owner_3 任何一格填的是他）的專案，
    加上所有人都能選的共用「個人專案」（config.PERSONAL_PROJECT_ID）。
    前端只用 id/name；是不是個人專案，前後端都是用固定 id 判斷，不看
    負責人欄位。"""
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql(
                "SELECT id, name FROM projects "
                "WHERE owner_1 = ? OR owner_2 = ? OR owner_3 = ? OR id = ? ORDER BY id"
            ),
            (owner_id, owner_id, owner_id, config.PERSONAL_PROJECT_ID),
        )
        return [{"id": row[0], "name": row[1]} for row in cur.fetchall()]
    finally:
        conn.close()


def list_owner_projects_excluding_closed(owner_id: int) -> list[dict]:
    """列出某個使用者名下（三個負責人欄位任何一格是他）、狀態不是 closed
    的專案（含狀態），給使用者管理頁籤讀取某個使用者之後，順便顯示他
    手上專案清單用。"""
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql(
                "SELECT id, name, status FROM projects "
                "WHERE (owner_1 = ? OR owner_2 = ? OR owner_3 = ?) AND status != ? ORDER BY id"
            ),
            (owner_id, owner_id, owner_id, "closed"),
        )
        return [{"id": row[0], "name": row[1], "status": row[2]} for row in cur.fetchall()]
    finally:
        conn.close()


def get_project(project_id: int) -> dict | None:
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql(
                "SELECT p.id, p.name, p.status, "
                "p.owner_1, u1.name, p.owner_2, u2.name, p.owner_3, u3.name "
                "FROM projects p "
                "LEFT JOIN users u1 ON p.owner_1 = u1.id "
                "LEFT JOIN users u2 ON p.owner_2 = u2.id "
                "LEFT JOIN users u3 ON p.owner_3 = u3.id "
                "WHERE p.id = ?"
            ),
            (project_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "status": row[2],
            "owner_1": row[3],
            "owner_1_name": row[4],
            "owner_2": row[5],
            "owner_2_name": row[6],
            "owner_3": row[7],
            "owner_3_name": row[8],
        }
    finally:
        conn.close()


def is_project_owner(project: dict, user_id: int | None) -> bool:
    """user_id 是不是這個專案的負責人之一。user_id 是 None（查不到這個
    使用者）一律不算——沒填的負責人欄位也是 None，直接用 in 比對會把
    「查不到的使用者」誤判成「空著的那一格」的負責人。"""
    if user_id is None:
        return False
    return user_id in (project["owner_1"], project["owner_2"], project["owner_3"])


def create_project(name: str, owners: tuple[int | None, int | None, int | None], status: str) -> dict:
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql(
                "INSERT INTO projects (name, owner_1, owner_2, owner_3, status) "
                "VALUES (?, ?, ?, ?, ?)"
            ),
            (name, *owners, status),
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


def update_project(
    project_id: int, name: str, owners: tuple[int | None, int | None, int | None], status: str
) -> bool:
    """回傳這次呼叫是否真的改到任何欄位。"""
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("SELECT name, owner_1, owner_2, owner_3, status FROM projects WHERE id = ?"),
            (project_id,),
        )
        current = cur.fetchone()
        if current is None:
            raise ValueError(f"找不到 id={project_id} 的專案")

        changed = tuple(current) != (name, *owners, status)
        if changed:
            cur.execute(
                sql(
                    "UPDATE projects SET name = ?, owner_1 = ?, owner_2 = ?, owner_3 = ?, "
                    "status = ? WHERE id = ?"
                ),
                (name, *owners, status, project_id),
            )
            conn.commit()
        return changed
    finally:
        conn.close()
