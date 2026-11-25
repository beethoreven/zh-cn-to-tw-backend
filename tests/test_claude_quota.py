"""Per-person monthly Claude Haiku limit (permissions.allowed_haiku) — through the real
app.py with the LLM clients stubbed."""
import itertools
import threading
import time

import harness
from harness import check

harness.reset_database()

from db_utils import connection as _conn  # noqa: E402

_c, _ = _conn.get_ready_conn()
_c.close()
import app as backend_app  # noqa: E402
from admin_utils import permissions as admin_permissions  # noqa: E402
from admin_utils import projects as admin_projects  # noqa: E402
from admin_utils import users as admin_users  # noqa: E402
from auth_utils import sessions  # noqa: E402
from configs import config  # noqa: E402
from jobs import job_manager  # noqa: E402
from llm_utils import claude_client, gemini_client  # noqa: E402
from usage import claude_quota  # noqa: E402

CLAUDE = "claude-haiku-4-5-20251001"
GEMINI = config.GEMINI_MODEL
config.BATCH_DELAY_SECONDS = 0

llm_calls = []
claude_client.refine_text = lambda text, **k: (llm_calls.append("claude.refine"), text)[1]
claude_client.review_text = lambda text, **k: (llm_calls.append("claude.review"), "[]")[1]
claude_client.analyze_pattern = lambda text, **k: (llm_calls.append("claude.analyze"), "[]")[1]
gemini_client.refine_text = lambda text, **k: (llm_calls.append("gemini.refine"), text)[1]
gemini_client.review_text = lambda text, **k: (llm_calls.append("gemini.review"), "[]")[1]

admin_users.create_user("boss@example.com", "Boss", 1, "active", actor_email="seed@test")
user = admin_users.create_user("U@Example.com", "U", 2, "active", actor_email="seed@test")
other = admin_users.create_user("other@example.com", "O", 2, "active", actor_email="seed@test")
admin_permissions.update_permission(2, "user", 3, actor_email="seed@test")
personal = admin_projects.create_project("個人專案", (user["id"], None, None), "ongoing", actor_email="seed@test")
assert personal["id"] == config.PERSONAL_PROJECT_ID
project = admin_projects.create_project("案子", (user["id"], other["id"], None), "ongoing", actor_email="seed@test")

client = backend_app.app.test_client()
_ips = itertools.count(1)
tokens = {e: sessions.create_session(e) for e in ("u@example.com", "other@example.com", "boss@example.com")}


def hdr(email):
    n = next(_ips)
    return {"Origin": "zhcntotw://app", "Authorization": f"Bearer {tokens[email]}",
            "CF-Connecting-IP": f"198.18.{n // 250}.{n % 250 + 1}"}


def stage1(email, model, preprocess=True, project_id=None):
    return client.post("/api/jobs/from-ocr-text", headers=hdr(email), json={
        "pages": ["第一页"], "model": model, "batch_pages": "5", "max_retry": "1", "file_name": "t.pdf",
        "project": project_id or project["id"], "enable_preprocess": preprocess})


def wait_done(job_id):
    for _ in range(1200):
        job = job_manager.get_job(job_id)
        if job["status"] in ("done", "failed", "error"):
            return job
        time.sleep(0.05)
    return job_manager.get_job(job_id)


def used(email):
    return claude_quota.used_this_month(email)


# ---------------------------------------------------------------- A. counting and the limit
print("== A. limit of 3 for the user role")
llm_calls.clear()
r = stage1("u@example.com", CLAUDE)
check("1st Claude Stage 1: accepted", r.status_code == 202, r.get_json())
first_job = wait_done(r.get_json()["job_id"])
check("...and the job runs on Claude", first_job["status"] == "done" and "claude.refine" in llm_calls, (first_job["status"], llm_calls))
check("counted once", used("u@example.com") == 1, used("u@example.com"))

r = stage1("u@example.com", GEMINI)
check("Gemini: accepted and not counted", r.status_code == 202 and used("u@example.com") == 1, (r.status_code, used("u@example.com")))
r = stage1("u@example.com", CLAUDE, preprocess=False)
check("Claude with preprocessing off (no LLM call): accepted and not counted",
      r.status_code == 202 and used("u@example.com") == 1, (r.status_code, used("u@example.com")))
r = stage1("u@example.com", CLAUDE, project_id=config.PERSONAL_PROJECT_ID)
check("Claude on the personal project: still rejected (400) and not counted",
      r.status_code == 400 and used("u@example.com") == 1, (r.status_code, used("u@example.com")))

r = client.post(f"/api/jobs/{first_job['id']}/review", headers=hdr("u@example.com"),
                data={"model": CLAUDE, "project": str(project["id"])})
check("Claude Stage 3 review: accepted and counted", r.status_code == 202 and used("u@example.com") == 2, (r.status_code, used("u@example.com")))
review_id = r.get_json()["review_id"]
for _ in range(200):
    if backend_app.review_manager.get_review(review_id)["status"] in ("done", "failed", "error"):
        break
    time.sleep(0.05)
r = client.post(f"/api/reviews/{review_id}/rerun", headers=hdr("u@example.com"),
                data={"model": CLAUDE, "project": str(project["id"])})
check("Claude rerun: accepted and counted (3 of 3)", r.status_code == 202 and used("u@example.com") == 3, (r.status_code, used("u@example.com")))

llm_calls.clear()
r = stage1("u@example.com", CLAUDE)
data = r.get_json() or {}
check("4th Claude job: 429 with a clear message", r.status_code == 429 and "3 次" in data.get("error", "") and "Gemini" in data.get("error", ""), data)
check("...the count stays at the limit", used("u@example.com") == 3, used("u@example.com"))
time.sleep(0.3)
check("...and nothing was sent to Claude", "claude.refine" not in llm_calls, llm_calls)
r = client.post(f"/api/jobs/{first_job['id']}/review", headers=hdr("u@example.com"),
                data={"model": CLAUDE, "project": str(project["id"])})
check("review over the limit: 429", r.status_code == 429, r.status_code)
r = client.post(f"/api/jobs/{first_job['id']}/review", headers=hdr("u@example.com"),
                data={"model": GEMINI, "project": str(project["id"])})
check("Gemini review over the Claude limit: still allowed", r.status_code == 202, r.status_code)

# ---------------------------------------------------------------- B. Stage 2 analysis
print("== B. Stage 2 analysis report")
r = client.post(f"/api/jobs/{first_job['id']}/pattern", headers=hdr("other@example.com"), json={"template": "第一頁 → 第壹頁\n"})
check("someone else's job is not reachable (404)", r.status_code == 404, r.status_code)
r2 = stage1("other@example.com", GEMINI)
other_job = wait_done(r2.get_json()["job_id"])
r = client.post(f"/api/jobs/{other_job['id']}/pattern", headers=hdr("other@example.com"), json={"template": "第一頁 → 第壹頁\n"})
with_hits = r.get_json()["job_id"]
r = client.post(f"/api/jobs/{other_job['id']}/pattern", headers=hdr("other@example.com"), json={"template": "不存在 → 也不存在\n"})
no_hits = r.get_json()["job_id"]
r = client.post(f"/api/jobs/{no_hits}/pattern-analysis", headers=hdr("other@example.com"),
                json={"model": CLAUDE, "project": project["id"]})
check("analysis with no word changes (no LLM call): accepted and not counted",
      r.status_code == 202 and used("other@example.com") == 0, (r.status_code, used("other@example.com")))
r = client.post(f"/api/jobs/{with_hits}/pattern-analysis", headers=hdr("other@example.com"),
                json={"model": CLAUDE, "project": project["id"]})
check("analysis with word changes on Claude: accepted and counted",
      r.status_code == 202 and used("other@example.com") == 1, (r.status_code, used("other@example.com")))
check("each person has their own count", used("u@example.com") == 3, used("u@example.com"))

# ---------------------------------------------------------------- C. simultaneous requests
print("== C. simultaneous requests")
admin_permissions.update_permission(2, "user", 5, actor_email="seed@test")  # other has used 1: 4 left
results = []
barrier = threading.Barrier(12)


def fire():
    barrier.wait()
    try:
        claude_quota.consume("other@example.com", CLAUDE)
        results.append("ok")
    except claude_quota.ClaudeQuotaExceeded:
        results.append("over")


threads = [threading.Thread(target=fire) for _ in range(12)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("12 at once with 4 left: exactly 4 granted", results.count("ok") == 4 and results.count("over") == 8, results)
check("count ends exactly at the limit", used("other@example.com") == 5, used("other@example.com"))

# ---------------------------------------------------------------- D. limits of 0, admins, months
print("== D. zero, admin, new month")
admin_permissions.update_permission(2, "user", 0, actor_email="seed@test")
r = stage1("u@example.com", CLAUDE)
check("limit 0: Claude rejected", r.status_code == 429, r.status_code)
for _ in range(5):
    claude_quota.consume("boss@example.com", CLAUDE)
r = stage1("boss@example.com", CLAUDE)
check("admin role (999999): still allowed", r.status_code == 202, r.status_code)

admin_permissions.update_permission(2, "user", 3, actor_email="seed@test")
real_month = claude_quota.current_month
claude_quota.current_month = lambda: "2099-01"
r = stage1("u@example.com", CLAUDE)
check("next month: the count starts over", r.status_code == 202 and used("u@example.com") == 1, (r.status_code, used("u@example.com")))
claude_quota.current_month = real_month
check("last month's count is kept separately", used("u@example.com") == 3, used("u@example.com"))
check("month is Taiwan time, YYYY-MM", len(real_month()) == 7 and real_month()[4] == "-", real_month())

raise SystemExit(0 if harness.summary("claude quota") else 1)
