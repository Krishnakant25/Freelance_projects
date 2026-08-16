"""
Exhaustive tests for the priority rules engine — every matrix cell, plus the
unknown-value escalation behavior that's the whole safety argument of this
module.

Run:  python eval/test_rules_engine.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.rules_engine import Impact, Priority, Urgency, resolve_priority  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


def test_every_matrix_cell():
    print("\n[matrix] every (impact, urgency) combination produces a known priority")
    expected = {
        (Impact.SINGLE_USER, Urgency.LOW): Priority.P4,
        (Impact.SINGLE_USER, Urgency.MEDIUM): Priority.P3,
        (Impact.SINGLE_USER, Urgency.HIGH): Priority.P2,
        (Impact.DEPARTMENT, Urgency.LOW): Priority.P3,
        (Impact.DEPARTMENT, Urgency.MEDIUM): Priority.P2,
        (Impact.DEPARTMENT, Urgency.HIGH): Priority.P1,
        (Impact.ORGANIZATION, Urgency.LOW): Priority.P2,
        (Impact.ORGANIZATION, Urgency.MEDIUM): Priority.P1,
        (Impact.ORGANIZATION, Urgency.HIGH): Priority.P1,
    }
    for (impact, urgency), exp_priority in expected.items():
        result = resolve_priority(impact, urgency)
        check(
            f"{impact.value:12s} x {urgency.value:8s} -> {exp_priority.value}",
            result.priority == exp_priority,
            f"got {result.priority}",
        )
        check(
            f"  ...no safe-default flag on a fully-specified case",
            result.used_safe_default is False,
        )


def test_priority_is_monotonic_in_impact():
    print("\n[monotonic] higher impact never produces a LOWER-numbered priority need than a lower impact, at fixed urgency")
    order = [Priority.P4, Priority.P3, Priority.P2, Priority.P1]  # increasing severity
    for urgency in (Urgency.LOW, Urgency.MEDIUM, Urgency.HIGH):
        single = resolve_priority(Impact.SINGLE_USER, urgency).priority
        dept = resolve_priority(Impact.DEPARTMENT, urgency).priority
        org = resolve_priority(Impact.ORGANIZATION, urgency).priority
        check(
            f"single_user <= department <= organization at urgency={urgency.value}",
            order.index(single) <= order.index(dept) <= order.index(org),
            f"got {single.value}, {dept.value}, {org.value}",
        )


def test_priority_is_monotonic_in_urgency():
    print("\n[monotonic] higher urgency never produces a lower-severity priority than lower urgency, at fixed impact")
    order = [Priority.P4, Priority.P3, Priority.P2, Priority.P1]
    for impact in (Impact.SINGLE_USER, Impact.DEPARTMENT, Impact.ORGANIZATION):
        low = resolve_priority(impact, Urgency.LOW).priority
        med = resolve_priority(impact, Urgency.MEDIUM).priority
        high = resolve_priority(impact, Urgency.HIGH).priority
        check(
            f"low <= medium <= high at impact={impact.value}",
            order.index(low) <= order.index(med) <= order.index(high),
            f"got {low.value}, {med.value}, {high.value}",
        )


def test_unknown_impact_escalates_not_deescalates():
    print("\n[unknown] unknown IMPACT must never resolve to something LESS severe than single_user")
    order = [Priority.P4, Priority.P3, Priority.P2, Priority.P1]
    for urgency in (Urgency.LOW, Urgency.MEDIUM, Urgency.HIGH):
        baseline = resolve_priority(Impact.SINGLE_USER, urgency).priority
        unknown_result = resolve_priority(Impact.UNKNOWN, urgency)
        check(
            f"unknown impact @ urgency={urgency.value} is >= single_user's priority",
            order.index(unknown_result.priority) >= order.index(baseline),
            f"unknown={unknown_result.priority.value} baseline={baseline.value}",
        )
        check(f"  ...flagged as a safe-default decision", unknown_result.used_safe_default is True)


def test_unknown_urgency_escalates_not_deescalates():
    print("\n[unknown] unknown URGENCY must never resolve to something LESS severe than low")
    order = [Priority.P4, Priority.P3, Priority.P2, Priority.P1]
    for impact in (Impact.SINGLE_USER, Impact.DEPARTMENT, Impact.ORGANIZATION):
        baseline = resolve_priority(impact, Urgency.LOW).priority
        unknown_result = resolve_priority(impact, Urgency.UNKNOWN)
        check(
            f"unknown urgency @ impact={impact.value} is >= low-urgency's priority",
            order.index(unknown_result.priority) >= order.index(baseline),
            f"unknown={unknown_result.priority.value} baseline={baseline.value}",
        )
        check(f"  ...flagged as a safe-default decision", unknown_result.used_safe_default is True)


def test_both_unknown():
    print("\n[unknown] both impact and urgency unknown — must still resolve, safely, without crashing")
    result = resolve_priority(Impact.UNKNOWN, Urgency.UNKNOWN)
    check("resolves to a real priority", isinstance(result.priority, Priority))
    check("flagged as safe-default", result.used_safe_default is True)
    check(
        "not the lowest priority (P4) when everything is unknown",
        result.priority != Priority.P4,
        f"got {result.priority.value}",
    )


def test_reasoning_is_human_readable():
    print("\n[reasoning] every result carries an inspectable reasoning string")
    result = resolve_priority(Impact.ORGANIZATION, Urgency.HIGH)
    check("reasoning mentions impact", "organization" in result.reasoning.lower())
    check("reasoning mentions urgency", "high" in result.reasoning.lower())
    check("reasoning mentions the resulting priority", "P1" in result.reasoning)

    unknown_result = resolve_priority(Impact.UNKNOWN, Urgency.LOW)
    check(
        "reasoning explicitly flags the unknown-default substitution",
        "unspecified" in unknown_result.reasoning.lower() or "default" in unknown_result.reasoning.lower(),
        unknown_result.reasoning,
    )


def main():
    print("=" * 74)
    print("Priority rules engine — exhaustive tests")
    print("=" * 74)

    test_every_matrix_cell()
    test_priority_is_monotonic_in_impact()
    test_priority_is_monotonic_in_urgency()
    test_unknown_impact_escalates_not_deescalates()
    test_unknown_urgency_escalates_not_deescalates()
    test_both_unknown()
    test_reasoning_is_human_readable()

    print("\n" + "=" * 74)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("Rules engine is monotonic, exhaustively covered, and fails safe on ambiguity.")


if __name__ == "__main__":
    main()
