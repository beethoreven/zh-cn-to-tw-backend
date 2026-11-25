import harness  # noqa: F401  (env + sys.path; no production .env)
from harness import check, summary
import app as backend_app

client = backend_app.app.test_client()
allowed = ["https://zh-cn-to-tw.example", "zhcntotw://app", "null", "http://localhost:5173", "http://127.0.0.1:8123"]
denied = ["https://beethoreven.github.io", "https://evil.example", "http://localhost.evil.com", "https://ZH-CN-TO-TW.example.evil"]
for o in allowed:
    r = client.options("/api/health", headers={"Origin": o, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Authorization, Content-Type"})
    check(f"preflight allowed: {o}", r.headers.get("Access-Control-Allow-Origin") == o, dict(r.headers))
    h = (r.headers.get("Access-Control-Allow-Headers") or "").lower()
    check(f"  allows Authorization: {o}", "authorization" in h, h)
    g = client.get("/api/health", headers={"Origin": o})
    check(f"  simple GET echoes origin + exposes Content-Disposition: {o}", g.headers.get("Access-Control-Allow-Origin") == o and "content-disposition" in (g.headers.get("Access-Control-Expose-Headers") or "").lower(), dict(g.headers))
for o in denied:
    r = client.options("/api/health", headers={"Origin": o, "Access-Control-Request-Method": "POST"})
    g = client.get("/api/health", headers={"Origin": o})
    check(f"denied: {o}", "Access-Control-Allow-Origin" not in r.headers and "Access-Control-Allow-Origin" not in g.headers, dict(g.headers))
raise SystemExit(0 if summary("cors") else 1)
