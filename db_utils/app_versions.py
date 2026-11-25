"""
桌面版 App 強制更新門檻的讀取層（表結構見 db_utils/schema.py 的
_ensure_app_versions）。目前只有讀取——調整門檻直接改 Neon 裡的
資料，還沒有另外做管理員介面。

版本檢查（GET /api/version_check）不需要登入，誰都能呼叫，所以結果放在
記憶體快取：整張表一次讀進來（只有幾列），config.APP_VERSIONS_CACHE_SECONDS
秒內的查詢都直接從記憶體回答，不管查的是哪個 os/os_version 組合。只快取
「查過的組合」不夠：對方每次換一組參數，就能讓每個請求都去開一次資料庫
連線，而同時連線上限只有 5（見 db_utils/connection.py）。代價是在 Neon
改了門檻之後，最晚要等這段時間才生效。

讀資料庫的那一段刻意不包在鎖裡（鎖裡不能包網路呼叫，理由見
db_utils/connection.py 的歷史教訓一）：快取剛好過期的瞬間，同時進來的
幾個請求可能各自讀一次，數量受上面那個連線上限節制，不會失控。各自讀完
寫回快取時，只有「比目前快取更晚開始讀」的那份會寫進去：先開始讀、卻因為
連線比較慢而比較晚讀完的那份是比較舊的資料，不能蓋掉已經寫進去的新資料。

讀取失敗時沿用上一份，並把它當成剛讀過：資料庫出問題的期間，不讓這個不用
登入的端點每個請求都再去試一次連線。
"""

from __future__ import annotations

import threading
import time

from configs import config
from db_utils.connection import get_ready_conn

_cache_lock = threading.Lock()
_cached_at: float | None = None
_cached_policies: dict[tuple[str, str], dict] = {}


def _load_all() -> dict[tuple[str, str], dict]:
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            "SELECT os, os_version, latest_major, latest_minor, min_major, min_minor FROM app_versions"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return {
        (os_name, os_version): {
            "latest_major": latest_major,
            "latest_minor": latest_minor,
            "min_major": min_major,
            "min_minor": min_minor,
        }
        for os_name, os_version, latest_major, latest_minor, min_major, min_minor in rows
    }


def get_policies() -> dict[tuple[str, str], dict]:
    """回傳整張門檻表，key 是 (os, os_version)（os_version 是同一個 os 底下
    各自獨立發版的分流，例如 '11+'、'10.15'）。表裡沒有的組合代表不是任何
    一個正式版會送的值，怎麼處理由呼叫端決定。從來沒讀成功過時往外丟。"""
    global _cached_at, _cached_policies

    with _cache_lock:
        if _cached_at is not None and time.monotonic() - _cached_at < config.APP_VERSIONS_CACHE_SECONDS:
            return _cached_policies

    # 快取的時間記「開始讀」的時間，不是讀完的時間：門檻改了之後最晚
    # APP_VERSIONS_CACHE_SECONDS 秒生效這個說法才不會因為連線慢而失準，
    # 同時也拿來判斷同時讀的幾份誰比較新
    started = time.monotonic()
    try:
        policies = _load_all()
    except Exception as exc:  # noqa: BLE001
        with _cache_lock:
            # 讀不到就沿用上一份（門檻很少改，舊資料比沒有資料好）；從來沒讀
            # 成功過就照舊往外丟
            if _cached_at is None:
                raise
            print(f"[app_versions] 讀取門檻失敗，沿用上一份快取：{exc}", flush=True)
            if started > _cached_at:
                _cached_at = started
            return _cached_policies

    with _cache_lock:
        if _cached_at is None or started >= _cached_at:
            _cached_policies = policies
            _cached_at = started
        return _cached_policies
