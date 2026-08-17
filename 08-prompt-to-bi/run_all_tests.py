"""
Full test suite. Deterministic, free, no API keys, no network.

    python run_all_tests.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

SUITES = [
    ("SQL safety (read-only, injection, cost ceilings)", ["eval/test_sql_safety.py"]),
    ("Frozen reports (no silent drift)", ["eval/test_reports.py"]),
    ("Golden queries (structure + consistency)", ["eval/run_eval.py"]),
]


def main():
    env = {"PYTHONIOENCODING": "utf-8", **os.environ}
    results = []
    for name, args in SUITES:
        print("\n" + "#" * 78)
        print(f"# {name}")
        print("#" * 78)
        proc = subprocess.run([PY, *args], cwd=ROOT, env=env)
        results.append((name, proc.returncode == 0))

    print("\n" + "=" * 78)
    print("SUITE SUMMARY")
    print("=" * 78)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"\n{len(failed)} suite(s) failed.")
        sys.exit(1)
    print("\nAll suites passed.")


if __name__ == "__main__":
    main()
