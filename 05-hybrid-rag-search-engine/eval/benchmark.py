"""
Scale benchmark: measures query latency vs corpus size for each vector
backend, so the "when do I need to upgrade" advice in the README is a
measured number rather than a guess.

Uses synthetic random embeddings written straight to a throwaway DB — it
measures the retrieval path, not embedding quality, so real text isn't
needed and the run stays fast.

Run:  python eval/benchmark.py
      python eval/benchmark.py --sizes 1000,10000,50000
"""
import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_tmpdir = Path(tempfile.mkdtemp(prefix="rag_bench_"))
config.DB_PATH = _tmpdir / "bench.sqlite3"

from app import db, vector_index  # noqa: E402

DIM = 384
ACL_GROUPS = ["", ",public,", ",management,", ",finance,"]


def seed_corpus(n_chunks: int) -> None:
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db.init_db()
    rng = np.random.default_rng(42)

    with db.session() as conn:
        conn.execute(
            "INSERT INTO documents (source_path, title, content_hash, acl_groups) VALUES (?,?,?,?)",
            ("bench://synthetic", "Benchmark Doc", "hash", ""),
        )
        doc_id = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]

        batch = 2000
        for start in range(0, n_chunks, batch):
            count = min(batch, n_chunks - start)
            vecs = rng.standard_normal((count, DIM)).astype(np.float32)
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
            rows = []
            for i in range(count):
                idx = start + i
                acl = ACL_GROUPS[idx % len(ACL_GROUPS)]
                rows.append(
                    (
                        doc_id,
                        idx,
                        "Section",
                        f"chunk text {idx} lorem ipsum dolor sit amet",
                        f"chunk text {idx} lorem ipsum dolor sit amet",
                        acl,
                        db.pack_embedding(vecs[i]),
                        DIM,
                    )
                )
            conn.executemany(
                """INSERT INTO chunks
                   (document_id, chunk_index, section, text, raw_text, acl_groups, embedding, embedding_dim)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )


def time_searches(backend: str, n_queries: int, groups: list[str]) -> tuple[float, float]:
    """Returns (cold_ms, warm_mean_ms)."""
    config.VECTOR_INDEX_BACKEND = backend
    vector_index.reset_index()
    index = vector_index.get_index()

    rng = np.random.default_rng(7)
    queries = rng.standard_normal((n_queries, DIM)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)

    t0 = time.perf_counter()
    index.search(queries[0], groups, 20)  # includes build/load cost
    cold_ms = (time.perf_counter() - t0) * 1000

    times = []
    for q in queries[1:]:
        t = time.perf_counter()
        index.search(q, groups, 20)
        times.append((time.perf_counter() - t) * 1000)
    return cold_ms, float(np.mean(times)) if times else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="1000,10000,50000")
    parser.add_argument("--queries", type=int, default=15)
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]

    print("=" * 78)
    print("Vector search scaling benchmark (synthetic embeddings, dim=384)")
    print(f"{args.queries} queries per measurement; 'warm' excludes index build/load")
    print("=" * 78)
    print(f"\n{'chunks':>8} {'backend':>12} {'cold ms':>10} {'warm ms':>10} {'note':>28}")
    print("-" * 78)

    try:
        for n in sizes:
            seed_corpus(n)
            for backend in ("bruteforce", "hnsw"):
                try:
                    cold, warm = time_searches(backend, args.queries, ["public"])
                    note = ""
                    if backend == "bruteforce" and warm > 100:
                        note = "exceeds 100ms budget"
                    print(f"{n:>8} {backend:>12} {cold:>10.1f} {warm:>10.2f} {note:>28}")
                except Exception as e:  # noqa: BLE001
                    print(f"{n:>8} {backend:>12} {'-':>10} {'-':>10} {str(e)[:28]:>28}")
        print("\nNotes:")
        print("  - 'cold' includes building/loading the index; it is paid once per process")
        print("    (and again after every ingest, which invalidates the cache).")
        print("  - 'warm' is the per-query cost users actually experience.")
        print("  - bruteforce is exact; hnsw is approximate (recall < 100%).")
    finally:
        shutil.rmtree(_tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
