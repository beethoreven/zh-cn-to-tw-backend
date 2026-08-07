"""
Google 登入驗證層。

負責驗證前端傳來的 Google ID Token——這不是 Service Account 憑證,
是使用者自己用 Google 帳號登入後,瀏覽器拿到的一段身分證明(JWT)。

驗證的三件事:
1. 這個 token 真的是 Google 簽發的(用 Google 的公開金鑰驗證簽章,
   google-auth 這個套件會自動處理金鑰快取/輪替,不用自己管)
2. 沒有過期
3. audience 對得上我們自己的 OAuth Client ID(不是別人專案的登入憑證)

驗證通過後,回傳 token payload 裡「Google 保證過的」email;
只要上面任何一項不通過,一律回傳 None,呼叫端自己決定要回什麼錯誤,
這一層不負責判斷這個 email 在不在白名單裡(那是 whitelist.py 的事)。
"""

from __future__ import annotations

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from configs import config

_google_request = google_requests.Request()


def verify_google_id_token(token: str | None):
    """
    驗證 Google ID Token,回傳驗證過的 email(字串);驗證失敗回傳 None。
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
