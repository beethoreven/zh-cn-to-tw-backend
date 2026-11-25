"""
應用程式自己的 session token 層——跟 Google ID Token 完全脫鉤。

背景：Google ID Token 效期固定約 1 小時。早期實作直接把它當成前端打
每一支 API 都要帶的長效憑證，等於整個工作階段每小時就會被打斷一次，
逼使用者重新登入，長時間的 Stage 1/校對（尤其遇到額度限制、重試+
指數退避時）常常一小時內跑不完，導致算好的結果因為登入過期而存不下來
——市面上正常「用 Google 登入」的產品不是這樣做的：Google 只在登入
當下驗證一次身份，之後靠應用程式自己簽發、自己管理生命週期的 session
才是真正撐住整個使用期間的憑證，這裡補上這一層。

設計：
- token 是隨機不透明字串（secrets.token_hex，不是 JWT），前端不需要、
  也不應該嘗試解析它。資料庫只存它的 SHA-256（login_sessions 表的
  token_hash），不存原文：資料庫備份或查詢結果外流時，拿到的雜湊不能
  直接拿來登入。token 本身有 256 位元的亂數，不需要加鹽或慢雜湊——那是
  給人選的、猜得到的密碼用的。
- 絕對效期：不管多常用，建立超過 SESSION_MAX_DAYS 天就失效、要重新
  登入一次。token 被偷走的話，對方能用的時間有上限，不會因為一直在用
  就永遠有效。
- 「滑動式」有效期：每次成功驗證都會把 last_seen_at 更新成現在，只要
  使用者還有在用（不管多久用一次），session 就不會過期；連續
  SESSION_IDLE_DAYS 天完全沒有任何請求才會真的過期——這是合理的
  「太久沒用」邊界，不是工作到一半被打斷。
- 撤銷（管理員停用帳號）完全不靠這一層：auth_utils/whitelist.py 的
  is_permitted_user() 在每支 require_auth 保護的路由都會即時查（30 秒
  快取），跟 session token 本身是否還「有效」是兩件事——session 有效
  只代表「這確實是我們自己發出去、還沒過期的憑證」，不代表這個帳號
  現在還有權限。兩層各司其職，管理員後台停用帳號一樣是 30 秒內生效，
  不會因為多了這一層而變慢或被繞過。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from db_utils.connection import get_ready_conn, sql

SESSION_IDLE_DAYS = int(os.environ.get("SESSION_IDLE_DAYS", "90"))
SESSION_MAX_DAYS = int(os.environ.get("SESSION_MAX_DAYS", "180"))

# last_seen_at 沒必要每次請求都真的寫一次 DB——前端輪詢 job/review 進度
# 每 5 秒打一次 API，不節流的話光是「順便更新一下時間戳記」就會把輪詢
# 拖垮。跟 whitelist.py 的讀取快取是同樣的考量，這裡反過來對寫入做節流。
#
# 當初的量化理由是「Neon 單次連線+查詢實測 1.2-1.6 秒」，那個數字在
# 2026-08-25 之後不成立了（跨區造成，同區後約 30ms，見
# db_utils/connection.py 教訓四）。節流保留著——它同時也在省 Neon 免費
# 方案的寫入量——但不要再拿那個過期數字當理由，要調之前先量現況。
_TOUCH_MIN_INTERVAL_SECONDS = 60

# token = 64 個十六進位字元的亂數 + 64 個十六進位字元的簽章（用 SESSION_SIGNING_KEY
# 對亂數算 HMAC-SHA256），共 128 個小寫十六進位字元。
#
# 簽章不對的一定不是我們發的，直接當成無效，不必開資料庫連線去查：
# /auth/status、/auth/logout 不需要登入就能呼叫，以前只檢查格式時，任何人
# 照格式隨便湊一串就能讓後端開一條連線查一次，而同時連線上限只有 5（見
# db_utils/connection.py）。有了簽章，沒有金鑰就湊不出會被拿去查的 token。
#
# 換掉 SESSION_SIGNING_KEY 等於讓所有人登出（舊 token 的簽章全部對不上）；
# 金鑰外流時這正是要的效果。早期只有亂數、沒有簽章的 64 字元 token 也因此
# 一律無效，用戶端收到 401 會自己重新登入一次。
_SIGNING_KEY = os.environ.get("SESSION_SIGNING_KEY", "")
if len(_SIGNING_KEY) < 32:
    # 沒設金鑰就不啟動：寧可部署失敗（Render 會繼續用舊版服務），也不要用
    # 空金鑰或很短的金鑰簽出任何人都能偽造的 token
    raise RuntimeError("SESSION_SIGNING_KEY 沒有設定或少於 32 個字元，請在環境變數設定一組隨機字串")
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{128}")


def _sign(nonce_hex: str) -> str:
    return hmac.new(_SIGNING_KEY.encode("utf-8"), nonce_hex.encode("ascii"), hashlib.sha256).hexdigest()


def _is_well_formed(token: str | None) -> bool:
    """格式對、而且簽章是用我們的金鑰簽的。"""
    if not token or _TOKEN_PATTERN.fullmatch(token) is None:
        return False
    return hmac.compare_digest(token[64:], _sign(token[:64]))


def hash_token(token: str) -> str:
    """資料庫裡存、查的都是這個值，不是 token 原文。"""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


_touch_lock = threading.Lock()
_last_touched: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(email: str) -> str:
    """驗證過 Google ID Token、確認過白名單之後才呼叫——這裡不重複做
    這兩件事，呼叫端（/auth/login）負責。"""
    nonce_hex = secrets.token_hex(32)
    token = nonce_hex + _sign(nonce_hex)
    now = _now_iso()
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql(
                "INSERT INTO login_sessions (token_hash, email, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?)"
            ),
            (hash_token(token), email, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def resolve_session(token: str | None) -> str | None:
    """token 有效（存在、沒有閒置太久、也沒有超過絕對效期）就回傳對應的
    email，並視情況更新 last_seen_at；無效回傳 None，逾期的話順便刪掉
    這筆記錄。"""
    if not _is_well_formed(token):
        return None

    token_hash = hash_token(token)
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("SELECT email, created_at, last_seen_at FROM login_sessions WHERE token_hash = ?"),
            (token_hash,),
        )
        row = cur.fetchone()
        if row is None:
            return None

        email, created_at, last_seen_at = row
        now = datetime.now(timezone.utc)
        idle_days = (now - datetime.fromisoformat(last_seen_at)).total_seconds() / 86400
        age_days = (now - datetime.fromisoformat(created_at)).total_seconds() / 86400
        if idle_days > SESSION_IDLE_DAYS or age_days > SESSION_MAX_DAYS:
            cur.execute(sql("DELETE FROM login_sessions WHERE token_hash = ?"), (token_hash,))
            conn.commit()
            return None

        _maybe_touch(cur, conn, token_hash)
        return email
    finally:
        conn.close()


def delete_idle_sessions() -> int:
    """刪掉超過 SESSION_IDLE_DAYS 天沒用過、或建立超過 SESSION_MAX_DAYS 天
    的 session，回傳刪掉的筆數。
    resolve_session 只會在同一個 token 再被拿出來用時順便刪掉逾期的那筆，
    但換電腦、清掉資料、重裝 App 之後，舊 token 永遠不會再出現，只能靠
    這裡定期清（app.py 啟動時的背景清理迴圈每小時呼叫一次）。

    created_at、last_seen_at 一律由 _now_iso() 寫入（UTC、同一種 ISO 格式），
    所以直接比字串就等同比時間，跟 db_utils/job_store.py 的 delete_expired
    同一招。"""
    now = datetime.now(timezone.utc)
    idle_cutoff = (now - timedelta(days=SESSION_IDLE_DAYS)).isoformat()
    age_cutoff = (now - timedelta(days=SESSION_MAX_DAYS)).isoformat()
    conn, cur = get_ready_conn()
    try:
        cur.execute(
            sql("DELETE FROM login_sessions WHERE last_seen_at < ? OR created_at < ?"),
            (idle_cutoff, age_cutoff),
        )
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def _maybe_touch(cur, conn, token_hash: str) -> None:
    now = time.time()
    with _touch_lock:
        last = _last_touched.get(token_hash, 0)
        if now - last < _TOUCH_MIN_INTERVAL_SECONDS:
            return
        _last_touched[token_hash] = now
    cur.execute(
        sql("UPDATE login_sessions SET last_seen_at = ? WHERE token_hash = ?"),
        (_now_iso(), token_hash),
    )
    conn.commit()


def delete_sessions_for_email(cur, email: str) -> int:
    """刪掉這個 email 名下所有 session，回傳刪掉幾筆。

    管理員把帳號停用時（見 admin_utils/users.py 的 update_user）、使用者
    按「登出所有裝置」時（見 delete_all_sessions_for_email）呼叫。
    刻意收 cursor 而不是自己開連線：這樣刪除跟「把 status 改成
    deactive」那句 UPDATE 在同一個交易裡，不會出現「帳號已停用但
    session 還在」或反過來的中間狀態，也少開一條連線。

    email 用 LOWER() 比對，跟 whitelist.py 查授權時同一個規則——
    users 表裡的大小寫不保證跟 Google 回傳的 email claim 一致，
    直接等號比對會漏刪，而漏刪的後果正是這支函式要防的事。
    """
    cur.execute(
        sql("DELETE FROM login_sessions WHERE LOWER(email) = LOWER(?) RETURNING token_hash"),
        (email,),
    )
    tokens = [row[0] for row in cur.fetchall()]
    # 順手把節流字典裡對應的 token 清掉。不清也不會錯（那些 token 之後
    # 永遠查不到 session，resolve_session 一律回 None），純粹是不要讓
    # 這個字典累積永遠用不到的鍵。
    if tokens:
        with _touch_lock:
            for t in tokens:
                _last_touched.pop(t, None)
    return len(tokens)


def delete_all_sessions_for_email(email: str) -> int:
    """「登出所有裝置」：這個帳號在每一台電腦上的登入都失效。token 外流、
    電腦遺失或借給別人用過時，使用者自己就能把所有登入一次撤掉，不用等
    管理員停用帳號。"""
    conn, cur = get_ready_conn()
    try:
        count = delete_sessions_for_email(cur, email)
        conn.commit()
        return count
    finally:
        conn.close()


def delete_session(token: str | None) -> None:
    if not _is_well_formed(token):
        return
    token_hash = hash_token(token)
    conn, cur = get_ready_conn()
    try:
        cur.execute(sql("DELETE FROM login_sessions WHERE token_hash = ?"), (token_hash,))
        conn.commit()
    finally:
        conn.close()
    with _touch_lock:
        _last_touched.pop(token_hash, None)
