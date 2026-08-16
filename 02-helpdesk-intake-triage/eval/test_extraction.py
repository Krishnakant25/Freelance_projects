"""
Tests for structured extraction — both the rule-based fallback (LLM_PROVIDER
=none) and the mock-provider path (proves invalid LLM enum values never
silently become a wrong-but-valid classification).

Run:  python eval/test_extraction.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import config  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


def test_rule_based_category_detection():
    print("\n[rule-based] category keyword matching")
    config.LLM_PROVIDER = "none"
    from app.extraction import extract_incident

    cases = [
        ("My VPN keeps disconnecting every few minutes", "network"),
        ("The printer on the 3rd floor won't turn on", "hardware"),
        ("Excel keeps crashing when I open large spreadsheets", "software"),
        ("I got locked out of my account after too many failed logins", "access"),
        ("I'm not receiving any emails in Outlook since this morning", "email"),
    ]
    for text, expected in cases:
        result = extract_incident(text)
        check(f"{expected}: {text[:50]!r}", result.category == expected, f"got {result.category}")


def test_rule_based_impact_scope():
    print("\n[rule-based] impact scope detection")
    config.LLM_PROVIDER = "none"
    from app.extraction import extract_incident
    from app.rules_engine import Impact

    cases = [
        ("Everyone in the company can't access the VPN", Impact.ORGANIZATION),
        ("My whole team can't reach the shared drive", Impact.DEPARTMENT),
        ("Just my laptop won't turn on", Impact.SINGLE_USER),
        ("Something is wrong with the printer", Impact.UNKNOWN),
    ]
    for text, expected in cases:
        result = extract_incident(text)
        check(f"{expected.value}: {text[:50]!r}", result.impact == expected, f"got {result.impact}")


def test_rule_based_urgency():
    print("\n[rule-based] urgency detection")
    config.LLM_PROVIDER = "none"
    from app.extraction import extract_incident
    from app.rules_engine import Urgency

    cases = [
        ("This is urgent, I need this fixed immediately", Urgency.HIGH),
        ("No rush on this, whenever you get a chance", Urgency.LOW),
        ("My monitor has a flickering issue", Urgency.UNKNOWN),
        # Regression: "urgent" is a substring of "not urgent" — a naive
        # HIGH-checked-first order misclassified this as HIGH. See the
        # comment in app/extraction.py for why LOW is checked first.
        ("Just my monitor has a weird flicker, not urgent", Urgency.LOW),
        ("This can wait, no rush at all", Urgency.LOW),
    ]
    for text, expected in cases:
        result = extract_incident(text)
        check(f"{expected.value}: {text[:50]!r}", result.urgency == expected, f"got {result.urgency}")


def test_rule_based_never_crashes_on_garbage():
    print("\n[rule-based] doesn't crash on empty/weird input")
    config.LLM_PROVIDER = "none"
    from app.extraction import extract_incident

    for text in ["", "   ", "asdkfjaskldfj", "🎉🎉🎉", "a" * 5000]:
        try:
            result = extract_incident(text)
            check(f"handled {text[:20]!r}", result is not None)
        except Exception as e:  # noqa: BLE001
            check(f"handled {text[:20]!r}", False, f"raised {e}")


def test_mock_provider_normal():
    print("\n[mock] normal extraction produces valid enums")
    config.LLM_PROVIDER = "mock"
    import app.extraction as extraction_module

    extraction_module.set_mock_behaviour("normal")
    result = extraction_module.extract_incident("my laptop screen is flickering")
    check("provider recorded", result.provider == "mock")
    check("category is a real category", result.category in extraction_module.CATEGORIES)


def test_mock_provider_invalid_enum_never_leaks():
    print("\n[mock] THE KEY TEST — a model returning garbage enum values must not produce a wrong-but-valid classification")
    config.LLM_PROVIDER = "mock"
    import app.extraction as extraction_module
    from app.rules_engine import Impact, Urgency

    extraction_module.set_mock_behaviour("invalid_enum")
    result = extraction_module.extract_incident("something is wrong")
    check(
        "invalid impact value falls back to UNKNOWN, not silently accepted",
        result.impact == Impact.UNKNOWN,
        f"got {result.impact}",
    )
    check(
        "invalid urgency value falls back to UNKNOWN, not silently accepted",
        result.urgency == Urgency.UNKNOWN,
        f"got {result.urgency}",
    )
    check(
        "invalid category falls back to 'other', not silently accepted",
        result.category == "other",
        f"got {result.category}",
    )
    check(
        "the substitution is recorded in confidence_notes (not silent)",
        any("invalid" in n.lower() for n in result.confidence_notes),
        str(result.confidence_notes),
    )


def test_mock_provider_malformed_json_falls_back():
    print("\n[mock] malformed JSON response falls back to rule-based extraction, doesn't crash")
    config.LLM_PROVIDER = "mock"
    import app.extraction as extraction_module

    extraction_module.set_mock_behaviour("malformed")
    result = extraction_module.extract_incident("everyone's VPN is down, this is urgent")
    check("did not crash, returned a result", result is not None)
    check("provider shows the fallback happened", "failed" in result.provider, result.provider)
    check(
        "fallback still extracts something sensible from rule-based logic",
        result.urgency.value == "high",
        f"got {result.urgency}",
    )


def main():
    print("=" * 74)
    print("Structured extraction tests")
    print("=" * 74)

    test_rule_based_category_detection()
    test_rule_based_impact_scope()
    test_rule_based_urgency()
    test_rule_based_never_crashes_on_garbage()
    test_mock_provider_normal()
    test_mock_provider_invalid_enum_never_leaks()
    test_mock_provider_malformed_json_falls_back()

    print("\n" + "=" * 74)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("Extraction never lets an invalid model output become a silently-wrong classification.")


if __name__ == "__main__":
    main()
