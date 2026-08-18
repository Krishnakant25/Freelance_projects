"""
Runs every test suite in this project and reports one verdict.

    python run_all_tests.py

Each suite is a standalone script; this runs them as subprocesses so a crash
in one cannot take the others with it, and so the exit code is meaningful in
CI.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SUITES = [
    ("Capability model", "eval/test_capabilities.py",
     "path containment, egress allow-list, SSRF, least privilege"),
    ("Trifecta split", "eval/test_trifecta.py",
     "poisoned file fails at every layer; the unsafe grant is unconstructible"),
    ("Audit + registry", "eval/test_audit_and_registry.py",
     "tamper detection, MCP rug pull, tool-description injection"),
    ("Policy + approvals", "eval/test_policy.py",
     "risk tiers, session grants, undo, fail-closed when unattended"),
    ("API + separation of duties", "eval/test_api_and_auth.py",
     "auth, role boundaries, nobody approves their own action"),
    ("Red-team corpus", "eval/run_redteam.py",
     "20 payloads x 5 runs, invariant-checked"),
    ("Ablation control", "eval/run_ablation.py",
     "invariants must hold with the instruction matcher DISABLED"),
]


def main() -> int:
    print("=" * 78)
    print("SECURE MCP AGENT - FULL TEST SUITE")
    print("=" * 78)

    results = []
    for name, script, blurb in SUITES:
        print(f"\n>>> {name}")
        print(f"    {blurb}")
        started = time.time()
        proc = subprocess.run(
            [sys.executable, script], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        elapsed = time.time() - started
        ok = proc.returncode == 0

        summary = ""
        for line in reversed((proc.stdout or "").splitlines()):
            if "passed," in line or "TOTAL:" in line:
                summary = line.strip()
                break

        print(f"    {'PASS' if ok else 'FAIL'}  ({elapsed:.1f}s)  {summary}")
        if not ok:
            tail = (proc.stdout or "").splitlines()[-25:]
            for line in tail:
                print(f"      | {line}")
            if proc.stderr:
                print(f"      ! {proc.stderr.strip()[:800]}")
        results.append((name, ok, elapsed, summary))

    print("\n" + "=" * 78)
    print(f"{'SUITE':32s} {'RESULT':8s} {'TIME':>7s}  DETAIL")
    print("-" * 78)
    for name, ok, elapsed, summary in results:
        print(f"{name:32s} {'PASS' if ok else 'FAIL':8s} {elapsed:6.1f}s  {summary[:28]}")

    failed = [n for n, ok, _, _ in results if not ok]
    print("=" * 78)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"All {len(results)} suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
