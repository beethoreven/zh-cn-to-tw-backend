"""
job / review 狀態的持久化層（Neon）。

為什麼需要：job 狀態原本只存在 process 記憶體裡，Render 只要重啟
（部署、平台搬機器、閒置休眠）進行中的工作就整份消失，前端只會看到
「找不到這個處理進度」，而那個 job 已經花掉的 LLM token 全部作廢，
必須從頭重跑重付一次。

設計重點（三個都是刻意的，不要改掉）：

1. **記憶體是讀取路徑，Neon 是復原路徑。** 前端每 5 秒輪詢一次進度，
   每次輪詢都查 DB 會讓整個介面卡死。所以正常情況一律從記憶體讀，
   只有記憶體裡沒有（代表這個 process 是重啟後的新 process）才回頭查
   Neon。（當初的量化理由是「Neon 單次連線+查詢實測 1.2-1.6 秒」，那
   個數字在 2026-08-25 之後不成立——見 db_utils/connection.py 教訓四。
   這個設計仍然保留，因為它真正的價值是「process 重啟後救得回來」，
   不只是省延遲；但不要再引用那個過期數字。）

2. **寫入要節流。** append_log 一個批次會被呼叫好幾次，每次都寫 Neon
   會把批次處理拖慢好幾倍。狀態轉換（完成/失敗/中斷）跟每批的部分成果
   一定要立刻寫，純 log 更新則最多每 _LOG_WRITE_MIN_INTERVAL 秒寫一次。

3. **沒人在跑的未完成工作要標成 interrupted，用心跳判斷誰沒人在跑。**
   光是把狀態存下來還不夠：process 死掉後那筆資料會停在 status=running，
   但實際上沒有任何執行緒在跑它，前端會永遠看到「執行中」而不會跳出重試
   對話窗，變成無聲卡死——比原本的「找不到 job」更糟。

   原本的做法是「啟動時把 pending/running 全部標成 interrupted」，前提是
   同一時間只有一個實例。部署時新舊實例只要重疊一段時間，新實例就會把
   舊實例還在跑的工作標成 interrupted；舊實例下一次寫入又把狀態蓋回
   running，接著舊實例被關掉，那筆就永遠停在 running。

   現在改成心跳：每個 process 對自己記憶體裡還在跑的工作，每
   HEARTBEAT_INTERVAL 秒更新一次 updated_at（touch_active）；啟動後的一段
   時間內定期把「不是自己的、而且超過 STALE_AFTER 秒沒有心跳」的
   pending/running 標成 interrupted（mark_stale_as_interrupted）。還活著的
   舊實例會持續心跳，不會被誤標；它死掉之後心跳停了，最多 STALE_AFTER
   秒後就被標起來。代價是 process 重啟後，前端要等最多約 STALE_AFTER 秒
   才看到「已中斷」（原本是立刻）。只有記憶體裡真的有工作時才寫心跳，
   沒工作時不會為了心跳去叫醒 Neon。
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

# 心跳間隔與「多久沒心跳就當成沒人在跑」，見模組說明第 3 點。STALE_AFTER
# 要明顯大於 HEARTBEAT_INTERVAL，一兩次心跳寫入失敗（Neon 冷啟動逾時）
# 才不會被誤判。
HEARTBEAT_INTERVAL = 20.0
STALE_AFTER = 90.0

# 終止狀態：進到這裡之後不能再被改成別的狀態（finalize 把 interrupted
# 收成 done 是唯一的例外，見各 manager 的 finalize_with_partial）。背景
# 執行緒晚到的 set_status/set_result/set_error 不能把已經結束的工作蓋掉。
TERMINAL_STATUSES = frozenset({"done", "failed", "interrupted"})

# 各 manager 登記「記憶體裡還沒結束的工作 id」的函式，心跳與「哪些是自己
# 的工作」都靠這份清單。用登記的方式是因為 manager 都 import 這個模組，
# 反過來 import 會循環。
_active_sources = []


def register_active_source(fn) -> None:
    _active_sources.append(fn)


def local_active_ids() -> list[str]:
    ids: list[str] = []
    for fn in _active_sources:
        ids.extend(fn())
    return ids

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

    # ensure_ascii=False：預設會把每個中文字寫成 \uXXXX（6 bytes），比 UTF-8
    # 的 3 bytes 大一倍。整包狀態每批都要送一次 Neon，而 Render 會把連到 Neon
    # 的流量算進 outbound bandwidth（2026-10 實測這裡佔了八成以上）。存進
    # JSONB 之後內容完全一樣，只差傳輸大小。
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
                (job_id, kind, user_email, status, json.dumps(payload, ensure_ascii=False), _now_iso(), _now_iso()),
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


def touch_active(job_ids: list[str]) -> None:
    """心跳：把這個 process 還在跑的工作的 updated_at 更新成現在。只更新
    還是 pending/running 的，已經被標成別的狀態就不去動它。"""
    if not job_ids:
        return
    try:
        conn, cur = get_ready_conn()
        try:
            cur.execute(
                sql(
                    "UPDATE jobs SET updated_at = ? "
                    "WHERE id = ANY(?::text[]) AND status IN ('pending', 'running')"
                ),
                (_now_iso(), list(job_ids)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        # 心跳失敗一兩次沒關係，STALE_AFTER 留了好幾次心跳的餘裕
        pass


def mark_stale_as_interrupted(own_ids: list[str]) -> tuple[int, int]:
    """把「不是這個 process 的、而且超過 STALE_AFTER 秒沒有心跳」的
    pending/running 標成 interrupted，見模組說明第 3 點。

    回傳 (這次標記的筆數, 還剩幾筆不是自己的 pending/running)。後者給
    呼叫端判斷還要不要繼續掃：不是自己的未完成工作都處理完了就可以停，
    不用一直查 Neon。查詢失敗時回傳 (0, -1)，代表「不知道」，要繼續掃。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER)).isoformat()
    own = list(own_ids)
    try:
        conn, cur = get_ready_conn()
        try:
            cur.execute(
                sql(
                    "UPDATE jobs SET status = 'interrupted', updated_at = ? "
                    "WHERE status IN ('pending', 'running') AND updated_at < ? "
                    "AND NOT (id = ANY(?::text[]))"
                ),
                (_now_iso(), cutoff, own),
            )
            marked = cur.rowcount
            cur.execute(
                sql(
                    "SELECT COUNT(*) FROM jobs "
                    "WHERE status IN ('pending', 'running') AND NOT (id = ANY(?::text[]))"
                ),
                (own,),
            )
            remaining = cur.fetchone()[0]
            conn.commit()
            return marked, remaining
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0, -1


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
