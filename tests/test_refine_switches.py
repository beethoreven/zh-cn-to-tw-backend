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

raise SystemExit(0 if harness.summary("refine switches") else 1)
