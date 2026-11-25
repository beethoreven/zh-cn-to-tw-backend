"""Login sessions: signed tokens, hashed storage, idle and absolute expiry, log out
everywhere — through the real app.py."""
import itertools
from datetime import datetime, timedelta, timezone

import harness
from harness import check

harness.reset_database()

from db_utils import connection as _conn  # noqa: E402

_c, _ = _conn.get_ready_conn()
_c.close()
import app as backend_app  # noqa: E402
from admin_utils import users as admin_users  # noqa: E402
from auth_utils import sessions  # noqa: E402

client = backend_app.app.test_client()
ORIGIN = "zhcntotw://app"
_ips = itertools.count(1)


def hdr(token=None):
    n = next(_ips)
    h = {"Origin": ORIGIN, "CF-Connecting-IP": f"198.51.{n // 250}.{n % 250 + 1}"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def db(query, params=()):
    conn = harness.raw_conn()
    cur = conn.cursor()
    cur.execute(query, params)
    out = cur.fetchall() if cur.description else None
    conn.commit()
    conn.close()
    return out


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


admin_users.create_user("a@example.com", "A", 1, "active", actor_email="seed@test")
admin_users.create_user("b@example.com", "B", 2, "active", actor_email="seed@test")

# ---------------------------------------------------------------- A. hashed storage
print("== A. only the hash is stored")
tok = sessions.create_session("a@example.com")
stored = db("SELECT token_hash, email FROM login_sessions")
check("one row stored", len(stored) == 1, stored)
check("the stored value is not the token", stored[0][0] != tok)
check("the stored value is its SHA-256", stored[0][0] == sessions.hash_token(tok))
check("no column anywhere holds the token",
      not db("SELECT 1 FROM login_sessions WHERE token_hash = %s OR email = %s", (tok, tok)))
check("token resolves", sessions.resolve_session(tok) == "a@example.com")
check("the stored hash itself does not work as a token", sessions.resolve_session(stored[0][0]) is None)
r = client.get("/auth/status", headers=hdr(tok))
check("/auth/status accepts the token", r.status_code == 200 and r.get_json()["email"] == "a@example.com", r.get_json())

# ---------------------------------------------------------------- B. expiry
print("== B. idle and absolute expiry")
fresh_old = sessions.create_session("a@example.com")
db("UPDATE login_sessions SET created_at = %s WHERE token_hash = %s",
   (ago(sessions.SESSION_MAX_DAYS + 1), sessions.hash_token(fresh_old)))
check("in daily use but older than the absolute limit: rejected", sessions.resolve_session(fresh_old) is None)
check("...and its row deleted",
      not db("SELECT 1 FROM login_sessions WHERE token_hash = %s", (sessions.hash_token(fresh_old),)))

young = sessions.create_session("a@example.com")
db("UPDATE login_sessions SET created_at = %s WHERE token_hash = %s",
   (ago(sessions.SESSION_MAX_DAYS - 1), sessions.hash_token(young)))
check("just inside the absolute limit: still works", sessions.resolve_session(young) == "a@example.com")

idle = sessions.create_session("a@example.com")
db("UPDATE login_sessions SET last_seen_at = %s WHERE token_hash = %s",
   (ago(sessions.SESSION_IDLE_DAYS + 1), sessions.hash_token(idle)))
check("idle too long: rejected", sessions.resolve_session(idle) is None)

# cleanup loop: one idle, one too old, one fine
c_idle = sessions.create_session("b@example.com")
c_old = sessions.create_session("b@example.com")
c_ok = sessions.create_session("b@example.com")
db("UPDATE login_sessions SET last_seen_at = %s WHERE token_hash = %s",
   (ago(sessions.SESSION_IDLE_DAYS + 1), sessions.hash_token(c_idle)))
db("UPDATE login_sessions SET created_at = %s WHERE token_hash = %s",
   (ago(sessions.SESSION_MAX_DAYS + 1), sessions.hash_token(c_old)))
deleted = sessions.delete_idle_sessions()
left = {r[0] for r in db("SELECT token_hash FROM login_sessions WHERE email = 'b@example.com'")}
check("cleanup deletes both the idle and the too-old session", deleted == 2, deleted)
check("cleanup keeps the live one", left == {sessions.hash_token(c_ok)}, left)

# ---------------------------------------------------------------- C. log out everywhere
print("== C. log out everywhere")
db("DELETE FROM login_sessions")
a1 = sessions.create_session("a@example.com")
a2 = sessions.create_session("a@example.com")
a_mixed = sessions.create_session("A@Example.com")  # a session stored with different casing
b1 = sessions.create_session("b@example.com")
r = client.post("/auth/logout-all", headers=hdr())
check("no token: 401", r.status_code == 401)
r = client.post("/auth/logout-all", headers=hdr("f" * 64))
check("unknown token: 401", r.status_code == 401)
r = client.post("/auth/logout-all", headers=hdr(a1))
check("200 with the revoked count", r.status_code == 200 and r.get_json() == {"status": "ok", "revoked": 3}, r.get_json())
check("the calling device is logged out", sessions.resolve_session(a1) is None)
check("the other device is logged out", sessions.resolve_session(a2) is None)
check("the differently-cased session is logged out", sessions.resolve_session(a_mixed) is None)
check("another user is untouched", sessions.resolve_session(b1) == "b@example.com")
r = client.get("/auth/status", headers=hdr(a2))
check("/auth/status on a revoked device: 401", r.status_code == 401)

# deactivated account can still revoke its own sessions
d = admin_users.create_user("d@example.com", "D", 2, "active", actor_email="seed@test")
d_tok = sessions.create_session("d@example.com")
db("UPDATE users SET status = 'deactive' WHERE id = %s", (d["id"],))
from auth_utils import whitelist  # noqa: E402

whitelist.invalidate_cache("d@example.com")
r = client.post("/auth/logout-all", headers=hdr(d_tok))
check("a deactivated account can still log out everywhere", r.status_code == 200 and r.get_json()["revoked"] == 1, r.get_json())

# ---------------------------------------------------------------- D. signature
print("== D. signed tokens")
real = sessions.create_session("a@example.com")
check("token is 128 lowercase hex characters", len(real) == 128 and all(c in "0123456789abcdef" for c in real))
check("signed token resolves", sessions.resolve_session(real) == "a@example.com")
tampered = real[:64] + ("0" if real[64] != "0" else "1") + real[65:]
check("changing one signature character: rejected", sessions.resolve_session(tampered) is None)
other = ("0" if real[0] != "0" else "1") + real[1:]
check("changing one random character: rejected", sessions.resolve_session(other) is None)
check("old unsigned 64-character token: rejected", sessions.resolve_session(real[:64]) is None)
# a forged token whose hash IS in the table is still rejected: the signature check comes first
forged = "4" * 128
db("INSERT INTO login_sessions VALUES (%s, 'a@example.com', %s, %s)",
   (sessions.hash_token(forged), datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
check("unsigned token is rejected even if its hash were in the table", sessions.resolve_session(forged) is None)
r = client.post("/auth/logout-all", headers=hdr(forged))
check("logout-all with an unsigned token: 401", r.status_code == 401)

# ---------------------------------------------------------------- E. logout of one device
print("== E. single logout")
e1 = sessions.create_session("b@example.com")
e2 = sessions.create_session("b@example.com")
r = client.post("/auth/logout", headers=hdr(e1))
check("logout 200", r.status_code == 200)
check("that device is logged out", sessions.resolve_session(e1) is None)
check("the other device stays logged in", sessions.resolve_session(e2) == "b@example.com")

# ---------------------------------------------------------------- F. missing key
print("== F. missing signing key")
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

for label, key in [("missing", None), ("too short", "short")]:
    env = {k: v for k, v in os.environ.items() if k != "SESSION_SIGNING_KEY"}
    if key is not None:
        env["SESSION_SIGNING_KEY"] = key
    # the child process must not load a developer's real .env either (it holds production
    # secrets, and its SESSION_SIGNING_KEY would make this check pass for the wrong reason)
    child = "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import auth_utils.sessions"
    p = subprocess.run([sys.executable, "-c", child], cwd=harness.BACKEND, env=env, capture_output=True, text=True)
    check(f"signing key {label}: the backend refuses to start", p.returncode != 0 and "SESSION_SIGNING_KEY" in p.stderr,
          p.stderr[-200:])

raise SystemExit(0 if harness.summary("sessions") else 1)
