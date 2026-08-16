"""
Tests for the LLM generation + citation-verification path.

WHY THIS EXISTS: this code path shipped untested. Every prior run used
LLM_PROVIDER=none (extractive mode), which returns passages verbatim and
never exercises JSON parsing, citation extraction, or verification. So the
part of the system whose entire job is "don't let the model lie to the user"
had zero test coverage.

These use the deterministic `mock` provider — no API key, no network, no
cost — so they run in CI on every change.

Run:  python eval/test_generation.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

config.LLM_PROVIDER = "mock"

from app import generate  # noqa: E402
from app.generate import generate_answer  # noqa: E402
from app.retrieval import RetrievedChunk  # noqa: E402

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


def make_chunks() -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=42,
            document_id=1,
            document_title="Employee Handbook",
            source_path="data/sample_docs/employee_handbook.md",
            section="Leave Policy",
            raw_text=(
                "Full-time employees accrue 18 days of paid vacation per calendar year, "
                "credited monthly at a rate of 1.5 days. Unused vacation days roll over "
                "up to a maximum of 10 days into the following year."
            ),
            text="[Source: Employee Handbook > Leave Policy] Full-time employees accrue 18 days...",
        ),
        RetrievedChunk(
            chunk_id=43,
            document_id=1,
            document_title="Employee Handbook",
            source_path="data/sample_docs/employee_handbook.md",
            section="Remote Work Policy",
            raw_text="Employees may work remotely up to 3 days per week with manager approval.",
            text="[Source: Employee Handbook > Remote Work Policy] Employees may work remotely...",
        ),
    ]


def test_normal_answer():
    print("\n[normal] model cites a real, supported chunk")
    generate.set_mock_behaviour("normal")
    result = generate_answer("How many vacation days?", make_chunks())
    check("returns an answer", bool(result.answer.strip()))
    check("has exactly one citation", len(result.citations) == 1, f"got {len(result.citations)}")
    if result.citations:
        c = result.citations[0]
        check("citation points at a retrieved chunk", c.chunk_id == 42, f"got {c.chunk_id}")
        check("citation is marked verified", c.verified, f"overlap={c.overlap_score}")
    check("not flagged insufficient", not result.insufficient_evidence)


def test_hallucinated_citation_is_dropped():
    print("\n[hallucinate_cite] model cites a chunk ID that was never retrieved")
    generate.set_mock_behaviour("hallucinate_cite")
    result = generate_answer("How many vacation days?", make_chunks())
    # THE KEY ASSERTION: a citation to a non-retrieved chunk must never reach
    # the user. Verification is the whole trust mechanism of this product.
    check(
        "fabricated citation is dropped entirely",
        len(result.citations) == 0,
        f"leaked {[c.chunk_id for c in result.citations]}",
    )


def test_unsupported_claim_is_flagged():
    print("\n[unsupported_claim] answer text unrelated to the cited chunk")
    generate.set_mock_behaviour("unsupported_claim")
    result = generate_answer("How many vacation days?", make_chunks())
    check("citation is retained for inspection", len(result.citations) == 1)
    if result.citations:
        c = result.citations[0]
        check(
            "citation is marked UNVERIFIED (low overlap)",
            not c.verified,
            f"verified={c.verified} overlap={c.overlap_score}",
        )


def test_insufficient_evidence():
    print("\n[insufficient] model correctly declines")
    generate.set_mock_behaviour("insufficient")
    result = generate_answer("What is the CEO's shoe size?", make_chunks())
    check("insufficient_evidence flag is propagated", result.insufficient_evidence)
    check("no citations returned", len(result.citations) == 0)


def test_malformed_json_falls_back_safely():
    print("\n[malformed_json] model returns prose instead of JSON")
    generate.set_mock_behaviour("malformed_json")
    result = generate_answer("How many vacation days?", make_chunks())
    # Must degrade to extractive rather than raising or returning nothing.
    check("does not crash and returns content", bool(result.answer.strip()))
    check("mentions the failure", "failed" in result.answer.lower())
    check(
        "falls back to real retrieved passages",
        "18 days" in result.answer,
        "extractive fallback did not include the top chunk",
    )


def test_no_chunks():
    print("\n[empty] no chunks retrieved at all")
    generate.set_mock_behaviour("normal")
    result = generate_answer("Anything?", [])
    check("flags insufficient evidence", result.insufficient_evidence)
    check("no citations", len(result.citations) == 0)


def main():
    print("=" * 70)
    print("Generation + citation verification tests (provider=mock)")
    print("=" * 70)

    test_normal_answer()
    test_hallucinated_citation_is_dropped()
    test_unsupported_claim_is_flagged()
    test_insufficient_evidence()
    test_malformed_json_falls_back_safely()
    test_no_chunks()

    print("\n" + "=" * 70)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAILED: {name} {detail}")
        sys.exit(1)
    print("All generation-path assertions passed.")


if __name__ == "__main__":
    main()
