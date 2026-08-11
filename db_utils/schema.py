"""
全站資料庫 schema 建立/遷移，集中在這一支檔案，不要分散在各個模組裡。

建立順序有相依性，不能隨便調換：
1. permissions（角色 → 額度對照表）——users.role 會參照這裡的 id
2. users（帳號）——projects.owner 會參照這裡的 id
3. projects
4. usage_log（用量記錄表）
5. sessions（應用程式自己簽發的登入 session）

只支援 Postgres（Neon），不再有 SQLite 模式——見 db_utils/connection.py。

跟 usage_log 原本的做法一樣：這些都是冪等操作（CREATE TABLE IF NOT
EXISTS、補欄位前先檢查），但只需要在這個 process 生命週期裡成功跑過
一次，跑第二次結果不會變，卻會對 Postgres（尤其 Neon 這種每次連線都
有實質延遲的服務）多花時間。第一次呼叫成功後就把旗標打開，之後的
呼叫直接跳過整段檢查。呼叫端要在 db_utils.connection.LOCK 保護下呼叫
ensure_schema()，避免多執行緒同時搶著跑初始化。
"""

from __future__ import annotations


_schema_ready = False

# 預設的角色/額度種子資料——id=1 是管理員（給很高的上限，等同不限制），
# id=2 是一般使用者（保守的預設值）。這只是「有資料可以動」的起始值，
# 之後要用權限管理頁籤隨時調整即可。
#
# allowed_opus 欄位已於 2026-08-10 移除（claude-opus-5 不再提供選用，
# 見 configs/config.py 的說明），這裡的種子資料跟著只剩 allowed_haiku。
_DEFAULT_PERMISSIONS = [
    (1, "admin", 999999),
    (2, "user", 10),
]


def _existing_columns(cur, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
    )
    return {row[0] for row in cur.fetchall()}


def ensure_schema(cur) -> None:
    global _schema_ready
    if _schema_ready:
        return

    _ensure_permissions(cur)
    _ensure_users(cur)
    _ensure_projects(cur)
    _ensure_usage_log(cur)
    _ensure_sessions(cur)

    _schema_ready = True


def _ensure_permissions(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS permissions (
            id SERIAL PRIMARY KEY,
            role TEXT NOT NULL,
            allowed_haiku INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # allowed_opus 已經不再使用（claude-opus-5 從 config.ALL_MODELS 移除，
    # 見那邊的說明）——這支 process 第一次啟動時把既有欄位刪掉，
    # IF EXISTS 讓這個操作對「本來就沒有這個欄位」的全新資料庫是無害的
    # 空操作，可以放心每次啟動都跑。
    cur.execute("ALTER TABLE permissions DROP COLUMN IF EXISTS allowed_opus")
    cur.execute("SELECT COUNT(*) FROM permissions")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO permissions (id, role, allowed_haiku) VALUES (%s, %s, %s)",
            _DEFAULT_PERMISSIONS,
        )
        # Postgres 的 SERIAL 序列不會因為手動指定 id 自動往前推進，下一次
        # 真的用 INSERT INTO permissions (role, ...) 不帶 id 時可能撞到
        # id=1/2 已經存在——重設序列起點，確保之後自動產生的 id 從 3 開始
        cur.execute("SELECT setval('permissions_id_seq', (SELECT MAX(id) FROM permissions))")


def _ensure_users(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            name TEXT,
            role INTEGER NOT NULL REFERENCES permissions(id),
            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )


def _ensure_projects(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            owner INTEGER REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )


def _ensure_usage_log(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_log (
            id SERIAL PRIMARY KEY,
            timestamp_utc TEXT NOT NULL,
            model TEXT NOT NULL,
            user_email TEXT
        )
        """
    )
    existing_columns = _existing_columns(cur, "usage_log")
    for column, col_type in (
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("file_name", "TEXT"),
        ("project", "INTEGER"),
    ):
        if column not in existing_columns:
            cur.execute(f"ALTER TABLE usage_log ADD COLUMN {column} {col_type}")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_model_time ON usage_log(model, timestamp_utc)")


def _ensure_sessions(cur) -> None:
    # 應用程式自己簽發的登入 session（跟 Google ID Token 脫鉤，效期由
    # auth_utils/sessions.py 的滑動視窗機制管理，不是 Google 那邊固定的
    # 1 小時）——token 是隨機不透明字串，不是 JWT，這裡只需要能用它反查
    # email。
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen_at)")
