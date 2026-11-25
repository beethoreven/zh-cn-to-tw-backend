"""
Shared setup for the backend tests.

- Never touches production: dotenv loading is disabled before configs is imported
  (a developer's .env holds production secrets), the database must be a local
  throwaway Postgres, and the LLM keys are dummies.
- Database: TEST_DATABASE_URL if set (CI uses a Postgres service container); it
  must point at localhost / 127.0.0.1, anything else is refused. Without it, an
  embedded Postgres from the optional `pgserver` package is started under
  tests/.pgdata.
- Tests import the real modules (and the real app.py) from the backend checkout.
  Every test file wipes the database first.
"""
import os
import sys
import threading
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)

import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False  # keep any real .env out of this process


def _database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        host = urlparse(url).hostname
        if host not in ("localhost", "127.0.0.1", "::1"):
            sys.exit(f"TEST_DATABASE_URL must point at a local throwaway database, not {host!r}")
        return url
    try:
        import pgserver
    except ImportError:
        sys.exit("Set TEST_DATABASE_URL to a local Postgres, or `pip install pgserver` for an embedded one.")
    server = pgserver.get_server(os.path.join(HERE, ".pgdata"), cleanup_mode="stop")
    return server.get_uri()


DATABASE_URL = _database_url()

os.environ.update(
    {
        "DATABASE_URL": DATABASE_URL,
        "GEMINI_API_KEY": "dummy-not-a-key",
        "ANTHROPIC_API_KEY": "dummy-not-a-key",
        "GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
        "SESSION_SIGNING_KEY": "test-signing-key-not-a-real-secret-0123456789",
    }
)
sys.path.insert(0, BACKEND)

import psycopg2  # noqa: E402

_results = []
_lock = threading.Lock()


def check(label, cond, detail=""):
    with _lock:
        _results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label + ("" if cond or not detail else f"  -> {detail}"))


def summary(name):
    passed = sum(_results)
    print(f"\n{name}: {passed}/{len(_results)} passed")
    return passed == len(_results)


def reset_database():
    """Drop every table so each test file starts from an empty database."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE")
    cur.execute("CREATE SCHEMA public")
    conn.close()


def raw_conn():
    return psycopg2.connect(DATABASE_URL)
