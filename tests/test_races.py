"""Two races around schema setup and the authorization cache, reproduced with real
threads against Postgres: each check fails if the guarding logic is removed."""
import os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
from harness import check

print("backend under test:", harness.BACKEND)
harness.reset_database()

from db_utils import connection as db_connection, schema  # noqa: E402
from auth_utils import whitelist  # noqa: E402
from admin_utils import users as admin_users  # noqa: E402

Conn = db_connection._SlotReleasingConnection
real_commit = Conn.commit


def free_slots():
    return db_connection._db_slots._value


def query_users():
    conn, cur = db_connection.get_ready_conn()
    try:
        cur.execute("SELECT COUNT(*) FROM users")
        return cur.fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------- A
print("== A. ensure_schema: nobody uses the tables before they are committed")
schema._schema_ready = False
state = {"a": None, "a_blocked": False}
a_in_commit, release_a = threading.Event(), threading.Event()


def blocking_commit(self):
    if threading.current_thread() is state["a"] and not state["a_blocked"]:
        state["a_blocked"] = True
        a_in_commit.set()
        release_a.wait(10)
    return real_commit(self)


Conn.commit = blocking_commit
b_errors = []


def first_request():
    conn, _ = db_connection.get_ready_conn()
    conn.close()


def second_request():
    try:
        query_users()
    except Exception as exc:  # noqa: BLE001
        b_errors.append(type(exc).__name__ + ": " + str(exc).splitlines()[0])


ta = threading.Thread(target=first_request)
state["a"] = ta
ta.start()
a_in_commit.wait(30)
tb = threading.Thread(target=second_request)
tb.start()
tb.join(0.7)
release_a.set()
ta.join(30)
tb.join(30)
Conn.commit = real_commit
check("a request arriving while the first one is still committing the schema works", not b_errors, b_errors)

# ---------------------------------------------------------------- B
print("== B. ensure_schema: a failed commit is retried, and its connection slot comes back")
harness.reset_database()
schema._schema_ready = False
slots_before = free_slots()
left = {"n": 1}


def failing_commit(self):
    if left["n"]:
        left["n"] -= 1
        raise db_connection.psycopg2.OperationalError("connection lost during commit")
    return real_commit(self)


Conn.commit = failing_commit
try:
    db_connection.get_ready_conn()
    raised = False
except Exception:  # noqa: BLE001
    raised = True
Conn.commit = real_commit
check("the failed commit is reported to the caller", raised)
check("the failed attempt gives its connection slot back", free_slots() == slots_before, (free_slots(), slots_before))
try:
    query_users()
    err = None
except Exception as exc:  # noqa: BLE001
    err = type(exc).__name__ + ": " + str(exc).splitlines()[0]
check("the next request creates the schema and works", err is None, err)

# ---------------------------------------------------------------- shared users for C and D
# make sure the tables exist even when B left the old code's flag stuck, so C and D still run
schema._schema_ready = False
_c, _ = db_connection.get_ready_conn()
_c.close()
conn = harness.raw_conn()
cur = conn.cursor()
cur.execute("INSERT INTO users (email, name, role, status) VALUES ('admin@example.com', 'A', 1, 'active')")
cur.execute("INSERT INTO users (email, name, role, status) VALUES ('x@example.com', 'X', 2, 'active') RETURNING id")
uid = cur.fetchone()[0]
conn.commit()
conn.close()

# ---------------------------------------------------------------- C
print("== C. whitelist: a lookup that read the row before a deactivation does not re-cache it")
whitelist._cache.clear()
real_query = whitelist._query_user_auth_row
queried, may_finish = threading.Event(), threading.Event()


def slow_query(email):
    row = real_query(email)
    queried.set()
    may_finish.wait(10)
    return row


whitelist._query_user_auth_row = slow_query
seen = []
t = threading.Thread(target=lambda: seen.append(whitelist.get_user_auth_info("x@example.com")))
t.start()
queried.wait(10)
whitelist._query_user_auth_row = real_query
admin_users.update_user(uid, "x@example.com", "X", 2, "deactive", actor_email="seed@test")
may_finish.set()
t.join(10)
check("the in-flight lookup still answers its own request with what it read", seen and seen[0]["active"] is True, seen)
check("right after the deactivation the user is no longer permitted", whitelist.is_permitted_user("x@example.com") is False,
      whitelist._cache.get("x@example.com"))

# ---------------------------------------------------------------- D
print("== D. whitelist: the deactivation clears the cache only after it is committed")
admin_users.update_user(uid, "x@example.com", "X", 2, "active", actor_email="seed@test")
whitelist._cache.clear()
main = threading.current_thread()
counter = {"n": 0}
reader = []


def commit_with_reader(self):
    if threading.current_thread() is main:
        counter["n"] += 1
        if counter["n"] == 2:  # 1 = get_ready_conn's commit, 2 = the deactivation itself
            r = threading.Thread(target=lambda: reader.append(whitelist.get_user_auth_info("x@example.com")))
            r.start()
            r.join(10)
    return real_commit(self)


Conn.commit = commit_with_reader
admin_users.update_user(uid, "x@example.com", "X", 2, "deactive", actor_email="seed@test")
Conn.commit = real_commit
check("a lookup just before the commit really saw the old row (the interleaving happened)",
      reader and reader[0]["active"] is True, reader)
check("after the deactivation commits the user is no longer permitted", whitelist.is_permitted_user("x@example.com") is False,
      whitelist._cache.get("x@example.com"))

ok = harness.summary("races")
raise SystemExit(0 if ok else 1)
