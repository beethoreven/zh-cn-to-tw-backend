"""
全站共用的資料庫連線層。

usage_log、users、permissions、projects、sessions 這幾張表都走這裡，
所有需要碰資料庫的模組（usage/、admin_utils/、auth_utils/）共用同一套
連線邏輯，不要每個模組各自複製一份。

一律連 Postgres（正式用 Neon），不再支援 SQLite 雙模式：桌面版 App 跟
Render 都是連同一個 Neon 資料庫，本機開發也是，只有一種資料來源，
不會再有「本機測起來好好的、上線卻是另一份資料」這種落差。

所有呼叫端都要在 LOCK 保護下取得連線。這個工具的呼叫量很小，統一用
同一個鎖邏輯簡單，也不會有效能瓶頸。
"""

from __future__ import annotations

import threading

# 一定要透過 configs.config 讀，不要在這裡直接讀 os.environ——這支模組
# 會在 app.py 最上面就被間接 import（admin_utils/auth_utils 都會用到），
# 時間點比 config 的 load_dotenv() 早，直接讀 os.environ 會讀不到 .env
# 裡的值。詳細說明見 configs/config.py 的 DATABASE_URL。
from configs import config

DATABASE_URL = config.DATABASE_URL

LOCK = threading.Lock()


def sql(query: str) -> str:
    """查詢語法統一用 `?` 佔位符撰寫（單一事實來源），這裡轉成 psycopg2
    要的 `%s`。保留這層轉換是為了不用把散落在各模組、已經驗證過的查詢
    字串全部改寫一遍。"""
    return query.replace("?", "%s")


def get_conn():
    """回傳 (conn, cur)，不做任何 schema 檢查/建立——schema 的事由
    db_utils.schema.ensure_schema() 統一負責，且只在 process 生命週期裡
    做一次。"""
    if not DATABASE_URL:
        raise RuntimeError(
            "環境變數 DATABASE_URL 未設定，無法連線資料庫。"
            "本機開發請在 zh-cn-to-tw-backend/.env 填入 Neon 連線字串；"
            "桌面版 App 則是打包時把 .env 一起放進執行檔旁邊。"
        )

    import psycopg2

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    return conn, cur


def get_ready_conn():
    """回傳 (conn, cur)，且保證 schema 已經就緒——大部分呼叫端要的其實
    是這個，不是裸的 get_conn()。集中在這裡避免每個用到資料庫的模組都
    各自重複「拿連線 + 確保 schema + commit」這三行。"""
    from db_utils.schema import ensure_schema  # 延遲 import，避免循環引用

    conn, cur = get_conn()
    ensure_schema(cur)
    conn.commit()
    return conn, cur
