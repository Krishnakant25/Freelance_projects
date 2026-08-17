"""
Runs the full test suite. Every check here is deterministic, free, and needs
no API key or network.

    python run_all_tests.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

SUITES = [
    ("Priority rules engine (exhaustive)", ["eval/test_rules_engine.py"]),
    ("Red-flag keyword scanner", ["eval/test_redflag.py"]),
    ("Structured extraction", ["eval/test_extraction.py"]),
    ("Alerting + escalation", ["eval/test_alerting.py"]),
    ("Production hardening regressions", ["eval/test_production_hardening.py"]),
    ("API validation / rate limiting / probes", ["eval/test_api.py"]),
    ("End-to-end intake pipeline", ["eval/run_eval.py"]),
]


def main():
    results = []
    for name, args in SUITES:
        print("\n" + "#" * 74)
        print(f"# {name}")
        print("#" * 74)
        proc = subprocess.run([PY, *args], cwd=ROOT, env={"PYTHONIOENCODING": "utf-8", **__import__("os").environ})
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
