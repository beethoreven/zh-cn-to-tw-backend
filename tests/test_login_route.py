"""
Runs the real /auth/login view function from app.py, without importing app.py
(importing it connects to the database and starts background threads).

The function's source is lifted out of app.py with ast and registered on a
bare Flask app. auth is the real module (with the same fake Google cert
fetcher as test_nonce.py); whitelist and sessions are stand-ins.
"""
import ast
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_nonce as tn  # noqa: E402  (sets up the fake signer / cert fetcher; prints its own results)

from flask import Flask, jsonify, request  # noqa: E402

source = open(os.path.join(tn.BACKEND, "app.py"), encoding="utf-8").read()
tree = ast.parse(source)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "auth_login")
fn.decorator_list = []
module = ast.Module(body=[fn], type_ignores=[])

created = []
fake_whitelist = types.SimpleNamespace(normalize_email=lambda e: e.strip().lower(), is_permitted_user=lambda e: True, is_admin_user=lambda e: False)
fake_sessions = types.SimpleNamespace(create_session=lambda e: created.append(e) or "session-token-for-test")
ns = {"request": request, "jsonify": jsonify, "auth": tn.auth, "whitelist": fake_whitelist, "sessions": fake_sessions}
exec(compile(module, "app.py:auth_login", "exec"), ns)

app = Flask(__name__)
app.add_url_rule("/auth/login", "auth_login", ns["auth_login"], methods=["POST"])
client = app.test_client()

results = []


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(("PASS" if ok else "FAIL"), label, "" if ok else f"-> {got!r}")


new_token = tn.make_token(tn.NEW)
legacy_token = tn.make_token(tn.LEGACY)

r = client.post("/auth/login", json={"id_token": new_token, "nonce": tn.RAW})
check("route: new client with nonce -> 200 + session", (r.status_code, r.get_json().get("session_token")), (200, "session-token-for-test"))
r = client.post("/auth/login", json={"id_token": new_token})
check("route: new client token without nonce -> 401", r.status_code, 401)
r = client.post("/auth/login", json={"id_token": new_token, "nonce": "wrong"})
check("route: wrong nonce -> 401", r.status_code, 401)
r = client.post("/auth/login", json={"id_token": legacy_token})
check("route: old client (no nonce field) -> 426", r.status_code, 426)
check("route: old client is told to update, with the download page", "beethoreven.github.io/zh-cn-to-tw-web" in r.get_json().get("error", ""), True)
r = client.post("/auth/login", json={"id_token": tn.make_token()})
check("route: token with no nonce claim (old browser version) -> 401", r.status_code, 401)
r = client.post("/auth/login", json={"id_token": tn.make_token(), "nonce": tn.RAW})
check("route: no nonce claim even with a raw nonce -> 401", r.status_code, 401)
r = client.post("/auth/login", json=[new_token, tn.RAW])
check("route: JSON array body -> 401, not a 500", r.status_code, 401)
r = client.post("/auth/login", data="not json", content_type="text/plain")
check("route: non-JSON body -> 401", r.status_code, 401)
check("route: a session was created only for the one accepted login", len(created), 1)

print(f"\nroute: {sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
