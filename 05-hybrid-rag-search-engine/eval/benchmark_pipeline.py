"""
End-to-end query latency breakdown: embed -> vector search -> keyword search
-> rerank.

The vector-search benchmark (benchmark.py) measures one stage in isolation,
which is exactly how people end up optimising the wrong thing. This measures
each stage of a real query so the actual bottleneck is visible rather than
assumed.

Run against the real sample corpus:
    python eval/benchmark_pipeline.py
"""
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db  # noqa: E402
from app.embeddings import embed_query  # noqa: E402
from app.rerank import rerank  # noqa: E402
from app.retrieval import _keyword_search, hybrid_search  # noqa: E402
from app.vector_index import get_index  # noqa: E402

QUESTIONS = [
    "How many vacation days do employees get?",
    "What is the API rate limit on the Starter plan?",
    "What security certification does the product have?",
    "How much does the Growth plan cost?",
    "What are the core hours for remote employees?",
]


def timeit(fn, n=5):
    fn()  # warm
    times = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t) * 1000)
    return float(np.mean(times))


def main():
    db.init_db()
    with db.session() as conn:
        chunks = conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]

    print("=" * 74)
    print(f"Query pipeline latency breakdown  (corpus: {chunks} chunks)")
    print(f"backend={config.VECTOR_INDEX_BACKEND}  rerank_enabled={config.RERANK_ENABLED}")
    print("=" * 74)

    q = QUESTIONS[0]
    groups = ["public"]

    t_embed = timeit(lambda: embed_query(q))
    qv = embed_query(q)

    index = get_index()
    index.search(qv, groups, 20)  # ensure built
    t_vector = timeit(lambda: index.search(qv, groups, 20))

    def kw():
        with db.session() as conn:
            _keyword_search(conn, q, groups, 20)

    t_keyword = timeit(kw)

    candidates = hybrid_search(q, user_groups=groups)
    t_rerank = timeit(lambda: rerank(q, list(candidates)))

    t_total = timeit(lambda: hybrid_search(q, user_groups=groups))
    t_total_rerank = t_total + t_rerank

    print(f"\n{'stage':<28} {'ms':>9}   {'% of total':>10}")
    print("-" * 74)
    rows = [
        ("embed query", t_embed),
        ("vector search", t_vector),
        ("keyword search (FTS5)", t_keyword),
        ("rerank (cross-encoder)", t_rerank),
    ]
    for name, ms in rows:
        pct = ms / t_total_rerank * 100 if t_total_rerank else 0
        bar = "#" * max(1, int(pct / 3))
        print(f"{name:<28} {ms:>9.2f}   {pct:>9.1f}%  {bar}")
    print("-" * 74)
    print(f"{'TOTAL (search + rerank)':<28} {t_total_rerank:>9.2f}")

    print("\nInterpretation:")
    dominant = max(rows, key=lambda r: r[1])
    print(f"  Dominant stage: {dominant[0]} ({dominant[1]:.1f} ms)")
    if dominant[0].startswith("rerank"):
        print(
            "  Vector search is NOT the bottleneck at this corpus size. Swapping in an\n"
            "  ANN index would optimise a stage that is already a rounding error, while\n"
            "  adding an index-rebuild cost on every ingest. Reduce reranked candidate\n"
            "  count, or move the reranker to a GPU/hosted API, before touching the\n"
            "  vector backend."
        )


if __name__ == "__main__":
    main()
