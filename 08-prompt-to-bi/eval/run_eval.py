"""
Golden query evaluation — architecture doc §6.8.

Two kinds of check:

  STRUCTURE: did the selector pick the right metric, grouping, date range,
  filters and joins? This is what interpretation can get wrong.

  CONSISTENCY: does the same number reached two different ways agree? A channel
  breakdown must sum to the period total. This is the strongest correctness
  signal available without re-implementing every metric — and it catches the
  classic silent BI error where a join fans out and double-counts.

Run:  python eval/run_eval.py
      python eval/run_eval.py --verbose
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

_harness.quiet_logs()
_harness.ensure_warehouse()

from app import answer as answer_mod  # noqa: E402
from app import selector  # noqa: E402

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_queries.json"
MODEL = answer_mod.get_model()


def run_case(case: dict, verbose: bool = False) -> dict:
    question = case["question"]
    result = answer_mod.ask(question)
    failures = []

    if case.get("expect_refusal"):
        if not result.refused:
            failures.append("expected a refusal, but the question was answered")
        elif case.get("expect_refusal_lists_metrics") and not result.available_metrics:
            failures.append("refusal did not tell the user what metrics ARE available")
        return {
            "id": case["id"], "passed": not failures, "failures": failures,
            "outcome": "refused" if result.refused else "answered",
            "detail": result.refusal_reason if result.refused else "",
        }

    if result.refused:
        failures.append(f"unexpected refusal: {result.message}")
        return {"id": case["id"], "passed": False, "failures": failures, "outcome": "refused", "detail": ""}

    # Re-derive the selection so structure can be inspected directly.
    selection = selector.select(question, MODEL)

    if "expect_metrics" in case:
        if sorted(selection.metrics) != sorted(case["expect_metrics"]):
            failures.append(f"metrics={selection.metrics}, expected {case['expect_metrics']}")
    if "expect_dimensions" in case:
        if sorted(selection.dimensions) != sorted(case["expect_dimensions"]):
            failures.append(f"dimensions={selection.dimensions}, expected {case['expect_dimensions']}")
    if "expect_date_range" in case and result.date_range != case["expect_date_range"]:
        failures.append(f"date_range={result.date_range!r}, expected {case['expect_date_range']!r}")
    if "expect_time_grain" in case and selection.time_grain != case["expect_time_grain"]:
        failures.append(f"time_grain={selection.time_grain!r}, expected {case['expect_time_grain']!r}")
    if "expect_chart" in case and result.chart.get("type") != case["expect_chart"]:
        failures.append(f"chart={result.chart.get('type')!r}, expected {case['expect_chart']!r}")
    if "expect_row_count" in case and result.row_count != case["expect_row_count"]:
        failures.append(f"row_count={result.row_count}, expected {case['expect_row_count']}")
    if "expect_max_rows" in case and result.row_count > case["expect_max_rows"]:
        failures.append(f"row_count={result.row_count} exceeds max {case['expect_max_rows']}")

    for dim in case.get("expect_filters", []):
        if not any(f.dimension == dim for f in selection.filters):
            failures.append(f"expected a filter on {dim!r}, filters={[f.dimension for f in selection.filters]}")

    for join in case.get("expect_joins", []):
        table = {"order_items": "order_items oi", "products": "products p", "customers": "customers c"}[join]
        if table not in result.sql:
            failures.append(f"expected join {join!r} in SQL")

    if "expect_assumption_contains" in case:
        needle = case["expect_assumption_contains"].lower()
        if not any(needle in a.lower() for a in result.assumptions):
            failures.append(f"no assumption containing {needle!r}; got {result.assumptions}")

    if verbose:
        print(f"\n--- {case['id']} ---")
        print(f"  Q: {question}")
        print(f"  metrics={selection.metrics} dims={selection.dimensions} "
              f"range={result.date_range!r} grain={selection.time_grain!r}")
        print(f"  rows={result.row_count} chart={result.chart.get('type')}")

    return {
        "id": case["id"], "passed": not failures, "failures": failures,
        "outcome": "answered", "detail": f"{result.row_count} rows",
    }


def run_consistency(check: dict, verbose: bool = False) -> dict:
    """A breakdown must sum to its total."""
    metric = check["metric"]
    total_a = answer_mod.ask(check["total_question"])
    breakdown = answer_mod.ask(check["breakdown_question"])
    failures = []

    if total_a.refused or breakdown.refused:
        return {
            "id": check["id"], "passed": False,
            "failures": ["one of the two questions was refused"], "detail": "",
        }

    if total_a.row_count != 1:
        failures.append(f"total question returned {total_a.row_count} rows, expected 1")
        return {"id": check["id"], "passed": False, "failures": failures, "detail": ""}

    total = total_a.rows[0].get(metric)
    parts = sum((r.get(metric) or 0) for r in breakdown.rows)

    # Tolerance covers per-group ROUND() in the metric definitions.
    tolerance = max(0.05 * len(breakdown.rows), 0.05)
    diff = abs((total or 0) - parts)
    if diff > tolerance:
        failures.append(
            f"total={total:,.2f} but breakdown sums to {parts:,.2f} (diff {diff:,.2f}) "
            f"— a join may be fanning out and double-counting"
        )

    if verbose:
        print(f"\n--- {check['id']} ---")
        print(f"  total={total:,.2f}  sum(parts)={parts:,.2f}  groups={len(breakdown.rows)}")

    return {
        "id": check["id"], "passed": not failures, "failures": failures,
        "detail": f"total={total:,.2f} parts={parts:,.2f} groups={len(breakdown.rows)}",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    checks = data["consistency_checks"]

    print("=" * 82)
    print("Golden query set — structure")
    print("=" * 82)
    results = [run_case(c, args.verbose) for c in cases]

    print(f"\n{'CASE':44s} {'PASS':6s} {'OUTCOME':10s} DETAIL")
    print("-" * 82)
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{r['id']:44s} {status:6s} {r['outcome']:10s} {r['detail']}")
        for f in r["failures"]:
            print(f"    -> {f}")

    print("\n" + "=" * 82)
    print("Consistency — a breakdown must sum to its total")
    print("=" * 82)
    consistency = [run_consistency(c, args.verbose) for c in checks]
    print(f"\n{'CHECK':44s} {'PASS':6s} DETAIL")
    print("-" * 82)
    for r in consistency:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{r['id']:44s} {status:6s} {r['detail']}")
        for f in r["failures"]:
            print(f"    -> {f}")

    struct_pass = sum(1 for r in results if r["passed"])
    cons_pass = sum(1 for r in consistency if r["passed"])
    refusals = sum(1 for c in cases if c.get("expect_refusal"))

    print("\n" + "=" * 82)
    print(f"Structure:    {struct_pass}/{len(results)}")
    print(f"Consistency:  {cons_pass}/{len(consistency)}")
    print(f"(of which {refusals} cases assert a clean REFUSAL rather than an answer)")

    if struct_pass < len(results) or cons_pass < len(consistency):
        sys.exit(1)
    print("\nEvery question mapped to the intended metric, and every breakdown reconciles.")


if __name__ == "__main__":
    main()
