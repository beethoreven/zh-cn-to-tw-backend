"""Admin audit log — through the real app.py on a throwaway Postgres."""
import inspect
import itertools
from datetime import datetime, timedelta, timezone

import harness
from harness import check

harness.reset_database()

from db_utils import connection as _conn  # noqa: E402

_c, _ = _conn.get_ready_conn()
_c.close()
import app as backend_app  # noqa: E402
from admin_utils import audit_log  # noqa: E402
from admin_utils import permissions as admin_permissions  # noqa: E402
from admin_utils import projects as admin_projects  # noqa: E402
from admin_utils import users as admin_users  # noqa: E402
from auth_utils import sessions  # noqa: E402
from db_utils import schema  # noqa: E402

client = backend_app.app.test_client()
ORIGIN = "zhcntotw://app"
_ips = itertools.count(1)


def hdr(token=None):
    # a fresh client IP per request keeps the per-IP rate limit out of the way
    h = {"Origin": ORIGIN, "CF-Connecting-IP": f"198.51.{next(_ips) // 250}.{next(_ips) % 250 + 1}"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def rows():
    conn = harness.raw_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, created_at, actor_email, action, target_id, result FROM admin_audit_log ORDER BY id")
    out = cur.fetchall()
    conn.close()
    return out


def new_rows(before):
    return [r[2:] for r in rows()[before:]]


admin = admin_users.create_user("Boss@Example.com", "Boss", 1, "active", actor_email="seed@test")
plain = admin_users.create_user("plain@example.com", "P", 2, "active", actor_email="seed@test")
tok_admin = sessions.create_session("boss@example.com")
tok_plain = sessions.create_session("plain@example.com")
ADMIN = "boss@example.com"
PLAIN = "plain@example.com"

# ---------------------------------------------------------------- A. table shape
print("== A. table shape")
conn = harness.raw_conn()
cur = conn.cursor()
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'admin_audit_log' ORDER BY ordinal_position")
cols = [r[0] for r in cur.fetchall()]
conn.close()
check("only time / actor / action / target / result columns", cols == ["id", "created_at", "actor_email", "action", "target_id", "result"], cols)
check("seeding through the data layer is recorded too", [r[2:] for r in rows()] == [
    ("seed@test", "user.create", admin["id"], "success"),
    ("seed@test", "user.create", plain["id"], "success"),
], rows())
schema._schema_ready = False
c2, cur2 = _conn.get_ready_conn()
c2.close()
check("ensure_schema runs again on an existing table without error", schema._schema_ready)

# ---------------------------------------------------------------- B. successful admin writes
print("== B. successful admin writes")
n = len(rows())
r = client.post("/admin/users", json={"email": "New@Example.com", "name": "N", "role": 2, "status": "active"}, headers=hdr(tok_admin))
check("create user -> 201", r.status_code == 201, r.status_code)
new_id = r.get_json()["id"]
check("user.create recorded with new id and admin as actor", new_rows(n) == [(ADMIN, "user.create", new_id, "success")], new_rows(n))
check("created_at is a UTC ISO timestamp", rows()[-1][1].endswith("+00:00"), rows()[-1][1])

n = len(rows())
r = client.put(f"/admin/users/{new_id}", json={"email": "new@example.com", "name": "N", "role": 2, "status": "active"}, headers=hdr(tok_admin))
check("no-op save -> 200 changed=false", r.status_code == 200 and r.get_json()["changed"] is False, r.get_json())
check("no-op save writes nothing", new_rows(n) == [], new_rows(n))

n = len(rows())
r = client.put(f"/admin/users/{new_id}", json={"email": "new@example.com", "name": "Renamed", "role": 2, "status": "active"}, headers=hdr(tok_admin))
check("rename only -> user.update", new_rows(n) == [(ADMIN, "user.update", new_id, "success")], new_rows(n))

n = len(rows())
r = client.put(f"/admin/users/{new_id}", json={"email": "new@example.com", "name": "Renamed", "role": 1, "status": "deactive"}, headers=hdr(tok_admin))
check("deactivate + role change in one save -> 200", r.status_code == 200, r.get_json())
check("one row per kind of change", new_rows(n) == [
    (ADMIN, "user.deactivate", new_id, "success"),
    (ADMIN, "user.role_change", new_id, "success"),
], new_rows(n))

n = len(rows())
client.put(f"/admin/users/{new_id}", json={"email": "new@example.com", "name": "Renamed", "role": 2, "status": "active"}, headers=hdr(tok_admin))
check("reactivate + role change", new_rows(n) == [
    (ADMIN, "user.activate", new_id, "success"),
    (ADMIN, "user.role_change", new_id, "success"),
], new_rows(n))

n = len(rows())
r = client.put("/admin/permissions/2", json={"role": "user", "allowed_haiku": 25}, headers=hdr(tok_admin))
check("quota change -> permission.update", r.status_code == 200 and new_rows(n) == [(ADMIN, "permission.update", 2, "success")], new_rows(n))
n = len(rows())
client.put("/admin/permissions/2", json={"role": "user", "allowed_haiku": 25}, headers=hdr(tok_admin))
check("unchanged permission writes nothing", new_rows(n) == [], new_rows(n))

n = len(rows())
r = client.post("/admin/projects", json={"name": "案A", "owner_1": plain["id"], "status": "ongoing"}, headers=hdr(tok_admin))
check("create project -> 201", r.status_code == 201, (r.status_code, r.get_json()))
pid = r.get_json()["id"]
check("project.create recorded with the RETURNING id", new_rows(n) == [(ADMIN, "project.create", pid, "success")], new_rows(n))
check("returned project is the one just created", r.get_json()["name"] == "案A")
n = len(rows())
client.put(f"/admin/projects/{pid}", json={"name": "案A2", "owner_1": plain["id"], "status": "ongoing"}, headers=hdr(tok_admin))
check("project.update recorded", new_rows(n) == [(ADMIN, "project.update", pid, "success")], new_rows(n))

# ---------------------------------------------------------------- C. failed admin writes
print("== C. failed admin writes")
n = len(rows())
r = client.post("/admin/users", json={"email": "x@example.com", "name": "X", "role": 2, "status": "bogus"}, headers=hdr(tok_admin))
check("invalid status -> 400 and failed row (no target)", r.status_code == 400 and new_rows(n) == [(ADMIN, "user.create", None, "failed")], new_rows(n))
n = len(rows())
r = client.post("/admin/users", json={"email": "NEW@example.com", "name": "dup", "role": 2, "status": "active"}, headers=hdr(tok_admin))
check("duplicate email -> 400 and only a failed row", r.status_code == 400 and new_rows(n) == [(ADMIN, "user.create", None, "failed")], new_rows(n))
n = len(rows())
r = client.put("/admin/users/99999", json={"email": "z@example.com", "name": "Z", "role": 2, "status": "active"}, headers=hdr(tok_admin))
check("unknown user -> 404 and failed row with target", r.status_code == 404 and new_rows(n) == [(ADMIN, "user.update", 99999, "failed")], new_rows(n))
n = len(rows())
r = client.put(f"/admin/users/{admin['id']}", json={"email": ADMIN, "name": "Boss", "role": 2, "status": "active"}, headers=hdr(tok_admin))
check("demoting the last admin -> 400 and failed row", r.status_code == 400 and new_rows(n) == [(ADMIN, "user.update", admin["id"], "failed")], new_rows(n))
check("last admin is still admin", admin_users.get_user(admin["id"])["role"] == 1)
n = len(rows())
r = client.put("/admin/permissions/2", json={"role": "user", "allowed_haiku": -1}, headers=hdr(tok_admin))
check("negative quota -> 400 and failed row", r.status_code == 400 and new_rows(n) == [(ADMIN, "permission.update", 2, "failed")], new_rows(n))
n = len(rows())
r = client.put("/admin/projects/99999", json={"name": "Q", "owner_1": plain["id"], "status": "ongoing"}, headers=hdr(tok_admin))
check("unknown project -> 404 and failed row", r.status_code == 404 and new_rows(n) == [(ADMIN, "project.update", 99999, "failed")], new_rows(n))

# ---------------------------------------------------------------- D. denied
print("== D. denied (signed in, not admin)")
n = len(rows())
r = client.put(f"/admin/users/{admin['id']}", json={"email": PLAIN, "name": "P", "role": 1, "status": "active"}, headers=hdr(tok_plain))
check("non-admin promote attempt -> 401", r.status_code == 401, r.status_code)
check("denied row with the attempted action and target", new_rows(n) == [(PLAIN, "user.update", admin["id"], "denied")], new_rows(n))
check("plain user was not promoted", admin_users.get_user(plain["id"])["role"] == 2)
n = len(rows())
client.get("/admin/users", headers=hdr(tok_plain))
check("non-admin read -> admin.view denied", new_rows(n) == [(PLAIN, "admin.view", None, "denied")], new_rows(n))
n = len(rows())
client.put("/admin/permissions/2", json={"role": "user", "allowed_haiku": 999}, headers=hdr(tok_plain))
check("non-admin quota change -> permission.update denied", new_rows(n) == [(PLAIN, "permission.update", 2, "denied")], new_rows(n))
n = len(rows())
for h in (hdr(), hdr("0" * 64), hdr("not-a-token")):
    r = client.put("/admin/users/1", json={}, headers=h)
check("not signed in -> 401 and nothing recorded", r.status_code == 401 and new_rows(n) == [], new_rows(n))

# ---------------------------------------------------------------- E. read API
print("== E. read API")
n = len(rows())
r = client.get("/admin/audit-log", headers=hdr(tok_plain))
check("non-admin cannot read the log (401, no entries)", r.status_code == 401 and "entries" not in (r.get_json() or {}), r.get_json())
check("...and that attempt is itself recorded as denied", new_rows(n) == [(PLAIN, "admin.view", None, "denied")], new_rows(n))
r = client.get("/admin/audit-log", headers=hdr())
check("anonymous cannot read the log", r.status_code == 401)
r = client.get("/admin/audit-log", headers=hdr(tok_admin))
body = r.get_json()
check("admin reads the log", r.status_code == 200 and body["retention_days"] == 180, body and body.get("retention_days"))
ids = [e["id"] for e in body["entries"]]
check("newest first", ids == sorted(ids, reverse=True) and ids[0] == rows()[-1][0], ids[:3])
check("entries carry exactly the recorded fields", set(body["entries"][0]) == {"id", "created_at", "actor_email", "action", "target_id", "result"}, body["entries"][0])
r = client.get("/admin/audit-log?limit=3", headers=hdr(tok_admin))
page1 = r.get_json()["entries"]
r = client.get(f"/admin/audit-log?limit=3&before_id={page1[-1]['id']}", headers=hdr(tok_admin))
page2 = r.get_json()["entries"]
check("limit and before_id paginate without overlap", len(page1) == 3 and len(page2) == 3 and page2[0]["id"] < page1[-1]["id"], (page1, page2))
r = client.get("/admin/audit-log?limit=99999", headers=hdr(tok_admin))
check("limit is capped", len(r.get_json()["entries"]) <= audit_log.MAX_LIST_LIMIT)
r = client.get("/admin/audit-log?limit=abc", headers=hdr(tok_admin))
check("non-numeric limit -> 400", r.status_code == 400)
n = len(rows())
client.get("/admin/audit-log", headers=hdr(tok_admin))
check("reading the log does not add rows", new_rows(n) == [])

# ---------------------------------------------------------------- F. same transaction
print("== F. same transaction as the operation")
real_record = audit_log.record


def failing_record(*a, **k):
    raise RuntimeError("audit insert failed")


audit_log.record = failing_record
before_quota = admin_permissions.get_permission(2)["allowed_haiku"]
n = len(rows())
try:
    admin_permissions.update_permission(2, "user", before_quota + 1, actor_email=ADMIN)
    raised = False
except RuntimeError:
    raised = True
check("audit insert failure surfaces", raised)
check("...and the permission change is rolled back", admin_permissions.get_permission(2)["allowed_haiku"] == before_quota)

try:
    admin_users.create_user("ghost@example.com", "G", 2, "active", actor_email=ADMIN)
except RuntimeError:
    pass
conn = harness.raw_conn()
cur = conn.cursor()
cur.execute("SELECT count(*) FROM users WHERE email = 'ghost@example.com'")
check("...and a create is rolled back too", cur.fetchone()[0] == 0)
conn.close()


def record_then_fail(cur, *a, **k):
    real_record(cur, *a, **k)
    raise RuntimeError("operation failed after the audit row was written")


audit_log.record = record_then_fail
try:
    admin_projects.update_project(pid, "案A3", (plain["id"], None, None), "ongoing", actor_email=ADMIN)
except RuntimeError:
    pass
check("failure after the audit row: project unchanged", admin_projects.get_project(pid)["name"] == "案A2")
check("failure after the audit row: no audit row left behind", new_rows(n) == [], new_rows(n))

audit_log.record = real_record
deact = admin_users.create_user("d@example.com", "D", 2, "active", actor_email=ADMIN)
tok_d = sessions.create_session("d@example.com")
audit_log.record = record_then_fail
try:
    admin_users.update_user(deact["id"], "d@example.com", "D", 2, "deactive", actor_email=ADMIN)
except RuntimeError:
    pass
audit_log.record = real_record
check("rolled-back deactivate leaves the account active", admin_users.get_user(deact["id"])["status"] == "active")
check("...and its session alive", sessions.resolve_session(tok_d) == "d@example.com")

r = client.put("/admin/permissions/2", json={"role": "user", "allowed_haiku": before_quota + 5}, headers=hdr(tok_admin))
check("normal path works again after restoring", r.status_code == 200 and rows()[-1][3:] == ("permission.update", 2, "success"))

# denied/failed rows are best effort: a broken log must not turn 401/400 into 500
real_standalone_get = audit_log.get_ready_conn


def broken_conn():
    raise RuntimeError("db down")


audit_log.get_ready_conn = broken_conn
r = client.put("/admin/permissions/2", json={"role": "user", "allowed_haiku": 1}, headers=hdr(tok_plain))
check("denied path still 401 when the log cannot be written", r.status_code == 401, r.status_code)
r = client.put("/admin/permissions/2", json={"role": "user", "allowed_haiku": -5}, headers=hdr(tok_admin))
check("failed path still 400 when the log cannot be written", r.status_code == 400, r.status_code)
audit_log.get_ready_conn = real_standalone_get

# ---------------------------------------------------------------- G. retention
print("== G. 180-day retention")
now = datetime.now(timezone.utc)
conn = harness.raw_conn()
cur = conn.cursor()
for days, action in ((400, "old.400"), (181, "old.181"), (179, "keep.179"), (1, "keep.1")):
    cur.execute(
        "INSERT INTO admin_audit_log (created_at, actor_email, action, target_id, result) VALUES (%s, %s, %s, NULL, 'success')",
        ((now - timedelta(days=days)).isoformat(), ADMIN, action),
    )
conn.commit()
conn.close()
total_before = len(rows())
removed = audit_log.delete_expired()
actions = [r[3] for r in rows()]
check("delete_expired removes exactly the two rows older than 180 days", removed == 2 and len(rows()) == total_before - 2, (removed, total_before, len(rows())))
check("old rows gone", "old.400" not in actions and "old.181" not in actions)
check("rows within 180 days kept", "keep.179" in actions and "keep.1" in actions)
check("second run removes nothing", audit_log.delete_expired() == 0)
src = inspect.getsource(backend_app._startup_recover_jobs)
check("the existing hourly cleanup loop calls delete_expired", "audit_log.delete_expired()" in src)

# ---------------------------------------------------------------- H. nothing sensitive stored
print("== H. nothing sensitive stored")
dump = " ".join(str(v) for r in rows() for v in r)
check("no session token in the log", tok_admin not in dump and tok_plain not in dump)
check("no client IP in the log", "198.51." not in dump)
check("no names / field values in the log", "Renamed" not in dump and "案A" not in dump)

ok = harness.summary("audit log")
raise SystemExit(0 if ok else 1)
