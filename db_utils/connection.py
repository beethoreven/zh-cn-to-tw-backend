"""
全站共用的資料庫連線層。

跟 usage_log 原本各管各的連線邏輯不一樣——現在有 usage_log、users、
permissions、projects 四張表要管，所有需要碰資料庫的模組（usage/、
admin_utils/、auth_utils/）都共用同一套連線/方言轉換邏輯，不要每個
模組各自複製一份 SQLite/Postgres 雙模式的判斷。

規則跟原本 usage_log 一樣：本機開發預設用 SQLite（跟這支程式同一個
repo 根目錄的 usage.db），設定 DATABASE_URL 環境變數就改連 Postgres
（正式環境用 Neon，因為 Render 免費方案的檔案系統非持久性）。

所有呼叫端都要在 LOCK 保護下呼叫 get_conn()，避免 SQLite 同時多個
連線寫入的問題，Postgres 這邊雖然不是必要，但統一用同一個鎖，邏輯
簡單、也不會有效能瓶頸（這個工具的呼叫量很小）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "usage.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)

LOCK = threading.Lock()


def sql(query: str) -> str:
    """SQLite 用 `?` 佔位符寫查詢語法(單一事實來源)，這裡在 Postgres 模式
    下轉成 `%s`，避免每條查詢都要維護兩份幾乎一樣的字串。"""
    return query.replace("?", "%s") if USE_POSTGRES else query


def get_conn():
    """回傳 (conn, cur)，不做任何 schema 檢查/建立——schema 的事由
    db_utils.schema.ensure_schema() 統一負責，且只在 process 生命週期裡
    做一次。SQLite 模式下 conn 跟 cur 是分開的物件(這裡統一都用
    conn.cursor() 拿 cur，兩種模式呼叫端寫法一致)；Postgres 模式下一樣。
    呼叫端一律用 cur.execute(...) 查詢、conn.commit()/conn.close() 管理
    連線。"""
    if USE_POSTGRES:
        import psycopg2

        conn = psycopg2.connect(DATABASE_URL)
    else:
        conn = sqlite3.connect(DB_PATH)

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
