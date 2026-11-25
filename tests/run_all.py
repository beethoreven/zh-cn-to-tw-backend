"""Runs every test file in its own process (each one wipes the database first) and
exits non-zero if any of them fails.

    python tests/run_all.py

Needs TEST_DATABASE_URL pointing at a local throwaway Postgres, or the optional
`pgserver` package for an embedded one (see harness.py)."""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

failed = []
for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
    name = os.path.basename(path)
    print(f"\n===== {name}", flush=True)
    result = subprocess.run([sys.executable, path], cwd=HERE)
    if result.returncode != 0:
        failed.append(name)

print("\n===== summary")
print("failed: " + ", ".join(failed) if failed else "all test files passed")
sys.exit(1 if failed else 0)
