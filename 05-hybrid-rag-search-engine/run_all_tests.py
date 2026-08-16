"""
Runs the full test suite. Every check here is deterministic, free, and needs
no API key or network — so there is no excuse for skipping it before a
deploy.

    python run_all_tests.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

SUITES = [
    ("Retrieval + ACL + refusal eval", ["eval/run_eval.py"]),
    ("Generation + citation verification", ["eval/test_generation.py"]),
    ("Query cache ACL isolation", ["eval/test_cache_isolation.py"]),
    ("ACL update on re-ingest", ["eval/test_acl_update.py"]),
]


def main():
    results = []
    for name, args in SUITES:
        print("\n" + "#" * 74)
        print(f"# {name}")
        print("#" * 74)
        proc = subprocess.run([PY, *args], cwd=ROOT)
        results.append((name, proc.returncode == 0))

    print("\n" + "=" * 74)
    print("SUITE SUMMARY")
    print("=" * 74)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"\n{len(failed)} suite(s) failed.")
        sys.exit(1)
    print("\nAll suites passed.")


if __name__ == "__main__":
    main()
