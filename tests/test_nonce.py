"""
Tests for the nonce check in zh-cn-to-tw-backend/auth_utils/auth.py.

Runs the real google-auth verification path (signature, expiry, audience,
issuer) against tokens signed by a locally generated RSA key: the module's
_google_request is replaced by a fake that serves our own certificate instead
of Google's, so nothing goes over the network and no real account is used.
"""
import base64
import datetime
import hashlib
import json
import os
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_ID = "test-client.apps.googleusercontent.com"

# configs.config calls load_dotenv(), which walks up from the working directory and
# would pick up a developer's .env (production secrets); disable it before importing.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False
os.environ["GOOGLE_CLIENT_ID"] = CLIENT_ID
sys.path.insert(0, BACKEND)

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from google.auth import crypt, jwt  # noqa: E402

from auth_utils import auth  # noqa: E402
from configs import config  # noqa: E402

assert config.GOOGLE_CLIENT_ID == CLIENT_ID, "test must not pick up the real client id"

KID = "test-kid"
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
now_dt = datetime.datetime.now(datetime.timezone.utc)
cert = (
    x509.CertificateBuilder()
    .subject_name(name).issuer_name(name).public_key(key.public_key())
    .serial_number(1)
    .not_valid_before(now_dt - datetime.timedelta(days=1))
    .not_valid_after(now_dt + datetime.timedelta(days=1))
    .sign(key, hashes.SHA256())
)
CERTS = {KID: cert.public_bytes(serialization.Encoding.PEM).decode()}
PRIVATE_PEM = key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)


class FakeResponse:
    status = 200
    headers = {}
    data = json.dumps(CERTS).encode()


class FakeRequest:
    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        return FakeResponse()


auth._google_request = FakeRequest()
SIGNER = crypt.RSASigner.from_string(PRIVATE_PEM, key_id=KID)


def make_token(nonce="__omit__", exp_offset=3600, email="someone@example.com"):
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": "1234567890",
        "email": email,
        "email_verified": True,
        "iat": now,
        "exp": now + exp_offset,
    }
    if nonce != "__omit__":
        payload["nonce"] = nonce
    return jwt.encode(SIGNER, payload).decode()


def independent_hash(raw):
    # Written separately from the module under test on purpose.
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()).decode().rstrip("=")


results = []


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(("PASS" if ok else "FAIL"), label, "->", repr(got) if not ok else "")


RAW = "0123456789abcdef0123456789abcdef"
NEW = independent_hash(RAW)
LEGACY = "5f2b9c0e7d1a4b3c8e6f0a1b2c3d4e5f"
EMAIL = "someone@example.com"

check("hash format is 43-char base64url", (len(NEW), "=" in NEW), (43, False))
check("hashed_nonce matches an independent implementation", auth.hashed_nonce(RAW), NEW)

# New desktop shell
check("new client, correct raw nonce -> accepted", auth.verify_google_id_token(make_token(NEW), RAW), EMAIL)
check("new client, wrong raw nonce -> rejected", auth.verify_google_id_token(make_token(NEW), RAW[:-1] + "0"), None)
check("new client token replayed without raw -> rejected", auth.verify_google_id_token(make_token(NEW), None), None)
check("new client token replayed with the token's own nonce as raw -> rejected",
      auth.verify_google_id_token(make_token(NEW), NEW), None)

# Old clients during the transition
check("legacy desktop (32 hex), no raw -> accepted", auth.verify_google_id_token(make_token(LEGACY), None), EMAIL)
check("legacy desktop with a raw nonce supplied -> rejected", auth.verify_google_id_token(make_token(LEGACY), RAW), None)
check("no nonce claim, no raw -> rejected", auth.verify_google_id_token(make_token(), None), None)
check("nonce claim explicitly null, no raw -> rejected", auth.verify_google_id_token(make_token(None), None), None)
check("empty nonce claim, no raw -> rejected", auth.verify_google_id_token(make_token(""), None), None)
check("nonce claim as a number, no raw -> rejected", auth.verify_google_id_token(make_token(123), None), None)
check("no nonce claim but raw supplied -> rejected", auth.verify_google_id_token(make_token(), RAW), None)
check("uppercase hex is not the legacy format -> rejected",
      auth.verify_google_id_token(make_token(LEGACY.upper()), None), None)
check("33 hex chars is not the legacy format -> rejected",
      auth.verify_google_id_token(make_token(LEGACY + "a"), None), None)
check("legacy prefix + newline is not the legacy format -> rejected",
      auth.verify_google_id_token(make_token(LEGACY + "\n"), None), None)

# Odd inputs never crash
check("raw nonce as a number -> rejected", auth.verify_google_id_token(make_token(NEW), 123), None)
check("raw nonce as a list -> rejected", auth.verify_google_id_token(make_token(NEW), [RAW]), None)
check("empty raw nonce -> rejected", auth.verify_google_id_token(make_token(NEW), ""), None)
check("raw nonce over the length limit -> rejected", auth.verify_google_id_token(make_token(NEW), "a" * 257), None)
check("non-ASCII nonce claim with raw -> rejected without exception",
      auth.verify_google_id_token(make_token("非ASCII的nonce"), RAW), None)
check("nonce claim that is a number, no raw -> rejected", auth.verify_google_id_token(make_token(12345), None), None)

# The existing checks still apply
check("expired token with correct raw -> rejected",
      auth.verify_google_id_token(make_token(NEW, exp_offset=-7200), RAW), None)
check("tampered signature -> rejected",
      auth.verify_google_id_token(make_token(NEW)[:-4] + "AAAA", RAW), None)
check("empty token -> rejected", auth.verify_google_id_token("", RAW), None)

print(f"\n{sum(results)}/{len(results)} passed")
if __name__ == "__main__":
    sys.exit(0 if all(results) else 1)
