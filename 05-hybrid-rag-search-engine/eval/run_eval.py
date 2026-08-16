"""
Deterministic retrieval evaluation harness (recall@k, MRR, and ACL enforcement
checks). Run on every retrieval/chunking/embedding change — it's fast and
free, unlike an LLM-judged answer-quality eval, so there's no excuse to skip
it. See architecture doc §6.7.

Usage:
    python eval/run_eval.py
    python eval/run_eval.py --skip-ingest   (if sample_docs are already indexed)
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db  # noqa: E402
from app.ingest import ingest_file  # noqa: E402
from app.retrieval import hybrid_search  # noqa: E402
from app.rerank import rerank  # noqa: E402

# Force extractive mode for the eval run regardless of .env: this harness
# must be free and deterministic to run on every change, never dependent on
# an API key or a paid provider being configured.
config.LLM_PROVIDER = "none"

from app.query import answer_question  # noqa: E402

SAMPLE_DOCS = ROOT / "data" / "sample_docs"
EVAL_SET_PATH = Path(__file__).resolve().parent / "eval_set.json"

DOC_ACL = {
    "employee_handbook.md": [],
    "product_faq.md": [],
    "exec_compensation_policy.md": ["management"],
}


def ensure_sample_docs_ingested():
    db.init_db()
    for filename, groups in DOC_ACL.items():
        ingest_file(SAMPLE_DOCS / filename, acl_groups=groups)


def run_case(case: dict) -> dict:
    # Refusal cases are only meaningful through the full pipeline (the
    # relevance floor lives there), so they're evaluated in the second pass.
    if case.get("refusal_test"):
        return {
            "id": case["id"],
            "passed": True,
            "skipped": True,
            "doc_found": False,
            "first_rank": None,
            "keyword_recall": 1.0,
            "keywords_missing": [],
            "acl_test": None,
            "acl_pass": True,
            "num_retrieved": 0,
        }

    candidates = hybrid_search(
        case["question"], user_groups=case.get("user_groups", []), top_k=case.get("top_k", 5)
    )
    ranked = rerank(case["question"], candidates)

    expected_doc = case["expected_document_contains"].lower()
    expected_keywords = [k.lower() for k in case.get("expected_keywords", [])]

    matching_ranks = [
        i for i, c in enumerate(ranked, start=1) if expected_doc in c.document_title.lower()
    ]
    doc_found = len(matching_ranks) > 0
    first_rank = matching_ranks[0] if matching_ranks else None

    combined_text = " ".join(c.raw_text.lower() for c in ranked)
    keywords_found = [kw for kw in expected_keywords if kw in combined_text]
    keyword_recall = len(keywords_found) / len(expected_keywords) if expected_keywords else 1.0

    acl_test = case.get("acl_test")
    acl_pass = True
    if acl_test == "must_not_contain":
        acl_pass = not doc_found
    elif acl_test == "must_contain":
        acl_pass = doc_found

    if acl_test == "must_not_contain":
        # Success here means the restricted content was correctly withheld —
        # zero keyword recall is the expected, correct outcome, not a failure.
        passed = acl_pass
    else:
        passed = keyword_recall == 1.0 and acl_pass and doc_found

    return {
        "id": case["id"],
        "passed": passed,
        "doc_found": doc_found,
        "first_rank": first_rank,
        "keyword_recall": keyword_recall,
        "keywords_missing": [kw for kw in expected_keywords if kw not in combined_text],
        "acl_test": acl_test,
        "acl_pass": acl_pass,
        "num_retrieved": len(ranked),
    }


def run_pipeline_case(case: dict) -> dict:
    """
    Exercises the FULL answer_question() pipeline (retrieval -> rerank ->
    relevance floor -> generation), not just raw retrieval. This exists
    because a real bug shipped in the relevance-floor logic inside
    query.answer_question that the retrieval-only checks in run_case() could
    never have caught: they call hybrid_search()/rerank() directly and skip
    the floor entirely. Any future change to that floor must be checked here.
    """
    result = answer_question(case["question"], user_groups=case.get("user_groups", []))
    expected_doc = (case.get("expected_document_contains") or "").lower()
    cited_docs = [c["document_title"].lower() for c in result["citations"]]
    doc_cited = bool(expected_doc) and any(expected_doc in d for d in cited_docs)

    acl_test = case.get("acl_test")
    if case.get("refusal_test"):
        # The answer genuinely isn't in the corpus. The system must say so
        # rather than return a confident near-miss. This is what calibrates
        # ABSOLUTE_RELEVANCE_FLOOR: too high and real answers get refused,
        # too low and these cases invent one.
        passed = result["insufficient_evidence"] and not result["citations"]
        kind = "refusal"
    elif acl_test == "must_not_contain":
        passed = (not doc_cited) and result["insufficient_evidence"] and not result["citations"]
        kind = "acl"
    elif acl_test == "must_contain":
        passed = doc_cited and not result["insufficient_evidence"]
        kind = "acl"
    else:
        passed = doc_cited and not result["insufficient_evidence"]
        kind = "standard"

    return {
        "id": case["id"],
        "kind": kind,
        "passed": passed,
        "doc_cited": doc_cited,
        "insufficient_evidence": result["insufficient_evidence"],
        "num_citations": len(result["citations"]),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-ingest", action="store_true")
    args = parser.parse_args()

    if not args.skip_ingest:
        print("Ingesting sample_docs (idempotent, safe to re-run)...")
        ensure_sample_docs_ingested()

    eval_set = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
    cases = eval_set["cases"]

    results = [run_case(c) for c in cases]

    print(f"\n{'ID':42s} {'PASS':6s} {'Rank':6s} {'KwRecall':9s} {'ACL':5s}")
    print("-" * 78)
    for r in results:
        if r.get("skipped"):
            continue
        status = "PASS" if r["passed"] else "FAIL"
        rank = str(r["first_rank"]) if r["first_rank"] else "-"
        print(
            f"{r['id']:42s} {status:6s} {rank:6s} {r['keyword_recall']:.2f}     "
            f"{'ok' if r['acl_pass'] else 'FAIL'}"
        )
        if r["keywords_missing"] and not r["acl_test"]:
            print(f"    missing keywords: {r['keywords_missing']}")

    non_acl = [r for r in results if not r["acl_test"] and not r.get("skipped")]
    recall_at_k = sum(1 for r in non_acl if r["doc_found"]) / len(non_acl) if non_acl else 0.0
    mrr = (
        sum(1.0 / r["first_rank"] for r in non_acl if r["first_rank"])
        / len(non_acl)
        if non_acl
        else 0.0
    )
    acl_cases = [r for r in results if r["acl_test"]]
    acl_pass_rate = sum(1 for r in acl_cases if r["acl_pass"]) / len(acl_cases) if acl_cases else 1.0

    scored = [r for r in results if not r.get("skipped")]
    total_pass = sum(1 for r in scored if r["passed"])
    print("\n" + "=" * 78)
    print("RETRIEVAL-ONLY METRICS")
    print(f"  Recall@k (non-ACL cases):  {recall_at_k:.2%}")
    print(f"  MRR (non-ACL cases):       {mrr:.3f}")
    print(f"  ACL enforcement pass rate: {acl_pass_rate:.2%}")
    print(f"  Passed: {total_pass}/{len(scored)}")

    # Second pass: the full answer_question() pipeline, including the
    # relevance-floor logic that the retrieval-only checks above skip.
    print("\n" + "=" * 78)
    print("END-TO-END PIPELINE (retrieval -> rerank -> relevance floor -> generation)")
    print(f"{'ID':42s} {'KIND':10s} {'PASS':6s} {'Cited':7s} {'Refused':8s}")
    print("-" * 78)
    pipeline_results = [run_pipeline_case(c) for c in cases]
    for r in pipeline_results:
        status = "PASS" if r["passed"] else "FAIL"
        print(
            f"{r['id']:42s} {r['kind']:10s} {status:6s} "
            f"{str(r['doc_cited']):7s} {str(r['insufficient_evidence']):8s}"
        )

    pipeline_pass = sum(1 for r in pipeline_results if r["passed"])

    by_kind: dict[str, list] = {}
    for r in pipeline_results:
        by_kind.setdefault(r["kind"], []).append(r)

    print("\n" + "=" * 78)
    print("PIPELINE METRICS BY CATEGORY")
    for kind in ("standard", "acl", "refusal"):
        rows = by_kind.get(kind, [])
        if not rows:
            continue
        passed = sum(1 for r in rows if r["passed"])
        label = {
            "standard": "Answerable questions (must find + cite)",
            "acl": "Access control (must block / must allow)",
            "refusal": "Not in corpus (must refuse, not guess)",
        }[kind]
        print(f"  {label:45s} {passed:>3}/{len(rows):<3} {passed / len(rows):.0%}")
    print(f"\n  TOTAL: {pipeline_pass}/{len(pipeline_results)} cases passed")

    if total_pass < len(scored) or pipeline_pass < len(pipeline_results):
        print("\nFAILURES PRESENT — see rows marked FAIL above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
