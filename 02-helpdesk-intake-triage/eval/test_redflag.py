"""
Tests for the red-flag keyword scanner — the override that must work
regardless of what the LLM does.

Run:  python eval/test_redflag.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.redflag import add_pattern, scan  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


POSITIVE_CASES = [
    ("Our systems appear to have been hit by ransomware overnight", "security"),
    ("We think there's been a data breach involving customer records", "security"),
    ("Someone reported unauthorized access to the finance shared drive", "security"),
    ("I clicked a link in an email and now I think it was phishing", "security"),
    ("My laptop says a virus was detected and quarantined", "security"),
    ("I got an alert about suspicious login activity on my account", "security"),
    ("The whole system is down for everyone in the Chicago office", "outage"),
    ("Entire company is unable to access email this morning", "outage"),
    ("All employees can't log in to the VPN right now", "outage"),
    ("We're seeing a company-wide outage affecting every department", "outage"),
    ("Production is down and customers are seeing errors", "outage"),
    ("Payroll is broken and today is payday", "business-critical"),
    ("Can't process payments on the checkout page, this is urgent", "business-critical"),
]

NEGATIVE_CASES = [
    "My mouse isn't working properly",
    "I need a new monitor for my desk",
    "Can you reset my password, I forgot it",
    "The printer on the 3rd floor is out of toner",
    "I'd like access to the marketing shared drive",
    "My email signature needs updating",
    "The wifi in the break room is a bit slow",
    "Requesting a software license for Photoshop",
]


def test_positive_cases():
    print("\n[positive] known dangerous phrases must be caught")
    for text, expected_category in POSITIVE_CASES:
        result = scan(text)
        check(
            f"matched: {text[:55]!r}",
            result.matched and result.category == expected_category,
            f"matched={result.matched} category={result.category!r} (expected {expected_category!r})",
        )


def test_negative_cases():
    print("\n[negative] routine tickets must NOT be flagged")
    for text in NEGATIVE_CASES:
        result = scan(text)
        check(f"not matched: {text[:55]!r}", not result.matched, f"false positive: {result.category}")


def test_case_insensitive():
    print("\n[case] matching is case-insensitive")
    result = scan("RANSOMWARE detected on file server")
    check("uppercase matches", result.matched and result.category == "security")


def test_empty_and_none_input():
    print("\n[edge] empty/missing input doesn't crash")
    check("empty string", scan("").matched is False)
    check("whitespace only", scan("   ").matched is False)


def test_multiple_matches_all_recorded():
    print("\n[multi] a ticket matching several patterns records all of them")
    result = scan("We had unauthorized access AND the entire company is unable to log in")
    check("matched", result.matched)
    check("recorded more than one match", len(result.all_matches) >= 2, str(result.all_matches))


def test_operator_can_extend_list():
    print("\n[extend] a client-specific pattern can be added at runtime")
    before = scan("Our custom internal tool CargoTrack is completely broken for the whole warehouse team")
    check("not matched before adding the pattern", not before.matched)
    add_pattern(r"\bcargotrack\b.*\bbroken\b", "business-critical")
    after = scan("Our custom internal tool CargoTrack is completely broken for the whole warehouse team")
    check("matched after adding the pattern", after.matched and after.category == "business-critical")


def main():
    print("=" * 74)
    print("Red-flag keyword scanner tests")
    print("=" * 74)

    test_positive_cases()
    test_negative_cases()
    test_case_insensitive()
    test_empty_and_none_input()
    test_multiple_matches_all_recorded()
    test_operator_can_extend_list()

    print("\n" + "=" * 74)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("Red-flag scanner catches known-dangerous phrases without false-positiving on routine tickets.")


if __name__ == "__main__":
    main()
