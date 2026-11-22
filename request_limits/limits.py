"""
請求大小與頻率的上限，用 install(app) 掛在 Flask 的 before_request 上。

為什麼需要：這個後端跑在 Render 免費方案（0.1 CPU、512 MB）、單一行程，
資料庫同時連線上限 5（見 db_utils/connection.py）。沒有上限的話，一個人
（或一個被偷走的 session）就能用大量請求把服務拖垮，或連續開工作燒掉 LLM
額度。

為什麼自己寫、不用現成套件：計數放在行程記憶體裡就夠了——這個服務刻意只跑
一個行程（授權快取、工作狀態都在行程記憶體裡，見 auth_utils/whitelist.py
的說明），不需要 Redis 之類的共用儲存；需要的邏輯也只有「固定時間窗內最多
N 次」，幾十行就寫完，不值得多一個相依套件。哪天改成多行程，每個行程各算
各的，實際上限會變成 N 倍，屆時要換成共用儲存。

只擋洪水式的濫用：門檻設在正常使用量的好幾倍以上（數字見 configs/config.py），
正常操作不會碰到。
"""

from __future__ import annotations

import math
import threading
import time

from flask import jsonify, request

from auth_utils.auth import extract_bearer_token
from configs import config


class FixedWindowLimiter:
    """每個 key 在固定長度的時間窗內最多 limit 次。

    固定時間窗在窗口交界處最多會放過兩倍的量，對「擋洪水」這個目的足夠，
    換來每個 key 只要記兩個數字。key 的數量有上限，超過就清掉已經過期的、
    還不夠就清掉最舊的一半：寧可讓少數 key 重新計數，也不讓有人換一堆來源
    把記憶體撐爆。"""

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 50_000, clock=time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int]] = {}

    def hit(self, key: str) -> float | None:
        """記一次。超過上限回傳還要等幾秒，沒超過回傳 None。超過的那次不計數。"""
        now = self._clock()
        with self._lock:
            started, count = self._windows.get(key, (now, 0))
            if now - started >= self.window:
                started, count = now, 0
            if count >= self.limit:
                return max(0.0, self.window - (now - started))
            if key not in self._windows and len(self._windows) >= self.max_keys:
                self._evict(now)
            self._windows[key] = (started, count + 1)
            return None

    def _evict(self, now: float) -> None:
        expired = [k for k, (started, _) in self._windows.items() if now - started >= self.window]
        for k in expired:
            del self._windows[k]
        if len(self._windows) >= self.max_keys:
            oldest = sorted(self._windows.items(), key=lambda item: item[1][0])
            for k, _ in oldest[: len(oldest) // 2]:
                del self._windows[k]

    def __len__(self) -> int:
        with self._lock:
            return len(self._windows)


_ip_sources_seen: set[str] = set()


def client_ip() -> str:
    """發出請求的 IP。

    Render 前面是 Cloudflare：CF-Connecting-IP 每個請求都會由 Cloudflare
    寫入，呼叫端自己送的值會被蓋掉，所以可信。X-Forwarded-For 不能用——
    Cloudflare 只在後面追加，最左邊那段是呼叫端自己填的，拿它當 key 等於讓
    對方隨意換身分。本機開發沒有 Cloudflare，用連線位址。

    兩種來源各在第一次出現時記一行 log（只記來源與路徑，不記 IP）：部署後
    看 log 就能確認使用者的請求真的帶著 CF-Connecting-IP。如果使用者的請求
    也落在「連線位址」，代表那個位址是平台的轉送節點，所有人會共用同一份
    額度。平台自己的健康檢查不經過 Cloudflare，出現在「連線位址」是正常的。"""
    forwarded = request.headers.get("CF-Connecting-IP")
    source = "CF-Connecting-IP" if forwarded else "連線位址"
    if source not in _ip_sources_seen:
        _ip_sources_seen.add(source)
        print(f"[rate-limit] 第一次收到 IP 取自{source}的請求：{request.method} {request.path}", flush=True)
    return forwarded or request.remote_addr or "unknown"


def _error(status: int, message: str, retry_after: float | None = None):
    response = jsonify({"error": message})
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
    return response


def install(app, *, llm_endpoints: set[str], upload_endpoints: set[str]) -> None:
    """掛上大小與頻率的檢查。

    llm_endpoints：會呼叫 LLM（要花錢）的路由，另外依 session 限次數。
    upload_endpoints：收檔案上傳的路由，大小上限用 MAX_UPLOAD_MB，其餘一律
    用 MAX_JSON_BODY_MB。"""
    max_json = config.MAX_JSON_BODY_MB * 1024 * 1024
    max_upload = config.MAX_UPLOAD_MB * 1024 * 1024
    # 全域上限取兩者較大的那個：沒有 Content-Length 的請求（分塊傳送）在讀取
    # 時由 Werkzeug 依這個值擋下（觸發下面的 413 處理）。有 Content-Length 的
    # 在 before_request 裡依路由各自比對。
    app.config["MAX_CONTENT_LENGTH"] = max(max_json, max_upload)

    per_ip = FixedWindowLimiter(config.RATE_LIMIT_PER_IP_PER_MINUTE, 60)
    login_per_ip = FixedWindowLimiter(config.RATE_LIMIT_LOGIN_PER_IP_PER_MINUTE, 60)
    llm_per_session = FixedWindowLimiter(config.RATE_LIMIT_LLM_PER_SESSION_PER_10_MINUTES, 600)
    app.extensions["request_limiters"] = {
        "per_ip": per_ip,
        "login_per_ip": login_per_ip,
        "llm_per_session": llm_per_session,
    }

    @app.before_request
    def _enforce_request_limits():
        # CORS 預檢沒有 body、處理成本極低，而且瀏覽器每個帶登入憑證的請求前
        # 都可能先送一次，算進去等於把額度打對折
        if request.method == "OPTIONS":
            return None

        limit = max_upload if request.endpoint in upload_endpoints else max_json
        if request.content_length is not None and request.content_length > limit:
            return _error(413, f"上傳的內容太大（上限 {limit // (1024 * 1024)} MB）")

        ip = client_ip()
        wait = per_ip.hit(ip)
        if wait is None and request.endpoint == "auth_login":
            wait = login_per_ip.hit(ip)
        if wait is None and request.endpoint in llm_endpoints:
            # 沒帶 token 的請求反正會被 require_auth 擋下，不用計數
            token = extract_bearer_token(request)
            if token:
                wait = llm_per_session.hit(token)
        if wait is not None:
            print(f"[rate-limit] 擋下過於頻繁的請求：{request.method} {request.endpoint}", flush=True)
            return _error(429, f"請求太頻繁，請稍候 {max(1, math.ceil(wait))} 秒再試", retry_after=wait)
        return None

    @app.errorhandler(413)
    def _handle_too_large(_exc):
        return _error(413, f"上傳的內容太大（上限 {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB）")
