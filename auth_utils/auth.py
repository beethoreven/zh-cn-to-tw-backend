"""
Google 登入驗證層。

負責驗證前端傳來的 Google ID Token——這不是 Service Account 憑證,
是使用者自己用 Google 帳號登入後,瀏覽器拿到的一段身分證明(JWT)。

驗證的三件事:
1. 這個 token 真的是 Google 簽發的(用 Google 的公開金鑰驗證簽章,
   google-auth 這個套件會自動處理金鑰快取/輪替,不用自己管)
2. 沒有過期
3. audience 對得上我們自己的 OAuth Client ID(不是別人專案的登入憑證)

另外比對 nonce(見下面 nonce_is_acceptable),確認這個 token 是送來的
這個 App 自己登入拿到的。

驗證通過後,回傳 token payload 裡「Google 保證過的」email;
只要上面任何一項不通過,一律回傳 None,呼叫端自己決定要回什麼錯誤,
這一層不負責判斷這個 email 在不在白名單裡(那是 whitelist.py 的事)。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from configs import config

_google_request = google_requests.Request()

# 舊版桌面殼送進登入網址的 nonce:16 bytes 亂數的十六進位(32 個字元)。殼
# 沒有留下這個值,登入時也不會送來。新版送的是原值的 SHA-256(base64url,
# 43 個字元),兩種格式長度不同,分得出來。
_LEGACY_NONCE = re.compile(r"[0-9a-f]{32}")
# 原值是殼產生的短字串(目前 32 個字元);長度上限只是擋掉明顯不是它的輸入
_MAX_RAW_NONCE_LENGTH = 256


def hashed_nonce(raw_nonce: str) -> str:
    """原值的 SHA-256,base64url、不帶「=」:桌面殼放進 Google 登入網址的
    就是這個值,Google 會原樣放進 ID Token 的 nonce 欄位。"""
    digest = hashlib.sha256(raw_nonce.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def nonce_is_acceptable(token_nonce, raw_nonce) -> bool:
    """
    ID Token 裡的 nonce(token_nonce)跟登入請求送來的原值(raw_nonce)對不對得上。

    桌面殼每次登入產生一個原值,只留在 App 裡;登入網址帶的是原值的
    SHA-256,登入時再把原值跟 ID Token 一起送來。token 裡只看得到雜湊,
    看不到原值,所以光是拿到一個 token(例如從瀏覽器或本機的回呼裡)的人
    湊不出原值,不能拿它來換 session。如果直接比對 token 裡的 nonce,
    擋不住這種情況:token 裡的值誰都讀得到,照抄送來就能通過。

    沒送原值時只放行兩種 token,都是還沒改成這套做法的用戶端:
    - nonce 是舊版桌面殼的格式(見 _LEGACY_NONCE)。
    - 沒有 nonce:瀏覽器版(Google Identity Services)沒有帶。
    新版桌面殼的 token 一定帶著雜湊格式的 nonce,沒有原值就不放行。
    """
    if raw_nonce is not None:
        if not isinstance(raw_nonce, str) or not raw_nonce or len(raw_nonce) > _MAX_RAW_NONCE_LENGTH:
            return False
        if not isinstance(token_nonce, str):
            return False
        # 兩邊都轉成 bytes 再比:compare_digest 收到含非 ASCII 字元的 str
        # 會丟 TypeError,token 裡的 nonce 不保證只有 ASCII
        return hmac.compare_digest(
            token_nonce.encode("utf-8"), hashed_nonce(raw_nonce).encode("ascii")
        )
    if token_nonce is None:
        return True
    return isinstance(token_nonce, str) and _LEGACY_NONCE.fullmatch(token_nonce) is not None


def verify_google_id_token(token: str | None, raw_nonce=None):
    """
    驗證 Google ID Token,回傳驗證過的 email(字串);驗證失敗回傳 None。

    raw_nonce 是登入請求一起送來的 nonce 原值(新版桌面殼才有),見
    nonce_is_acceptable。
    """
    if not token:
        return None

    try:
        payload = id_token.verify_oauth2_token(
            token, _google_request, config.GOOGLE_CLIENT_ID
        )
    except ValueError:
        # 格式錯誤、簽章驗證失敗、過期、audience 不符,都會是 ValueError
        return None

    if not payload.get("email_verified"):
        # Google 帳號本身的 email 沒有經過驗證(理論上很少見),不能信任
        return None

    if not nonce_is_acceptable(payload.get("nonce"), raw_nonce):
        # 只記哪一種情況,不記 token 或 nonce 的內容
        reason = "送來的原值對不上" if raw_nonce is not None else "沒送原值,token 也不是舊版用戶端的格式"
        print(f"[auth] 拒絕登入:nonce 檢查沒過({reason})", flush=True)
        return None

    return payload.get("email")


def extract_bearer_token(request):
    """
    從 `Authorization: Bearer <token>` header 取出 token 字串;
    header 沒帶或格式不對,回傳 None。
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header[len("Bearer "):].strip() or None
