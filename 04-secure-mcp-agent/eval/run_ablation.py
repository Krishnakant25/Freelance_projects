"""
Ablation control: does containment depend on RECOGNISING attack text?

The central claim of this project is that a prompt injection fails because the
capability to act on it was never granted — not because the attack string was
spotted. That claim is easy to assert and easy to quietly break: one refactor
that leans on the sanitizer, and the project becomes exactly the pattern-matching
defence it was built to avoid, with a green test suite either way.

So this runs the red-team corpus TWICE:

  1. normally
  2. with the instruction matcher disabled entirely (it never matches)

If run 2 still holds every invariant, the capability model is genuinely carrying
the security. If it does not, this suite fails and names what regressed.

Secret redaction (`redact_secrets`) stays active in both runs. It is a separate,
unconditional control: a credential sitting in a file the reader is legitimately
allowed to read is a data-hygiene problem, not an injection, and it should not
share a mechanism with instruction detection.

Run:  python eval/run_ablation.py
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_PY = ROOT / "app" / "agent.py"
REDTEAM = ROOT / "eval" / "run_redteam.py"

ABLATION_TARGET = '_INSTRUCTION_RE = re.compile("|".join(_INSTRUCTION_PATTERNS), re.IGNORECASE)'
ABLATION_PATCH = '_INSTRUCTION_RE = re.compile(r"(?!x)x")  # ABLATED BY eval/run_ablation.py'


def run_corpus(label: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(REDTEAM)], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    summary = ""
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("TOTAL:"):
            summary = line.strip()
            break
    violations = [ln.strip() for ln in (proc.stdout or "").splitlines()
                  if ln.strip().startswith("->")]
    print(f"  {label:34s} {'PASS' if proc.returncode == 0 else 'FAIL'}   {summary}")
    for v in violations:
        print(f"      {v}")
    return proc.returncode, summary


def main() -> int:
    print("=" * 78)
    print("ABLATION CONTROL - is the capability model doing the work?")
    print("=" * 78)

    original = AGENT_PY.read_text(encoding="utf-8")
    if ABLATION_TARGET not in original:
        print("\nFAIL: could not find the instruction matcher to ablate.")
        print(f"      expected in {AGENT_PY}:\n      {ABLATION_TARGET}")
        print("      If the sanitizer was refactored, update this control -")
        print("      do NOT delete it. An untested claim is the thing it guards.")
        return 1

    print("\nBASELINE - everything enabled")
    baseline_code, _ = run_corpus("full defence")

    print("\nABLATED - instruction matcher never matches")
    try:
        AGENT_PY.write_text(original.replace(ABLATION_TARGET, ABLATION_PATCH, 1), encoding="utf-8")
        ablated_code, ablated_summary = run_corpus("capability model alone")
    finally:
        AGENT_PY.write_text(original, encoding="utf-8")
        # Restoring matters more than the result: leaving the repo ablated
        # would disable a real defence for every later run.
        assert AGENT_PY.read_text(encoding="utf-8") == original, "FAILED TO RESTORE agent.py"

    print("\n" + "=" * 78)
    if baseline_code != 0:
        print("FAIL: the baseline run did not pass. Fix that before reading the ablation.")
        return 1
    if ablated_code != 0:
        print("FAIL: with the instruction matcher disabled, invariants broke.")
        print("      Security has shifted onto pattern-matching attack text,")
        print("      which is a losing game - the attacker writes the string.")
        print(f"      {ablated_summary}")
        return 1

    print("PASS: every invariant held with the instruction matcher disabled.")
    print("      Injections fail because the capability was never granted,")
    print("      not because the attack text was recognised. The sanitizer is")
    print("      defence in depth, and is not load-bearing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
