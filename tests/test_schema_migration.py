"""users.email lowercase migration on an existing (old-schema) database."""
import contextlib
import io

import harness
from harness import check

import psycopg2  # noqa: E402

from db_utils import connection, schema  # noqa: E402

OLD_SCHEMA = """
CREATE TABLE permissions (id SERIAL PRIMARY KEY, role TEXT NOT NULL, allowed_haiku INTEGER NOT NULL DEFAULT 0);
INSERT INTO permissions (id, role, allowed_haiku) VALUES (1, 'admin', 999999), (2, 'user', 10);
SELECT setval('permissions_id_seq', 2);
CREATE TABLE users (id SERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT,
                    role INTEGER NOT NULL REFERENCES permissions(id), status TEXT NOT NULL DEFAULT 'active');
"""


def fresh_old_db(users):
    harness.reset_database()
    conn = harness.raw_conn()
    cur = conn.cursor()
    cur.execute(OLD_SCHEMA)
    for email, role in users:
        cur.execute("INSERT INTO users (email, name, role) VALUES (%s, 'n', %s)", (email, role))
    conn.commit()
    conn.close()
    schema._schema_ready = False


def run_ensure_schema():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        conn, cur = connection.get_ready_conn()
        conn.close()
    return out.getvalue()


def emails():
    conn = harness.raw_conn()
    cur = conn.cursor()
    cur.execute("SELECT email FROM users ORDER BY id")
    rows = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'users' AND indexname = 'users_email_lower_key'")
    has_index = cur.fetchone() is not None
    cur.execute("SELECT to_regclass('jobs') IS NOT NULL, to_regclass('app_versions') IS NOT NULL")
    later_tables = all(cur.fetchone())
    conn.close()
    return rows, has_index, later_tables


print("== old database with mixed-case emails, no duplicates")
fresh_old_db([("Alice@Example.com", 1), ("bob@example.com", 2), ("CAROL@EXAMPLE.COM", 2)])
log = run_ensure_schema()
rows, has_index, later = emails()
check("emails are lowercased", rows == ["alice@example.com", "bob@example.com", "carol@example.com"], rows)
check("case-insensitive unique index created", has_index)
check("later schema steps still ran", later)
check("no warning printed", "警告" not in log, log)
conn = harness.raw_conn()
cur = conn.cursor()
try:
    cur.execute("INSERT INTO users (email, name, role) VALUES ('ALICE@example.com', 'x', 2)")
    conn.commit()
    check("a case variant can no longer be inserted", False)
except psycopg2.IntegrityError:
    conn.rollback()
    check("a case variant can no longer be inserted", True)
conn.close()
schema._schema_ready = False
log = run_ensure_schema()
check("running again is a no-op", emails()[0] == rows and "警告" not in log, log)

print("== old database with a case-only duplicate")
fresh_old_db([("dup@example.com", 1), ("Dup@Example.com", 2), ("Other@Example.com", 2)])
log = run_ensure_schema()
rows, has_index, later = emails()
check("duplicates: nothing is renamed", rows == ["dup@example.com", "Dup@Example.com", "Other@Example.com"], rows)
check("duplicates: no index", not has_index)
check("duplicates: a warning with the count is logged", "1 組" in log and "警告" in log, log)
check("duplicates: the rest of the schema is still created (service keeps working)", later)
check("duplicates: the warning does not include the emails", "dup@example.com" not in log.lower(), log)

conn = harness.raw_conn()
cur = conn.cursor()
cur.execute("UPDATE users SET email = 'dup-old@example.com' WHERE email = 'Dup@Example.com'")
conn.commit()
conn.close()
schema._schema_ready = False
log = run_ensure_schema()
rows, has_index, later = emails()
check("after the admin fixes the duplicate, the next start completes the migration",
      has_index and rows == ["dup@example.com", "dup-old@example.com", "other@example.com"], (rows, log))

print("== admin role named 'admin' under another id")
fresh_old_db([("a@example.com", 1)])
conn = harness.raw_conn()
cur = conn.cursor()
cur.execute("UPDATE permissions SET role = '管理員' WHERE id = 1")
cur.execute("INSERT INTO permissions (role, allowed_haiku) VALUES ('Admin', 5)")
conn.commit()
conn.close()
log = run_ensure_schema()
check("a warning is logged when 'admin' is not id 1", "警告" in log and "id=1" in log, log)

print("== failure inside the migration is contained")
fresh_old_db([("Zed@Example.com", 1)])
real_execute_sql = "CREATE UNIQUE INDEX"
orig = schema._normalize_user_emails


def failing_normalize(cur):
    class Wrapper:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, q, *a):
            if q.startswith(real_execute_sql):
                raise psycopg2.errors.UniqueViolation("simulated race")
            return self.inner.execute(q, *a)

        def fetchone(self):
            return self.inner.fetchone()

        def fetchall(self):
            return self.inner.fetchall()

    return orig(Wrapper(cur))


schema._normalize_user_emails = failing_normalize
log = run_ensure_schema()
schema._normalize_user_emails = orig
rows, has_index, later = emails()
check("a failing index creation is rolled back, not raised", "略過" in log and not has_index, log)
check("...the lowercase UPDATE is rolled back with it", rows == ["Zed@Example.com"], rows)
check("...and the remaining schema steps still ran", later)

ok = harness.summary("schema migration")
raise SystemExit(0 if ok else 1)
