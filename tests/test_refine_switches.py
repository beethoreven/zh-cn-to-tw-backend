"""Stage 1 switches after the OCR-typo "LLM polish" was removed: which requests
still call the LLM (old clients included), through the real app.py."""
import harness
from harness import check

harness.reset_database()

from db_utils import connection as _conn  # noqa: E402

_c, _ = _conn.get_ready_conn()
_c.close()
import app as backend_app  # noqa: E402

parse = backend_app._parse_refine_switches
cases = [
    # (what the client sends, preprocess expected)
    ({}, True),  # nothing sent: default on
    ({"enable_preprocess": True, "enable_llm_refine": True}, True),
    ({"enable_preprocess": True, "enable_llm_refine": False}, True),
    ({"enable_preprocess": False, "enable_llm_refine": True}, False),  # polish alone no longer calls the LLM
    ({"enable_preprocess": False, "enable_llm_refine": False}, False),
    ({"enable_llm_refine": True}, True),  # oldest clients: one switch, on
    ({"enable_llm_refine": False}, False),  # oldest clients: switch off = no LLM at all
    ({"enable_preprocess": "false", "enable_llm_refine": "true"}, False),  # form encoding
]
for sent, expected in cases:
    got = parse(sent)
    check(f"{sent} -> preprocess={expected}", got == {"enable_preprocess": expected}, got)

# ---------------------------------------------------------------- end to end, released 2.2 request
# The released 2.2 app sends both switches as JSON booleans to /api/jobs/from-ocr-text
# (its polish switch is off by default). Run that exact request through the real route
# and pipeline with the LLM stubbed, and check the job still finishes.
import itertools  # noqa: E402
import time  # noqa: E402

from admin_utils import projects as admin_projects  # noqa: E402
from admin_utils import users as admin_users  # noqa: E402
from auth_utils import sessions  # noqa: E402
from configs import config  # noqa: E402
from jobs import job_manager  # noqa: E402
from llm_utils import gemini_client  # noqa: E402
from llm_utils.prompts import ALL_REFINE_ITEMS  # noqa: E402

config.BATCH_DELAY_SECONDS = 0
user = admin_users.create_user("u@example.com", "U", 2, "active", actor_email="seed@test")
project = admin_projects.create_project("案子", (user["id"], None, None), "ongoing", actor_email="seed@test")
token = sessions.create_session("u@example.com")
client = backend_app.app.test_client()
_ips = itertools.count(1)
llm_calls = []


def fake_refine(text, model=None, user_email=None, file_name=None, project=None, items=None):
    llm_calls.append(items)
    return text


gemini_client.refine_text = fake_refine


def run_job(switches):
    llm_calls.clear()
    n = next(_ips)
    r = client.post(
        "/api/jobs/from-ocr-text",
        json={"pages": ["第一页内容", "第二页内容"], "model": config.GEMINI_MODEL, "batch_pages": "5",
              "max_retry": "1", "file_name": "t.pdf", "project": project["id"], **switches},
        headers={"Origin": "zhcntotw://app", "Authorization": f"Bearer {token}", "CF-Connecting-IP": f"203.0.113.{n}"},
    )
    if r.status_code != 202:
        return r.status_code, None
    job_id = r.get_json()["job_id"]
    for _ in range(1200):  # up to 60 s: the first job also loads OpenCC
        job = job_manager.get_job(job_id)
        if job["status"] in ("done", "failed", "error"):
            break
        time.sleep(0.05)
    return r.status_code, job


for label, switches, expect_llm in [
    ("2.2 default (preprocess on, polish off)", {"enable_preprocess": True, "enable_llm_refine": False}, True),
    ("2.2 both on", {"enable_preprocess": True, "enable_llm_refine": True}, True),
    ("2.2 polish only", {"enable_preprocess": False, "enable_llm_refine": True}, False),
    ("2.2 both off", {"enable_preprocess": False, "enable_llm_refine": False}, False),
]:
    status, job = run_job(switches)
    check(f"{label}: accepted (202)", status == 202, status)
    check(f"{label}: job finishes", job is not None and job["status"] == "done", job and job["status"])
    check(f"{label}: result is traditional text", job is not None and "第一頁內容" in (job.get("result_text") or ""), job and job.get("result_text"))
    if expect_llm:
        check(f"{label}: LLM gets the preprocessing rules only", llm_calls and all(i == ALL_REFINE_ITEMS for i in llm_calls), llm_calls)
    else:
        check(f"{label}: LLM not called", llm_calls == [], llm_calls)

raise SystemExit(0 if harness.summary("refine switches") else 1)
