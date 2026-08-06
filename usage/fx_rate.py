"""
USD/TWD 參考匯率查詢。

原本想抓台灣銀行牌告匯率，但那個網站有防爬蟲機制（需要執行 JavaScript
過關），一般後端請求過不去。改用 open.er-api.com——免金鑰、專門設計給
程式呼叫、沒有防爬的免費匯率 API，數字是市場參考匯率，會跟台銀牌告價
有些微落差，但用途只是「把 token 費用換算成大概多少台幣」這種參考值，
不是拿來做實際結匯，這個落差可以接受。

加了簡單的記憶體快取（預設 1 小時），避免每次有人看畫面就打一次外部
API；查詢失敗（網路問題、外部服務掛掉）就退回一個寫死的參考匯率，
不會讓整個使用量頁面因為外部服務不穩而壞掉。
"""

from __future__ import annotations

import threading
import time

import requests

_FX_API_URL = "https://open.er-api.com/v6/latest/USD"
_CACHE_TTL_SECONDS = 3600
_FALLBACK_USD_TWD_RATE = 32.5  # 抓不到即時匯率時的保底參考值

_lock = threading.Lock()
_cached_rate: float | None = None
_cached_at: float = 0.0


def get_usd_twd_rate() -> float:
    """回傳目前的 USD/TWD 參考匯率，1 小時內重複呼叫會用快取。"""
    global _cached_rate, _cached_at

    with _lock:
        if _cached_rate is not None and (time.time() - _cached_at) < _CACHE_TTL_SECONDS:
            return _cached_rate

        try:
            response = requests.get(_FX_API_URL, timeout=5)
            response.raise_for_status()
            data = response.json()
            rate = float(data["rates"]["TWD"])
            _cached_rate = rate
            _cached_at = time.time()
            return rate
        except Exception:  # noqa: BLE001
            # 查不到就用保底值，不快取失敗結果，下次還會再嘗試重新查詢
            return _cached_rate if _cached_rate is not None else _FALLBACK_USD_TWD_RATE
