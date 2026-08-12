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
5 秒輪詢一次進度、每個請求的登入驗證又要各自連線，疊加起來會讓
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

歷史教訓三：把全域鎖拿掉之後，一度變成「完全沒有任何併發上限」，結果在
Render 免費方案（0.1 CPU / 512 MB）上實測 10 個併發請求直接把 instance
打掛（整個服務 502，要等它自己重啟才恢復）。那把鎖雖然把並行度壓到 1、
造成前面說的凍結，但它同時也是唯一在做「反壓」的東西——拿掉它就等於
把水閘整個拆了。教訓是：問題從來不是「有沒有上限」，而是「上限是 1，
而且持有上限的那段時間沒有邊界」。

現在的做法：每次呼叫都獨立開一條連線、用完就真的關掉，不共用池；但用
一個號誌（semaphore）把「同時存在的連線數」限制在 _MAX_CONCURRENT_DB
以內。號誌跟互斥鎖的關鍵差別是它允許 N 個同時進行，不是 1 個——一條
連線卡住只會用掉 N 個名額中的一個，其他人照常運作；再加上
connect_timeout 保證任何一條連線最多卡 10 秒就會失敗釋出名額，所以
最壞情況是有邊界的，不會像原本那樣無限期凍結全站。
"""

from __future__ import annotations

import threading

import psycopg2
import psycopg2.extensions

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

# 同時最多幾條資料庫連線。這是「反壓」，不是「互斥」——差別很重要：
# 互斥鎖只准 1 個進行，一卡住全站跟著死；號誌准 N 個同時進行，一條卡住
# 只用掉 N 個名額中的一個。Render 免費方案是 0.1 CPU / 512 MB，實測完全
# 不設上限時，10 個併發連線就足以把整個 instance 打掛（服務 502，要等它
# 自己重啟）。5 這個數字對這個工具的實際並行量（前端輪詢 + 背景批次）
# 綽綽有餘，又離「打爆 instance」有安全距離。
_MAX_CONCURRENT_DB = 5
_db_slots = threading.Semaphore(_MAX_CONCURRENT_DB)

# 等不到名額時最多等多久。一定要有上限：不設的話，一旦所有名額都被慢速
# 連線佔住，後面的請求會無限期排隊，又變回「整個後端凍住」的老問題，
# 只是換成排在號誌前面而不是排在鎖前面。等不到就明確失敗，讓呼叫端能
# 回報錯誤、讓使用者知道發生什麼事。
_SLOT_WAIT_TIMEOUT_SECONDS = 20


class _SlotGuard:
    """借用一個連線名額，並保證它一定會被還回去。

    綁在 connection 物件上（見 get_conn）：呼叫端原本就一定會呼叫
    conn.close()（每個呼叫端都寫在 finally 裡），把「還名額」掛在
    close() 上，就不需要要求所有呼叫端改寫成 with 區塊。
    """

    def __init__(self):
        if not _db_slots.acquire(timeout=_SLOT_WAIT_TIMEOUT_SECONDS):
            raise RuntimeError(
                f"資料庫忙碌中（同時連線數已達上限 {_MAX_CONCURRENT_DB}），"
                f"等待 {_SLOT_WAIT_TIMEOUT_SECONDS} 秒後仍無法取得連線"
            )
        self._released = False

    def release(self):
        # 防止重複釋放：呼叫端如果不小心 close() 兩次，多還一個名額會讓
        # 上限逐漸失效，是很難察覺的漏洞。
        if not self._released:
            self._released = True
            _db_slots.release()


class _SlotReleasingConnection(psycopg2.extensions.connection):
    """close() 時順便把號誌名額還回去。

    為什麼要用子類別而不是直接覆寫 conn.close：psycopg2 的 connection 是
    C extension，close 是唯讀屬性，指派會直接丟
    `AttributeError: attribute 'close' is read-only`（實測撞過）。
    connection_factory 是官方支援的擴充點，用它才改得動。

    注意這裡跟先前試過又撤掉的連線池不同：這條連線 close() 就是真的關閉，
    沒有任何共用的池或鎖，名額只是計數器加減，不會有「一條卡住、其他人
    連還都還不了」的問題。
    """

    _slot_guard = None

    def attach_slot(self, guard):
        self._slot_guard = guard

    def close(self):
        try:
            super().close()
        finally:
            if self._slot_guard is not None:
                self._slot_guard.release()
                self._slot_guard = None


def sql(query: str) -> str:
    """查詢語法統一用 `?` 佔位符撰寫（單一事實來源），這裡轉成 psycopg2
    要的 `%s`。保留這層轉換是為了不用把散落在各模組、已經驗證過的查詢
    字串全部改寫一遍。"""
    return query.replace("?", "%s")


def get_conn():
    """回傳 (conn, cur)，不做任何 schema 檢查/建立——schema 的事由
    db_utils.schema.ensure_schema() 統一負責，且只在 process 生命週期裡
    做一次。每次呼叫都是一條獨立、用完即關的新連線，不共用連線池，但受
    _MAX_CONCURRENT_DB 這個號誌節制（見檔案開頭的說明）。

    呼叫端照舊在 finally 裡呼叫 conn.close() 即可，名額會跟著一起還回去。
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "環境變數 DATABASE_URL 未設定，無法連線資料庫。"
            "本機開發請在 zh-cn-to-tw-backend/.env 填入 Neon 連線字串；"
            "桌面版 App 則是打包時把 .env 一起放進執行檔旁邊。"
        )

    guard = _SlotGuard()
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            connection_factory=_SlotReleasingConnection,
        )
    except Exception:
        # 連線失敗一定要把名額還回去，不然失敗幾次之後名額就被耗光了，
        # 整個後端會卡在「等名額」——那正是這次要根治的凍結型故障。
        guard.release()
        raise

    conn.attach_slot(guard)
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
