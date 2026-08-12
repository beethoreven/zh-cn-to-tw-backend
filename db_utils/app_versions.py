"""
桌面版 App 強制更新門檻的讀取層（表結構見 db_utils/schema.py 的
_ensure_app_versions）。目前只有讀取——調整門檻直接改 Neon 裡的
資料，還沒有另外做管理員介面。
"""

from __future__ import annotations

from db_utils.connection import get_ready_conn, sql


def get_policy(os_name: str) -> dict | None:
    """回傳指定作業系統的版本門檻，查無資料回傳 None（呼叫端應該視為
    「沒有門檻資訊，不擋」，不要讓查詢失敗變成使用者連工作都開始不了）。"""
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql(
                "SELECT latest_major, latest_minor, min_major, min_minor "
                "FROM app_versions WHERE os = ?"
            ),
            (os_name,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    latest_major, latest_minor, min_major, min_minor = row
    return {
        "latest_major": latest_major,
        "latest_minor": latest_minor,
        "min_major": min_major,
        "min_minor": min_minor,
    }
