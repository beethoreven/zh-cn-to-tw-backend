"""Request limits, DB-free public endpoints, and admin rules — through the real app.py."""
import json
import threading
import time

import harness
from harness import check

harness.reset_database()

import psycopg2  # noqa: E402

from request_limits.limits import FixedWindowLimiter  # noqa: E402

# ---------------------------------------------------------------- A. limiter unit tests
print("== A. FixedWindowLimiter")
now = [0.0]
lim = FixedWindowLimiter(3, 60, max_keys=4, clock=lambda: now[0])
check("first 3 hits allowed", [lim.hit("a") for _ in range(3)] == [None, None, None])
wait = lim.hit("a")
check("4th hit blocked with a wait time", wait is not None and 0 < wait <= 60, wait)
now[0] = 59.5
check("still blocked just before the window ends", lim.hit("a") is not None)
now[0] = 60.0
check("allowed again once the window has passed", lim.hit("a") is None)
for k in "bcd":
    lim.hit(k)
check("4 keys tracked", len(lim) == 4, len(lim))
lim.hit("e")
check("key count stays bounded when a 5th key arrives", len(lim) <= 4, len(lim))
now[0] = 200.0
for k in "fgh":
    lim.hit(k)
check("expired windows are evicted first", len(lim) <= 4, len(lim))

lim2 = FixedWindowLimiter(100, 60)
allowed = []


def worker():
    for _ in range(50):
        allowed.append(lim2.hit("same-key") is None)


threads = [threading.Thread(target=worker) for _ in range(10)]
for th in threads:
    th.start()
for th in threads:
    th.join()
check("exactly the limit is allowed under 10 concurrent threads", sum(allowed) == 100, sum(allowed))

# ---------------------------------------------------------------- real app
# Create the schema in this thread first, so the background threads that
# importing app.py starts never race the test setup on a brand-new database.
from db_utils import connection as _conn  # noqa: E402
_c, _ = _conn.get_ready_conn()
_c.close()
import app as backend_app  # noqa: E402
from admin_utils import users as admin_users  # noqa: E402
from auth_utils import sessions, whitelist  # noqa: E402
from db_utils import app_versions, job_store  # noqa: E402
from db_utils import connection as db_connection  # noqa: E402
from jobs import job_manager  # noqa: E402

client = backend_app.app.test_client()
ORIGIN = "zhcntotw://app"

admin_a = admin_users.create_user("Admin.A@Example.com", "A", 1, "active", actor_email="seed@test")
plain_user = admin_users.create_user("plain@example.com", "U", 2, "active", actor_email="seed@test")
token_a = sessions.create_session("admin.a@example.com")
token_u = sessions.create_session("plain@example.com")
check("create_user stores email in lowercase", admin_a["email"] == "admin.a@example.com", admin_a["email"])


def auth(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


# ---------------------------------------------------------------- B. request size
print("== B. request size limits")
big_json = b'{"pages": ["' + b"a" * (17 * 1024 * 1024) + b'"]}'
r = client.post("/api/jobs/from-ocr-text", data=big_json, content_type="application/json",
                headers={**auth(token_a), "CF-Connecting-IP": "203.0.113.10"})
check("17 MB JSON body -> 413", r.status_code == 413, r.status_code)
check("413 body is JSON with the limit", "16 MB" in (r.get_json() or {}).get("error", ""), r.data[:120])
check("413 carries the CORS header for the desktop origin", r.headers.get("Access-Control-Allow-Origin") == ORIGIN,
      dict(r.headers))

r = client.post("/api/jobs/direct-upload", data={"file": (__import__("io").BytesIO(b"a" * (20 * 1024 * 1024)), "big.txt")},
                content_type="multipart/form-data", headers={**auth(token_a), "CF-Connecting-IP": "203.0.113.11"})
check("20 MB upload to the upload endpoint is accepted", r.status_code == 202, (r.status_code, r.data[:200]))

r = client.post("/api/jobs/direct-upload", data={"file": (__import__("io").BytesIO(b"a" * (65 * 1024 * 1024)), "huge.txt")},
                content_type="multipart/form-data", headers={**auth(token_a), "CF-Connecting-IP": "203.0.113.12"})
check("65 MB upload -> 413", r.status_code == 413 and "64 MB" in (r.get_json() or {}).get("error", ""),
      (r.status_code, r.data[:120]))

# ---------------------------------------------------------------- C. rate limits
print("== C. rate limits")
statuses = [client.get("/api/health", headers={"CF-Connecting-IP": "198.51.100.1"}).status_code for _ in range(600)]
check("600 requests from one IP in a minute are all served", statuses.count(200) == 600, set(statuses))
r = client.get("/api/health", headers={"CF-Connecting-IP": "198.51.100.1", "Origin": ORIGIN,
                                       "X-Forwarded-For": "1.2.3.4"})
check("601st request -> 429", r.status_code == 429, r.status_code)
check("429 has Retry-After and a JSON error", r.headers.get("Retry-After", "").isdigit()
      and "請求太頻繁" in (r.get_json() or {}).get("error", ""), (dict(r.headers), r.data))
check("429 carries the CORS header", r.headers.get("Access-Control-Allow-Origin") == ORIGIN)
r = client.get("/api/health", headers={"CF-Connecting-IP": "198.51.100.1", "X-Forwarded-For": "9.9.9.9"})
check("changing X-Forwarded-For does not escape the limit", r.status_code == 429, r.status_code)
r = client.get("/api/health", headers={"CF-Connecting-IP": "198.51.100.2"})
check("another IP is unaffected", r.status_code == 200, r.status_code)

statuses = [client.options("/api/jobs/active", headers={"CF-Connecting-IP": "198.51.100.3", "Origin": ORIGIN,
                                                        "Access-Control-Request-Method": "GET"}).status_code
            for _ in range(700)]
check("700 CORS preflights are not counted", 429 not in statuses, set(statuses))
check("a real request after them still passes", client.get("/api/health", headers={"CF-Connecting-IP": "198.51.100.3"}).status_code == 200)

statuses = [client.post("/auth/login", json={"id_token": "x.y.z"}, headers={"CF-Connecting-IP": "198.51.100.4"}).status_code
            for _ in range(20)]
check("20 failed logins from one IP get 401", statuses == [401] * 20, set(statuses))
r = client.post("/auth/login", json={"id_token": "x.y.z"}, headers={"CF-Connecting-IP": "198.51.100.4"})
check("21st login from that IP -> 429", r.status_code == 429, r.status_code)

statuses = [client.post("/api/jobs/from-ocr-text", json={}, headers={**auth(token_a), "CF-Connecting-IP": "198.51.100.5"}).status_code
            for _ in range(30)]
check("30 Stage 1 starts in 10 minutes reach the view (400: no pages)", statuses == [400] * 30, set(statuses))
r = client.post("/api/jobs/from-ocr-text", json={}, headers={**auth(token_a), "CF-Connecting-IP": "198.51.100.5"})
check("31st LLM request on the same session -> 429", r.status_code == 429, r.status_code)
r = client.post("/api/jobs/1/review", json={}, headers={**auth(token_a), "CF-Connecting-IP": "198.51.100.6"})
check("other LLM endpoints share that session's budget", r.status_code == 429, r.status_code)
r = client.post("/api/jobs/from-ocr-text", json={}, headers={**auth(token_u), "CF-Connecting-IP": "198.51.100.5"})
check("another session is unaffected", r.status_code == 400, r.status_code)
r = client.post("/api/jobs/doesnotexist/pattern", json={}, headers={**auth(token_a), "CF-Connecting-IP": "198.51.100.7"})
check("non-LLM endpoints are not counted in that budget", r.status_code != 429, r.status_code)

# ---------------------------------------------------------------- D. public endpoints without DB
print("== D. public endpoints do not open DB connections")
connects = []
real_connect = psycopg2.connect


def counting_connect(*a, **k):
    connects.append(1)
    return real_connect(*a, **k)


db_connection.psycopg2.connect = counting_connect


def connections_for(fn):
    connects.clear()
    response = fn()
    return response, len(connects)


ip = {"CF-Connecting-IP": "192.0.2.50"}
r, n = connections_for(lambda: client.get("/auth/status", headers={**ip, "Authorization": "Bearer abc"}))
check("/auth/status with a malformed token: 401 and no DB connection", r.status_code == 401 and n == 0, (r.status_code, n))
r, n = connections_for(lambda: client.get("/auth/status", headers={**ip, "Authorization": "Bearer " + "0" * 128}))
check("/auth/status with a well-formed but unsigned token: 401 and no DB connection", r.status_code == 401 and n == 0, (r.status_code, n))
r, n = connections_for(lambda: client.get("/auth/status", headers={**ip, "Authorization": "Bearer " + "0" * 64}))
check("/auth/status with an old-format (unsigned 64-char) token: 401 and no DB connection", r.status_code == 401 and n == 0, (r.status_code, n))
_unknown = "3" * 64 + sessions._sign("3" * 64)
r, n = connections_for(lambda: client.get("/auth/status", headers={**ip, "Authorization": f"Bearer {_unknown}"}))
check("/auth/status with a correctly signed but unknown token does query (401)", r.status_code == 401 and n == 1, (r.status_code, n))
r, n = connections_for(lambda: client.get("/auth/status", headers={**ip, "Authorization": f"Bearer {token_a}"}))
check("/auth/status with a real session still works", r.status_code == 200 and r.get_json()["authorized"], r.data)
r, n = connections_for(lambda: client.post("/auth/logout", headers={**ip, "Authorization": "Bearer ' OR 1=1 --"}))
check("/auth/logout with a malformed token: 200 and no DB connection", r.status_code == 200 and n == 0, (r.status_code, n))
r, n = connections_for(lambda: client.post("/auth/login", json={"id_token": "a.b.c"}, headers=ip))
check("/auth/login with an invalid Google token: no DB connection", r.status_code == 401 and n == 0, (r.status_code, n))
r, n = connections_for(lambda: client.get("/api/health", headers=ip))
check("/api/health: no DB connection", n == 0, n)
r, n = connections_for(lambda: client.get("/api/ta-notice", headers=ip))
check("/api/ta-notice: no DB connection", r.status_code == 200 and n == 0, (r.status_code, n))

job_store._active_sources.append(lambda: ["job-a", "job-b"])
r, n = connections_for(lambda: client.get("/api/jobs/active", headers=ip))
check("/api/jobs/active counts in-memory jobs without the DB", r.get_json().get("active", -1) >= 2 and n == 0,
      (r.data, n))
job_store._active_sources.pop()

app_versions._cached_at = None
r = client.get("/api/version_check?os=macos&os_version=windows&major=1&minor=0", headers=ip)
check("an empty threshold table blocks nobody", r.get_json().get("force_update") is False, r.data)

conn = harness.raw_conn()
cur = conn.cursor()
cur.execute("INSERT INTO app_versions (os, os_version, latest_major, latest_minor, min_major, min_minor) "
            "VALUES ('macos', '11+', 2, 2, 2, 0), ('windows', 'default', 2, 2, 2, 1)")
conn.commit()
conn.close()
app_versions._cached_at = None
r, n = connections_for(lambda: client.get("/api/version_check?os=macos&os_version=11%2B&major=1&minor=9", headers=ip))
check("first version check loads the table once", n == 1 and r.get_json()["force_update"] is True, (n, r.data))
total = 0
for i in range(40):
    r, n = connections_for(lambda: client.get(
        f"/api/version_check?os=windows&os_version=probe{i}&major=1&minor=0", headers=ip))
    total += n
check("40 more checks with varying parameters: no DB connection", total == 0, total)
r, n = connections_for(lambda: client.get("/api/version_check?os=windows&os_version=default&major=2&minor=0", headers=ip))
check("cached thresholds still answer correctly", r.get_json().get("force_update") is True and n == 0, r.data)
for label, qs in [
    ("an unknown os/os_version combination", "os=macos&os_version=windows&major=9&minor=9"),
    ("a missing os_version", "os=macos&major=9&minor=9"),
    ("missing major/minor", "os=windows&os_version=default"),
    ("a non-integer version", "os=windows&os_version=default&major=x&minor=1"),
]:
    r = client.get(f"/api/version_check?{qs}", headers=ip)
    body = r.get_json()
    check(f"{label} is forced to update with status 200", r.status_code == 200 and body["force_update"] is True
          and body["min_version"] == "2.1", (r.status_code, r.data))
app_versions._cached_at = time.monotonic() - 3600
r, n = connections_for(lambda: client.get("/api/version_check?os=macos&os_version=11%2B&major=2&minor=2", headers=ip))
check("after the cache expires it reloads once", n == 1 and r.get_json()["force_update"] is False, (n, r.data))
app_versions._cached_at = time.monotonic() - 3600
real_load = app_versions._load_all
app_versions._load_all = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
r = client.get("/api/version_check?os=macos&os_version=11%2B&major=1&minor=0", headers=ip)
check("if reloading fails, the previous thresholds are used", r.status_code == 200 and r.get_json()["force_update"] is True,
      (r.status_code, r.data))
failing_calls = []
def failing_load():
    failing_calls.append(1)
    raise RuntimeError("db down")
app_versions._cached_at = time.monotonic() - 3600
app_versions._load_all = failing_load
for _ in range(5):
    client.get("/api/version_check?os=macos&os_version=11%2B&major=1&minor=0", headers=ip)
check("while the DB is down, only one reload attempt per cache window", len(failing_calls) == 1, len(failing_calls))

# two reloads overlap: the one that started first finishes last and must not overwrite the newer one
import threading as _threading
order = []
first_may_finish = _threading.Event()
def ordered_load():
    n = len(order)
    order.append(n)
    if n == 0:
        first_may_finish.wait(5)
        return {("macos", "11+"): {"latest_major": 2, "latest_minor": 0, "min_major": 1, "min_minor": 0}}
    return {("macos", "11+"): {"latest_major": 2, "latest_minor": 3, "min_major": 2, "min_minor": 3}}
app_versions._cached_at = time.monotonic() - 3600
app_versions._load_all = ordered_load
slow_result = []
t = _threading.Thread(target=lambda: slow_result.append(app_versions.get_policies()[("macos", "11+")]))
t.start()
for _ in range(500):
    if order:
        break
    time.sleep(0.01)
time.sleep(0.01)
fast = app_versions.get_policies()[("macos", "11+")]
first_may_finish.set()
t.join(5)
check("overlapping reloads: the later-started load wins", app_versions._cached_policies[("macos", "11+")]["min_minor"] == 3
      and fast["min_minor"] == 3, app_versions._cached_policies)
check("the slow, older load returns the newer cached answer instead of its own", slow_result and slow_result[0]["min_minor"] == 3,
      slow_result)
app_versions._cached_at = None
app_versions._load_all = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
r = client.get("/api/version_check?os=macos&os_version=windows&major=1&minor=0", headers=ip)
check("thresholds never loaded: not blocked", r.status_code == 200 and r.get_json().get("force_update") is False,
      (r.status_code, r.data))
app_versions._load_all = real_load
db_connection.psycopg2.connect = real_connect

# ---------------------------------------------------------------- E. admin by role id, emails, last admin
print("== E. admin rules")
ip = {"CF-Connecting-IP": "192.0.2.60"}


def status_of(token, email):
    whitelist.invalidate_cache(email)
    return client.get("/auth/status", headers={**ip, "Authorization": f"Bearer {token}"}).get_json()


check("role id 1 is admin", status_of(token_a, "admin.a@example.com")["is_admin"] is True)
check("role id 2 is not admin", status_of(token_u, "plain@example.com")["is_admin"] is False)
r = client.put("/admin/permissions/2", json={"role": "admin", "allowed_haiku": 10}, headers={**auth(token_a), **ip})
check("renaming role 2 to 'admin' succeeds", r.status_code == 200, r.data)
check("...but its users do not become admins", status_of(token_u, "plain@example.com")["is_admin"] is False)
r = client.put("/admin/permissions/1", json={"role": "管理員", "allowed_haiku": 999999}, headers={**auth(token_a), **ip})
check("renaming role 1 does not demote its users", r.status_code == 200
      and status_of(token_a, "admin.a@example.com")["is_admin"] is True)
r = client.get("/admin/users", headers={**auth(token_u), **ip})
check("a non-admin still gets 401 from admin endpoints", r.status_code == 401, r.status_code)

r = client.post("/admin/users", json={"email": "  New.User@Example.COM ", "name": "N", "role": 2, "status": "active"},
                headers={**auth(token_a), **ip})
check("admin-created email is stored lowercase", r.status_code == 201 and r.get_json()["email"] == "new.user@example.com", r.data)
r = client.post("/admin/users", json={"email": "NEW.USER@example.com", "name": "N2", "role": 2, "status": "active"},
                headers={**auth(token_a), **ip})
check("same email in different case is rejected", r.status_code == 400, (r.status_code, r.data))

whitelist.get_user_auth_info("New.User@EXAMPLE.com")
check("cache keys are normalised", "new.user@example.com" in whitelist._cache and "New.User@EXAMPLE.com" not in whitelist._cache)
whitelist.invalidate_cache("NEW.USER@example.com")
check("invalidating with another case clears the entry", "new.user@example.com" not in whitelist._cache)

aid = admin_a["id"]


def put_user(uid, **fields):
    current = admin_users.get_user(uid)
    body = {"email": current["email"], "name": current["name"], "role": current["role"], "status": current["status"]}
    body.update(fields)
    return client.put(f"/admin/users/{uid}", json=body, headers={**auth(token_a), **ip})


r = put_user(aid, role=2)
check("demoting the only admin -> 400", r.status_code == 400 and "最後一位" in r.get_json()["error"], r.data)
r = put_user(aid, status="deactive")
check("deactivating the only admin -> 400", r.status_code == 400, r.data)
r = put_user(aid, name="A renamed")
check("editing the only admin's name is fine", r.status_code == 200 and r.get_json()["changed"] is True, r.data)
check("the only admin is still an active admin", admin_users.get_user(aid)["role"] == 1 and admin_users.get_user(aid)["status"] == "active")
r = client.put("/admin/users/99999", json={"email": "x@example.com", "name": "X", "role": 2, "status": "active"}, headers={**auth(token_a), **ip})
check("unknown user is still 404", r.status_code == 404, r.status_code)

admin_b = admin_users.create_user("b@example.com", "B", 1, "active", actor_email="seed@test")
r = put_user(aid, role=2)
check("with a second admin, demoting the first works", r.status_code == 200, r.data)
r = client.put(f"/admin/users/{admin_b['id']}", json={"email": "b@example.com", "name": "B", "role": 2, "status": "active"},
               headers={**auth(sessions.create_session("b@example.com")), **ip})
check("then the remaining admin cannot demote themself", r.status_code == 400, r.data)

# concurrent demotion of the last two admins
admin_users.update_user(aid, "admin.a@example.com", "A renamed", 1, "active", actor_email="seed@test")  # two admins again: A and B
conn1 = harness.raw_conn()
c1 = conn1.cursor()
c1.execute("SELECT id FROM users WHERE role = 1 AND status = 'active' FOR UPDATE")
c1.execute("UPDATE users SET role = 2 WHERE id = %s", (aid,))
outcome = {}


def demote_b():
    try:
        admin_users.update_user(admin_b["id"], "b@example.com", "B", 2, "active", actor_email="seed@test")
        outcome["result"] = "demoted"
    except admin_users.LastAdminError:
        outcome["result"] = "LastAdminError"


th = threading.Thread(target=demote_b)
th.start()
time.sleep(1.0)
check("a concurrent demotion waits for the other transaction", th.is_alive())
conn1.commit()
conn1.close()
th.join(10)
check("...and then refuses, leaving one admin", outcome.get("result") == "LastAdminError", outcome)
check("exactly one active admin remains", sum(1 for u in admin_users.list_users() if u["role"] == 1 and u["status"] == "active") == 1)

mixed = admin_users.create_user("Mixed@Example.com", "M", 2, "active", actor_email="seed@test")
old_token = sessions.create_session("MIXED@example.com")  # a session stored with Google's original casing
check("old mixed-case session resolves", sessions.resolve_session(old_token) == "MIXED@example.com")
r = put_user(mixed["id"], status="deactive")
check("deactivating revokes the differently-cased session", r.status_code == 200 and sessions.resolve_session(old_token) is None,
      r.data)

case_user = admin_users.create_user("case@example.com", "C", 2, "active", actor_email="seed@test")
case_token = sessions.create_session("case@example.com")
job_id = job_manager.create_job(original_filename="x.txt", user_email="Case@Example.com")
r = client.get(f"/api/jobs/{job_id}", headers={**auth(case_token), **ip})
check("job ownership ignores email case", r.status_code == 200, (r.status_code, r.data))
r = client.get(f"/api/jobs/{job_id}", headers={**auth(token_u), **ip})
check("someone else still gets 404", r.status_code == 404, r.status_code)

# ---------------------------------------------------------------- F. which source the client IP came from
print("== F. client IP source log")
import contextlib, io
from request_limits import limits as request_limits_module
request_limits_module._ip_sources_seen.clear()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    client.get("/api/health")
    client.get("/api/health")
    client.get("/api/health", headers={"CF-Connecting-IP": "203.0.113.77"})
    client.get("/api/health", headers={"CF-Connecting-IP": "203.0.113.78"})
lines = [line for line in buf.getvalue().splitlines() if "IP 取自" in line]
check("one log line per IP source, not per request", len(lines) == 2, lines)
check("the lines name the source and path", any("連線位址" in l and "/api/health" in l for l in lines)
      and any("CF-Connecting-IP" in l for l in lines), lines)
check("no IP address is logged", not any("203.0.113" in l or "127.0.0.1" in l for l in lines), lines)

ok = harness.summary("limits/admin")
raise SystemExit(0 if ok else 1)
