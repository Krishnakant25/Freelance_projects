"""
Calibrates ABSOLUTE_RELEVANCE_FLOOR against the labeled eval set.

The floor decides "is the best retrieved chunk actually relevant, or is this
a question the corpus can't answer?" It was originally set from 4 hand-picked
examples, which is not enough to separate the two populations — and the
expanded eval set duly caught two failures caused by it being too permissive.

This script prints the top-rerank-score distribution for questions that
SHOULD be answered vs questions that SHOULD be refused, then sweeps candidate
thresholds and reports accuracy at each, so the value in config.py is chosen
from evidence instead of intuition.

Run:  python eval/calibrate_threshold.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

config.LLM_PROVIDER = "none"

from app.rerank import rerank  # noqa: E402
from app.retrieval import hybrid_search  # noqa: E402

EVAL_SET_PATH = Path(__file__).resolve().parent / "eval_set.json"


def top_score(question: str, groups: list[str]):
    candidates = hybrid_search(question, user_groups=groups, top_k=8)
    if not candidates:
        return None
    ranked = rerank(question, candidates)
    if not ranked or ranked[0].rerank_score is None:
        return None
    return ranked[0].rerank_score


def main():
    eval_set = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
    should_answer = []   # (id, score)
    should_refuse = []   # (id, score)

    print("Scoring eval cases (this runs the reranker on every case)...\n")

    for case in eval_set["cases"]:
        groups = case.get("user_groups", [])
        score = top_score(case["question"], groups)

        if case.get("refusal_test"):
            should_refuse.append((case["id"], score))
        elif case.get("acl_test") == "must_not_contain":
            # Restricted content is invisible to this caller, so from their
            # perspective the corpus cannot answer — the correct behaviour is
            # to refuse, not to return an unrelated public near-miss.
            should_refuse.append((case["id"], score))
        else:
            should_answer.append((case["id"], score))

    def summarize(label, rows):
        scored = [(i, s) for i, s in rows if s is not None]
        print(f"{label} (n={len(rows)}, with candidates={len(scored)})")
        if scored:
            scores = sorted(s for _, s in scored)
            print(f"  min={scores[0]:+.2f}  median={scores[len(scores)//2]:+.2f}  max={scores[-1]:+.2f}")
            for cid, s in sorted(scored, key=lambda x: x[1]):
                print(f"    {s:+7.2f}  {cid}")
        none_rows = [i for i, s in rows if s is None]
        if none_rows:
            print(f"  no candidates at all (auto-refused): {none_rows}")
        print()
        return scored

    print("=" * 78)
    answer_scored = summarize("SHOULD ANSWER", should_answer)
    refuse_scored = summarize("SHOULD REFUSE", should_refuse)

    print("=" * 78)
    print("THRESHOLD SWEEP")
    print("A case is answered when top_score >= threshold.\n")
    print(f"{'threshold':>10} {'answer-ok':>10} {'refuse-ok':>10} {'total':>8}")
    print("-" * 42)

    candidates = [round(x * 0.5, 1) for x in range(-24, 9)]
    best = None
    for t in candidates:
        # No candidates at all == refused regardless of threshold.
        ans_ok = sum(1 for _, s in answer_scored if s >= t)
        ref_ok = sum(1 for _, s in refuse_scored if s < t) + (
            len(should_refuse) - len(refuse_scored)
        )
        total = ans_ok + ref_ok
        n_total = len(should_answer) + len(should_refuse)
        marker = ""
        if best is None or total > best[1]:
            best = (t, total)
            marker = "  <-- best so far"
        print(f"{t:>10.1f} {ans_ok:>4}/{len(should_answer):<5} {ref_ok:>4}/{len(should_refuse):<5} {total:>3}/{n_total:<4}{marker}")

    print("\n" + "=" * 78)
    if best:
        n_total = len(should_answer) + len(should_refuse)
        print(f"Best threshold: {best[0]:+.1f}  ({best[1]}/{n_total} correct)")
        print(f"Current config: {config.ABSOLUTE_RELEVANCE_FLOOR:+.1f}")
        if abs(best[0] - config.ABSOLUTE_RELEVANCE_FLOOR) > 0.01:
            print(f"\n  -> Update ABSOLUTE_RELEVANCE_FLOOR to {best[0]:+.1f} in app/config.py or .env")
        else:
            print("\n  -> Current value is already optimal for this eval set.")
    print(
        "\nNOTE: this optimises against THIS corpus and eval set. Re-run after any\n"
        "change to the corpus, embedding model, reranker, or chunking. If the two\n"
        "distributions overlap heavily, no single threshold separates them and the\n"
        "answer is a better relevance signal, not a better cutoff."
    )


if __name__ == "__main__":
    main()
