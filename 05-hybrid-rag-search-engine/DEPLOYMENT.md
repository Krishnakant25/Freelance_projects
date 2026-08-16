# Deployment & Scaling Guide

Honest operational guidance, with measured numbers rather than guesses. Read [`MANUAL_STEPS.md`](MANUAL_STEPS.md) first for what you must do by hand.

---

## 1. Readiness status

| Area | Status |
|---|---|
| Retrieval, reranking, citations | Tested — 31/31 eval cases |
| ACL filtering + authentication | Tested — API keys, escalation attempts blocked |
| Generation path + citation verification | Tested — 15 assertions, adversarial cases |
| Query cache ACL isolation | Tested — 19 assertions |
| PDF/text ingestion | Tested on a real 5-page PDF |
| Refusal behaviour ("not in corpus") | Tested + threshold calibrated on data |
| **Horizontal scaling (multi-worker)** | **Not ready — see §4** |
| **Postgres backend** | **Not implemented — SQLite only** |
| **TLS / HTTPS** | **Not implemented — terminate at a proxy** |
| **OCR for scanned PDFs** | **Not implemented** |

**Verdict: ready for a single-instance internal deployment at a small-to-medium firm.** Not ready for multi-tenant SaaS or multi-worker horizontal scale without the changes in §4.

---

## 2. Measured performance

Run `python eval/benchmark.py` and `python eval/benchmark_pipeline.py` to reproduce on your hardware.

### Query latency breakdown (41-chunk corpus, CPU)

| Stage | Time | Share |
|---|---:|---:|
| Embed query | 14.3 ms | 8.0% |
| Vector search | 0.2 ms | 0.1% |
| Keyword search (FTS5) | 1.3 ms | 0.7% |
| **Rerank (cross-encoder)** | **161.3 ms** | **90.4%** |
| **Total** | **178.4 ms** | |

### Vector search scaling (synthetic, dim=384)

| Chunks | Brute-force (warm) | FAISS HNSW (warm) | HNSW build cost |
|---:|---:|---:|---:|
| 1,000 | 0.67 ms | 0.29 ms | 0.13 s |
| 10,000 | 4.09 ms | 0.92 ms | 1.29 s |
| 50,000 | 20.36 ms | 4.47 ms | **17.7 s** |

### What this actually means

**The reranker is the bottleneck, not vector search.** At 50,000 chunks, brute-force vector search costs 20 ms against a 161 ms reranker — still under 13% of query time. The intuitive "add an ANN index to scale" move would optimise a stage that is a rounding error, while adding a 17.7-second index rebuild after *every ingest*.

Practical guidance, in priority order:

1. **Under ~100k chunks: keep `VECTOR_INDEX_BACKEND=bruteforce`.** It is exact, has no rebuild cost, and is not your bottleneck.
2. **To make queries faster, attack the reranker first:**
   - The query cache already eliminates it entirely on repeat questions (measured: 162 ms → 0.1 ms).
   - Lower `RETRIEVAL_TOP_K_FUSED` — reranking cost is linear in candidate count.
   - Move the reranker to a GPU, or to Cohere Rerank (a network call, but off the CPU).
   - `RERANK_ENABLED=false` is a last resort — it measurably hurts result quality.
3. **Past ~200k chunks, switch to `VECTOR_INDEX_BACKEND=hnsw`** (requires `pip install faiss-cpu`). Accept that it is approximate and that ingest triggers a rebuild.

---

## 3. Single-instance deployment (recommended starting point)

Suitable for: internal tool, one team or company, tens of concurrent users, corpus under ~100k chunks.

```bash
pip install -r requirements.txt
cp .env.example .env          # then edit — see MANUAL_STEPS.md
python scripts/manage_keys.py create --name admin --groups management,public --can-ingest
python run_all_tests.py       # must pass before deploying
uvicorn app.api:app --host 127.0.0.1 --port 8000
```

Put a reverse proxy (nginx/Caddy) in front for TLS termination. **Do not expose uvicorn directly to the internet.**

### Why a single worker

Three pieces of state live in process memory and are **not shared between workers**:

| State | Consequence of multiple workers |
|---|---|
| Query cache (`app/cache.py`) | Each worker has its own; hit rate drops, memory multiplies |
| Vector index cache (`app/vector_index.py`) | Each worker loads its own copy of all embeddings |
| Rate limiter (`app/rate_limit.py`) | Each enforces the limit independently → effective limit is N× configured |

Additionally, **SQLite allows only one writer at a time.** Concurrent ingestion from multiple workers will produce `database is locked` errors.

Run with `--workers 1`. To serve more concurrent readers, scale vertically (more CPU for the reranker) before adding workers.

---

## 4. What multi-worker / horizontal scale requires

These are **not implemented**. Do not assume the current code handles them.

| Requirement | Change needed |
|---|---|
| Shared query cache | Move `app/cache.py` to Redis. Keep the ACL-group cache key — see the warning in that file. |
| Shared rate limiting | Move `app/rate_limit.py` to Redis, or enforce at the API gateway. |
| Concurrent writes | Migrate SQLite → Postgres. SQLite's single-writer limit is the hard blocker. |
| Shared vector index | Postgres + `pgvector`, or a dedicated vector DB (Qdrant/Weaviate). |
| Index rebuild coordination | With HNSW, workers each rebuild independently after ingest. Needs a shared index or a rebuild-and-publish step. |

### Postgres migration sketch

The storage layer (`app/db.py`) is the only module with SQL in it, which keeps this contained but **not free**:

1. Replace the `chunks.embedding` BLOB with a `pgvector` column.
2. Replace the FTS5 virtual table with a `tsvector` column + GIN index.
3. Port `acl_where_clause` — the logic is portable, the `LIKE` pattern matching should become a proper array/`&&` overlap operator in Postgres, which is both faster and clearer.
4. Replace the brute-force vector scan with a `pgvector` ANN index (IVFFlat or HNSW).
5. Re-run the full suite — **especially the ACL tests.** The access-control logic is the part most likely to break silently in a port, and it is the part where a bug is a data breach rather than a quality regression.

Budget this as real work, not a config change.

---

## 5. Operational notes

**Cold start.** The first query after startup loads the embedding model and reranker — measured at ~15 s. Subsequent queries are ~180 ms. Send a warm-up query after deploy; do not let a user's first request pay this.

**Memory.** Roughly 500 MB–1 GB resident (models dominate). Embeddings add ~1.5 KB per chunk (384 × float32), so 100k chunks ≈ 150 MB.

**Backups.** Everything lives in one SQLite file (`data/db/rag.sqlite3`). Back it up on a schedule; it can be copied while running if you use `sqlite3 .backup`. `keys.json` should be backed up **separately and securely** — it holds credential hashes.

**Re-indexing after a pipeline change.** Bump `PIPELINE_VERSION` in `.env` and re-run ingest. Without this, `ingest` reports `unchanged` and silently keeps chunks built by the old logic. This is deliberate, and it caught a real bug during development.

**Log aggregation.** Set `LOG_JSON=true` for structured logs. Every request carries an `X-Request-ID` (echoed in the response header) for tracing.

---

## 6. Security checklist before going live

- [ ] `AUTH_ENABLED=true` (the startup log screams if it isn't)
- [ ] `keys.json` is **not** in version control (it is gitignored — verify)
- [ ] Each consumer has its own key with the **narrowest** group set that works
- [ ] `can_ingest` granted only to keys that genuinely need to write
- [ ] TLS terminated at a proxy; uvicorn bound to `127.0.0.1`, not `0.0.0.0`
- [ ] `python run_all_tests.py` passes — the ACL tests are the ones that matter
- [ ] Rate limits reviewed for your traffic (and enforced at the proxy if multi-worker)
- [ ] Document ACL groups reviewed — **an over-permissive ingest is the most likely real-world leak**, and no amount of correct filtering code compensates for a restricted document ingested as public

That last point deserves emphasis: the code correctly enforces whatever ACLs you assign. It cannot tell you that you assigned the wrong one. Audit what you ingest.
