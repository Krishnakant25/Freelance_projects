"""
End-to-end intake pipeline eval — runs every case through the real
app.intake.start_intake() entry point (not individual modules in isolation),
using LLM_PROVIDER=none so it's free, deterministic, and needs no API key.

Run:  python eval/run_eval.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import config, db, kb  # noqa: E402

config.LLM_PROVIDER = "none"
config.SLACK_WEBHOOK_URL = ""

from app.intake import start_intake  # noqa: E402

EVAL_SET_PATH = Path(__file__).resolve().parent / "eval_set.json"
KB_DIR = ROOT / "data" / "kb_articles"


def setup():
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db.init_db()
    kb.ingest_kb_directory(KB_DIR)
    kb.invalidate_cache()


def run_case(case: dict) -> dict:
    result = start_intake(case["description"], requester="eval-harness")

    passed = True
    reasons = []

    if result.outcome != case["expect_outcome"]:
        passed = False
        reasons.append(f"expected outcome={case['expect_outcome']!r}, got {result.outcome!r}")

    if case["expect_outcome"] == "deflected":
        expected_title_fragment = case.get("expect_kb_title_contains", "")
        if expected_title_fragment and (
            result.kb_match is None or expected_title_fragment.lower() not in result.kb_match.title.lower()
        ):
            passed = False
            got_title = result.kb_match.title if result.kb_match else None
            reasons.append(f"expected KB title containing {expected_title_fragment!r}, got {got_title!r}")

    if case["expect_outcome"] == "ticket_created":
        expected_priority = case.get("expect_priority")
        if expected_priority and result.priority != expected_priority:
            passed = False
            reasons.append(f"expected priority={expected_priority!r}, got {result.priority!r}")

        expected_red_flag = case.get("expect_red_flag", False)
        if result.red_flag != expected_red_flag:
            passed = False
            reasons.append(f"expected red_flag={expected_red_flag!r}, got {result.red_flag!r}")

    return {
        "id": case["id"],
        "passed": passed,
        "reasons": reasons,
        "outcome": result.outcome,
        "priority": result.priority,
        "red_flag": result.red_flag,
    }


def main():
    print("=" * 78)
    print("End-to-end intake pipeline eval (LLM_PROVIDER=none)")
    print("=" * 78)

    setup()
    eval_set = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
    cases = eval_set["cases"]

    results = [run_case(c) for c in cases]

    print(f"\n{'ID':32s} {'PASS':6s} {'OUTCOME':16s} {'PRIORITY':9s} {'REDFLAG':8s}")
    print("-" * 78)
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{r['id']:32s} {status:6s} {r['outcome']:16s} {str(r['priority']):9s} {str(r['red_flag']):8s}")
        for reason in r["reasons"]:
            print(f"    -> {reason}")

    total_pass = sum(1 for r in results if r["passed"])
    print("\n" + "=" * 78)
    print(f"TOTAL: {total_pass}/{len(results)} cases passed")

    if total_pass < len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
