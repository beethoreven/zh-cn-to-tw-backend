"""
管理操作紀錄：管理員做了什麼、被拒絕的特權操作，事後查得到。

每筆只記時間、誰（登入的 email）、動作代碼、對象 id、結果，刻意不記
請求內容：改名、改 email 這類欄位值、劇本內容、token、IP 都不寫進來。
要知道「改成什麼」去看資料本身；這張表回答的是「誰在什麼時候動過哪一筆」。

成功的操作用 record(cur, ...)，跟操作本身共用同一個 cursor、同一個交易：
操作 commit 了紀錄就一定在，操作失敗回滾了紀錄也跟著消失，不會出現
「資料改了卻沒紀錄」或「有紀錄但其實沒改成」。

被拒絕或失敗的操作沒有可以搭的交易，用 record_standalone() 另外開連線
寫。這條路徑寫失敗只印 log、不往外拋：原本的回應（401、400）不該因為
紀錄寫不進去而變成 500。

保留 RETENTION_DAYS 天，由 app.py 既有的每小時清理迴圈呼叫
delete_expired() 清掉更舊的。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db_utils.connection import get_ready_conn, sql

RETENTION_DAYS = 180

RESULT_SUCCESS = "success"
# 已登入、但不是管理員（或帳號已停用）卻呼叫管理 API
RESULT_DENIED = "denied"
# 是管理員，但操作沒有成立：欄位不合法、找不到對象、會讓系統沒有管理員等
RESULT_FAILED = "failed"
_RESULTS = (RESULT_SUCCESS, RESULT_DENIED, RESULT_FAILED)

# 列表 API 一次最多回傳幾筆
MAX_LIST_LIMIT = 500


def _now_iso() -> str:
    # 一律 UTC、同一種 ISO 格式，delete_expired 才能直接比字串
    return datetime.now(timezone.utc).isoformat()


def record(cur, actor_email: str, action: str, target_id: int | None, result: str) -> None:
    """用呼叫端的 cursor 寫一筆，不 commit——由呼叫端跟操作一起 commit。"""
    if result not in _RESULTS:
        raise ValueError(f"未知的結果代碼：{result}")
    cur.execute(
        sql(
            "INSERT INTO admin_audit_log (created_at, actor_email, action, target_id, result) "
            "VALUES (?, ?, ?, ?, ?)"
        ),
        (_now_iso(), actor_email, action, target_id, result),
    )


def record_standalone(actor_email: str, action: str, target_id: int | None, result: str) -> None:
    """給沒有交易可搭的情況（被拒絕、驗證失敗）。寫不進去只印 log。"""
    try:
        conn, cur = get_ready_conn()
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] 寫入失敗（{action} {result}）：{exc}", flush=True)
        return
    try:
        record(cur, actor_email, action, target_id, result)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] 寫入失敗（{action} {result}）：{exc}", flush=True)
    finally:
        conn.close()


def list_entries(limit: int = 100, before_id: int | None = None) -> list[dict]:
    """新的在前。before_id 用來往前翻頁：只回傳 id 比它小的。"""
    limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    conn, cur = get_ready_conn()
    try:
        if before_id is None:
            cur.execute(
                sql(
                    "SELECT id, created_at, actor_email, action, target_id, result "
                    "FROM admin_audit_log ORDER BY id DESC LIMIT ?"
                ),
                (limit,),
            )
        else:
            cur.execute(
                sql(
                    "SELECT id, created_at, actor_email, action, target_id, result "
                    "FROM admin_audit_log WHERE id < ? ORDER BY id DESC LIMIT ?"
                ),
                (before_id, limit),
            )
        return [
            {
                "id": row[0],
                "created_at": row[1],
                "actor_email": row[2],
                "action": row[3],
                "target_id": row[4],
                "result": row[5],
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def delete_expired() -> int:
    """刪掉超過 RETENTION_DAYS 天的紀錄，回傳刪掉的筆數。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    conn, cur = get_ready_conn()
    try:
        cur.execute(sql("DELETE FROM admin_audit_log WHERE created_at < ?"), (cutoff,))
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()
