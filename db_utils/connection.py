"""
全站共用的資料庫連線層。

usage_log、users、permissions、projects、sessions 這幾張表都走這裡，
所有需要碰資料庫的模組（usage/、admin_utils/、auth_utils/）共用同一套
連線邏輯，不要每個模組各自複製一份。

一律連 Postgres（正式用 Neon），不再支援 SQLite 雙模式：桌面版 App 跟
Render 都是連同一個 Neon 資料庫，本機開發也是，只有一種資料來源，
不會再有「本機測起來好好的、上線卻是另一份資料」這種落差。

歷史教訓一（2026-08-12，詳見 known-issue-check skill item 16）：這裡曾經
有一把全域 LOCK，序列化所有呼叫端「取得連線」這個動作，而取得連線的
實作是每次都對 Neon 開一條全新的連線（實測 1.2-1.6 秒）。這在本機接
近端資料庫時完全無感，但部署到 Render 打真正的遠端 Neon 之後，等於
把每一次資料庫存取都變成一次「鎖住全站 1.2-1.6 秒」的操作——前端每
1.5 秒輪詢一次進度、每個請求的登入驗證又要各自連線，疊加起來會讓
整個後端卡住達分鐘等級，而且卡住時完全没有連上 Neon（用
pg_stat_activity 實測過，Neon 端看到的連線數是 0——請求根本沒送到，
是卡在自己 process 內部搶鎖）。這把鎖從一開始（加 Postgres 支援的那個
commit）就在，不是哪次修改新增的；Postgres 本身透過 MVCC 正確處理
並行，這把鎖從來就不是必要的，已經拿掉。

歷史教訓二：拿掉那把鎖後，第一個念頭是「改用連線池減少每次都要重新
建立連線的成本」，用了 psycopg2.pool.ThreadedConnectionPool，結果實測
出一模一樣的凍結——只是換了一層、藏得更隱密。那個 pool 內部用同一把
鎖同時保護「借連線」跟「建立新連線」：池子需要生出新連線時，
psycopg2.connect()（真正的網路操作）是在持有那把鎖的狀態下執行的。
只要有一次連線卡住（Neon 免費方案本來就會閒置休眠、之後的請求要等它
冷啟動），其他所有執行緒連「還一條已經用完的連線」都做不到，整個池子
一起凍結。實測：10 個併發請求，第一個 12 秒完成後，其餘 9 個卡了
90 秒以上完全沒有動靜。這正是要修的那個「鎖包住無邊界網路操作」的
問題，只是換了個地方重演一次，已經撤掉。

現在的做法：完全不用共用的鎖或池，每次呼叫都獨立開一條連線、用完就真的
關掉，但明確帶 connect_timeout——這樣一來，慢是「這一個呼叫自己慢」，
不會拖累其他任何人；連線卡住也是快速失敗（10 秒逾時），不會無限期掛著。
這個工具呼叫量很小，多付一點每次都要重新連線的成本（1.2-1.6 秒），換
來的是任何一次連線異常都只影響那一個請求，不會擴散成全站級的凍結——
這比省下那 1.x 秒更重要。之後如果流量成長到真的需要池化，要挑一個
「建立新連線不會佔住共用鎖」的池實作，不能重蹈這裡的覆轍。
"""

from __future__ import annotations

import psycopg2

# 一定要透過 configs.config 讀，不要在這裡直接讀 os.environ——這支模組
# 會在 app.py 最上面就被間接 import（admin_utils/auth_utils 都會用到），
# 時間點比 config 的 load_dotenv() 早，直接讀 os.environ 會讀不到 .env
# 裡的值。詳細說明見 configs/config.py 的 DATABASE_URL。
from configs import config

DATABASE_URL = config.DATABASE_URL

# 建立連線本身要付真正的網路成本（TCP+TLS 握手，實測對 Neon 是
# 1.2-1.6 秒），不設 timeout 的話，任何一次網路抖動都可能讓這個呼叫
# 無限期掛著，而不是快速失敗。10 秒對這個工具的規模來說綽綽有餘，
# 同時遠比「無限期掛著」安全——這是這次修正裡最核心的一行。
_CONNECT_TIMEOUT_SECONDS = 10


def sql(query: str) -> str:
    """查詢語法統一用 `?` 佔位符撰寫（單一事實來源），這裡轉成 psycopg2
    要的 `%s`。保留這層轉換是為了不用把散落在各模組、已經驗證過的查詢
    字串全部改寫一遍。"""
    return query.replace("?", "%s")


def get_conn():
    """回傳 (conn, cur)，不做任何 schema 檢查/建立——schema 的事由
    db_utils.schema.ensure_schema() 統一負責，且只在 process 生命週期裡
    做一次。每次呼叫都是一條獨立、用完即關的新連線，不共用任何鎖或池
    （見檔案開頭的說明），一個呼叫端連線異常不會拖累其他人。"""
    if not DATABASE_URL:
        raise RuntimeError(
            "環境變數 DATABASE_URL 未設定，無法連線資料庫。"
            "本機開發請在 zh-cn-to-tw-backend/.env 填入 Neon 連線字串；"
            "桌面版 App 則是打包時把 .env 一起放進執行檔旁邊。"
        )

    conn = psycopg2.connect(DATABASE_URL, connect_timeout=_CONNECT_TIMEOUT_SECONDS)
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
