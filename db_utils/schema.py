"""
全站資料庫 schema 建立/遷移，集中在這一支檔案，不要分散在各個模組裡。

建立順序有相依性，不能隨便調換：
1. permissions（角色 → 額度對照表）——users.role 會參照這裡的 id
2. users（帳號）——projects.owner 會參照這裡的 id
3. projects
4. usage_log（用量記錄表）
5. sessions（應用程式自己簽發的登入 session）
6. jobs（Stage 1/2 工作狀態）
7. app_versions（桌面版 App 強制更新門檻）——沒有 FK 依賴，順序其實
   不重要，放最後只是照加入的時間排

只支援 Postgres（Neon），不再有 SQLite 模式——見 db_utils/connection.py。

跟 usage_log 原本的做法一樣：這些都是冪等操作（CREATE TABLE IF NOT
EXISTS、補欄位前先檢查），但只需要在這個 process 生命週期裡成功跑過
一次，跑第二次結果不會變，卻會對 Postgres（尤其 Neon 這種每次連線都
有實質延遲的服務）多花時間。第一次呼叫成功後就把旗標打開，之後的
呼叫直接跳過整段檢查。

補欄位那幾處是「先查 information_schema 確認欄位不存在、再 ALTER TABLE
ADD COLUMN」，中間沒有 IF NOT EXISTS 保護，兩個請求在冷啟動時第一次
同時呼叫 ensure_schema() 會真的搶著跑、可能撞出「欄位已存在」的錯誤
（不是理論上的風險，是這幾行程式碼本身的寫法就有這個 TOCTOU 空隙）。
用 _schema_lock 保護，但這把鎖只鎖「跑不跑這段初始化」這個判斷本身，
一次性、跑完就不會再進來，不是包住每次資料庫存取的那種鎖（那種鎖的
問題見 db_utils/connection.py 開頭的說明）。
"""

from __future__ import annotations

import threading

_schema_ready = False
_schema_lock = threading.Lock()

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
    # 先在鎖外面快速判斷——絕大多數呼叫（process 生命週期裡的第二次
    # 之後）都是這條路徑，不用每次都付搶鎖的成本。
    if _schema_ready:
        return

    with _schema_lock:
        # 鎖內要重新檢查一次：可能在等鎖的這段時間，另一個執行緒已經
        # 把初始化跑完了（double-checked locking），不重新檢查的話會
        # 白白把補欄位那幾段再跑一次，撞上前面說的 TOCTOU 錯誤。
        if _schema_ready:
            return

        _ensure_permissions(cur)
        _ensure_users(cur)
        _ensure_projects(cur)
        _ensure_usage_log(cur)
        _ensure_sessions(cur)
        _ensure_jobs(cur)
        _ensure_app_versions(cur)

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


def _ensure_jobs(cur) -> None:
    # Stage 1 潤飾 job 與 Stage 2 校對共用這張表（用 kind 區分）。狀態
    # 整包存成 JSONB，因為 job_manager/review_manager 本來就是操作一個
    # dict，序列化整包最單純，不用為了兩種工作各自維護一組欄位。
    # 詳細設計說明見 db_utils/job_store.py。
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            user_email TEXT,
            status TEXT NOT NULL,
            payload JSONB NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # 清理過期紀錄（updated_at < 24 小時前）與「目前有幾個工作在跑」
    # 都會掃這兩個欄位
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")


def _ensure_app_versions(cur) -> None:
    # 桌面版 App 的強制更新門檻。原本一個作業系統一列（os UNIQUE），
    # 但 macOS 13 以下要另外拆一包 App（舊系統只留 Stage 2，Stage 1 鎖
    # 住），兩包各自有各自的最低支援版號，同一個 os 底下要能同時放兩
    # 筆——唯一鍵改成 (os, os_version) 複合鍵，os_version 從「先留著、
    # 目前沒用到」的欄位變成真正拿來分流 build 的欄位。版本號本身仍然
    # 拆 major/minor 兩個整數存，不存版本字串——字串排序會把 "0.9" 排在
    # "0.10" 後面，比較邏輯本身就是錯的；拆欄位存，呼叫端
    # （version_check API）直接用整數比較，不用在查詢當下現拆字串。
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_versions (
            id SERIAL PRIMARY KEY,
            os TEXT NOT NULL,
            os_version TEXT NOT NULL,
            latest_major INTEGER NOT NULL,
            latest_minor INTEGER NOT NULL,
            min_major INTEGER NOT NULL,
            min_minor INTEGER NOT NULL
        )
        """
    )

    # 上面的 CREATE TABLE IF NOT EXISTS 對「表已經存在」的正式環境是
    # 空操作，不會把舊的 os UNIQUE 換成新的複合鍵、也不會替既有那筆
    # 資料補上 os_version——這幾步用 pg_constraint 查過現況再動，一定
    # 跑得完全冪等，跑第二次（constraint 已經是新的）會直接跳過。
    cur.execute(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'app_versions'::regclass AND contype = 'u'"
    )
    existing_constraints = {row[0] for row in cur.fetchall()}
    if "app_versions_os_key" in existing_constraints:
        cur.execute("ALTER TABLE app_versions DROP CONSTRAINT app_versions_os_key")
    if "app_versions_os_os_version_key" not in existing_constraints:
        cur.execute(
            "ALTER TABLE app_versions "
            "ADD CONSTRAINT app_versions_os_os_version_key UNIQUE (os, os_version)"
        )

    # 歷史一次性遷移殘留：早期環境唯一一筆資料的 os_version 過去沒在
    # 用，是 NULL，當初補成 '13+' 才能正確落在新的分流鍵裡。這筆資料
    # 現在已經改名成 '11+'（見下方說明），不會再有 NULL 需要補，這段
    # UPDATE 對正式環境是無資料可改的空跑，留著純粹冪等、無副作用。
    # 補完之後才能把欄位鎖成 NOT NULL——複合唯一鍵允許多筆 NULL 同時
    # 存在（Postgres 的 NULL 互不相等），os_version 是 NULL 的話「同一
    # 個 os 只能一筆」這個舊限制其實還是沒解除。
    cur.execute(
        "UPDATE app_versions SET os_version = '13+' "
        "WHERE os = 'macos' AND os_version IS NULL"
    )
    cur.execute("ALTER TABLE app_versions ALTER COLUMN os_version SET NOT NULL")

    # 這張表刻意不在這裡自動補種子資料。version_check API（見 app.py）
    # 對查無門檻資料的 (os, os_version) 組合本來就設計成一律不擋
    # （force_update: False），完全沒有資料不影響任何人；每個分流的
    # 門檻要有意義的值時，直接手動 INSERT/UPDATE 這張表（見
    # update_version skill），不要在這裡自動生。
    #
    # 這裡曾經有一段「查無 os_version='13+' 就自動種一筆預設值
    # (1.0, 1.0)」的邏輯，是最早從「每個 os 一列」升級成 (os,
    # os_version) 複合鍵時的一次性遷移殘留。後來 13+ 分流改名成 11+
    # 之後（zh-cn-to-tw-mac 1.3），DB 裡再也沒有 os_version='13+' 的
    # 資料，這段「查無就補」的判斷條件從此永遠成立，變成每次 backend
    # 啟動都會憑空重新種出一筆過期、沒有任何 client 會查到的
    # (macos, '13+', ...) 死資料（2026-08-21 發現、移除）。
