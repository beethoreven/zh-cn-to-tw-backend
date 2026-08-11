"""
job / review 狀態的持久化層（Neon）。

為什麼需要：job 狀態原本只存在 process 記憶體裡，Render 只要重啟
（部署、平台搬機器、閒置休眠）進行中的工作就整份消失，前端只會看到
「找不到這個處理進度」，而那個 job 已經花掉的 LLM token 全部作廢，
必須從頭重跑重付一次。

設計重點（三個都是刻意的，不要改掉）：

1. **記憶體是讀取路徑，Neon 是復原路徑。** 前端每 1.5 秒輪詢一次進度，
   而 Neon 單次連線+查詢實測要 1.2-1.6 秒——每次輪詢都查 DB 會讓整個
   介面卡死。所以正常情況一律從記憶體讀，只有記憶體裡沒有（代表這個
   process 是重啟後的新 process）才回頭查 Neon。

2. **寫入要節流。** append_log 一個批次會被呼叫好幾次，每次都寫 Neon
   會把批次處理拖慢好幾倍。狀態轉換（完成/失敗/中斷）跟每批的部分成果
   一定要立刻寫，純 log 更新則最多每 _LOG_WRITE_MIN_INTERVAL 秒寫一次。

3. **重啟時把未完成的 job 標成 interrupted。** 光是把狀態存下來還不夠：
   重啟後那筆資料會停在 status=running，但實際上沒有任何執行緒在跑它，
   前端會永遠看到「執行中」而不會跳出重試對話窗，變成無聲卡死——比原本
   的「找不到 job」更糟。process 重啟了就代表照定義不可能還有東西在跑，
   所以啟動時直接把 pending/running 全部標成 interrupted。
   （前提是只跑單一實例，Render 免費方案就是如此；之後若擴成多實例，
   這條規則要改成用心跳時間判斷。）
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

from db_utils.connection import get_ready_conn, sql

# 完成/中斷的工作保留多久。使用者可能重整頁面之後才回來下載成果，
# 所以不能一做完就刪；但也沒必要永久保留（Neon 免費方案容量有限，
# 一份劇本的文字有幾百 KB）。
RETENTION_HOURS = 24

_LOG_WRITE_MIN_INTERVAL = 5.0

_throttle_lock = threading.Lock()
_last_write: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(job_id: str, kind: str, status: str, user_email: str | None, payload: dict,
         *, force: bool = True) -> None:
    """把整份狀態寫進 Neon。force=False 代表這次只是 log 更新，可以被
    節流掉（見模組說明第 2 點）。"""
    if not force:
        now = time.time()
        with _throttle_lock:
            if now - _last_write.get(job_id, 0.0) < _LOG_WRITE_MIN_INTERVAL:
                return
            _last_write[job_id] = now
    else:
        with _throttle_lock:
            _last_write[job_id] = time.time()

    try:
        conn, cur = get_ready_conn()
        try:
            cur.execute(
                sql(
                    "INSERT INTO jobs (id, kind, user_email, status, payload, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "status = EXCLUDED.status, payload = EXCLUDED.payload, "
                    "updated_at = EXCLUDED.updated_at"
                ),
                (job_id, kind, user_email, status, json.dumps(payload), _now_iso(), _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        # 持久化失敗絕對不能讓正在跑的工作掛掉——這一層是「額外的保險」，
        # 不是主要流程。最壞情況只是這次重啟後救不回來，跟沒有這層一樣，
        # 不該因此讓使用者眼前正在跑的批次直接失敗。
        pass


def load(job_id: str) -> dict | None:
    """從 Neon 撈回狀態。只有記憶體裡找不到時才會走到這裡。"""
    try:
        conn, cur = get_ready_conn()
        try:
            cur.execute(sql("SELECT payload, status FROM jobs WHERE id = ?"), (job_id,))
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None

    if row is None:
        return None
    payload, status = row
    if isinstance(payload, str):
        payload = json.loads(payload)
    # DB 的 status 才是權威（啟動時可能已經把它改標成 interrupted），
    # payload 裡那份是寫入當下的舊值
    payload["status"] = status
    return payload


def mark_running_as_interrupted() -> int:
    """process 啟動時呼叫，見模組說明第 3 點。回傳被標記的筆數。"""
    try:
        conn, cur = get_ready_conn()
        try:
            cur.execute(
                sql(
                    "UPDATE jobs SET status = 'interrupted', updated_at = ? "
                    "WHERE status IN ('pending', 'running')"
                ),
                (_now_iso(),),
            )
            count = cur.rowcount
            conn.commit()
            return count
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0


def delete_expired() -> int:
    """刪掉超過保留期的紀錄。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS)).isoformat()
    try:
        conn, cur = get_ready_conn()
        try:
            cur.execute(sql("DELETE FROM jobs WHERE updated_at < ?"), (cutoff,))
            count = cur.rowcount
            conn.commit()
            return count
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0


def count_active() -> int:
    """目前有幾個工作正在跑。給「部署前先確認沒有工作在進行」用
    （見 /api/jobs/active）。"""
    try:
        conn, cur = get_ready_conn()
        try:
            cur.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'running')")
            return cur.fetchone()[0]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0
