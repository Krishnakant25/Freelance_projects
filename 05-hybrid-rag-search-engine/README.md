# Hybrid RAG Search Engine

Domain-specific hybrid search (dense + lexical) with reranking, ACL-filtered retrieval, and verified citations. This is the buildable version of [`05_Hybrid_RAG_Search_Engine.md`](05_Hybrid_RAG_Search_Engine.md) — read that file for the full architecture rationale; this README is setup + what's implemented.

This project is self-contained. It does not share code or data with any other project in `D:\FreelancePortfolio`.

**Start here:** [`MANUAL_STEPS.md`](MANUAL_STEPS.md) (what you must configure by hand) → [`DEPLOYMENT.md`](DEPLOYMENT.md) (scaling, measured numbers, security checklist).

---

## Status

| | |
|---|---|
| Test suite | **65 assertions across 3 suites, all passing** (`python run_all_tests.py`) |
| Retrieval quality | 100% recall@k, MRR 1.0 on a 31-case labeled eval set |
| Access control | Enforced in SQL + at the API; privilege-escalation attempts verified blocked |
| Ready for | Single-instance internal deployment, corpus under ~100k chunks |
| **Not** ready for | Multi-worker horizontal scale, multi-tenant SaaS (see DEPLOYMENT.md §4) |

---

## Roadmap & Tradeoffs

Every choice below was a scope decision made on purpose, with a named trigger for revisiting it. A system that's simple *because the measured scale doesn't justify more* reads very differently from one that's simple because nobody checked.

| Decision | Why | Upgrade trigger |
|---|---|---|
| **SQLite** (FTS5 + brute-force vectors) over Postgres/Qdrant | Zero infra; one file serves both dense and lexical search. Measured: 20 ms vector search at 50k chunks — not the bottleneck. | Past ~100k chunks, or **any** need for concurrent writers/multiple workers (SQLite's single-writer limit is the hard blocker) |
| **Brute-force vector search** over an ANN index | Exact, no rebuild cost. Measured: FAISS HNSW is faster per-query but costs **17.7 s to rebuild at 50k chunks, after every ingest** | Past ~200k chunks. `VECTOR_INDEX_BACKEND=hnsw` is implemented and ACL-correct — it's just not worth it yet |
| **Local cross-encoder reranker** over Cohere Rerank / ColBERT | Good quality, no external dependency. But measured at **90.4% of query latency** — this is the real bottleneck | When query latency matters: cut `RETRIEVAL_TOP_K_FUSED`, move to GPU, or use a hosted reranker. ColBERT-style late interaction is stronger at scale but costs ~10–30× storage (a vector per token) and needs PLAID-style indexing |
| **Local embeddings** (MiniLM, ~80 MB) over OpenAI/Voyage | Free, no key, no rate limits | Retrieval quality on domain vocabulary needs to improve |
| **`LLM_PROVIDER=none`** (extractive) as default | Zero cost, cannot hallucinate — nothing is generated | Fluent synthesized answers needed, and the grounding risk is accepted (mitigated by citation verification, not eliminated) |

**This discipline has repeatedly paid for itself here.** The measured latency breakdown inverted the obvious scaling move — the intuitive "add an ANN index" would have optimised a stage worth 0.1% of query time while adding a 17.7-second rebuild after every ingest. Separately, three real bugs were caught only because the eval harness exists: a relevance floor that silently dropped correct answers, a threshold calibrated on 4 examples that failed as soon as the eval set grew to 31, and a reranker that couldn't see section titles. Heuristics in this repo are labelled inline (`app/config.py`) with how they were derived and how fragile they are.

---

## What's implemented

**Retrieval**
- **Hybrid search** (`app/retrieval.py`) — dense vector + FTS5/BM25, fused with Reciprocal Rank Fusion
- **Reranking** (`app/rerank.py`) — local cross-encoder over fused candidates, scoring the contextual header too (section titles are often the strongest relevance signal)
- **Vector backends** (`app/vector_index.py`) — exact brute-force (cached in memory) or FAISS HNSW, both ACL-correct
- **Calibrated refusal** (`app/query.py`) — says "not in corpus" instead of returning a confident near-miss; threshold derived from labeled data via `eval/calibrate_threshold.py`

**Security**
- **API-key auth** (`app/auth.py`) — groups resolved server-side from the key, hashed at rest. **Never** accepted from the request body
- **ACL enforcement** (`app/db.py`) — SQL `WHERE` pre-filter inside both vector and keyword queries, never a post-filter
- **Cache isolation** (`app/cache.py`) — cache keys include ACL groups, so a cache hit cannot leak a privileged answer
- **Rate limiting** (`app/rate_limit.py`) — per-principal sliding window (in-process; see DEPLOYMENT.md §4)

**Ingestion**
- **Parsing** (`app/parsing.py`) — `.md`/`.txt`/`.pdf` with extraction-quality warnings (scanned pages, garbled text, repeated headers) surfaced rather than swallowed
- **Structure-aware chunking** (`app/chunking.py`) — markdown headings, plus structural heading detection for PDFs (on a real PDF this took a document from 4 undifferentiated blobs to 17 correctly-titled sections)
- **Pipeline-versioned hashing** (`app/ingest.py`) — a change to parsing/chunking/embedding invalidates old chunks automatically instead of silently keeping stale ones

**Verification**
- `eval/run_eval.py` — 31 cases: answerable, ACL, and refusal, checked both at retrieval level and end-to-end
- `eval/test_generation.py` — citation verification incl. hallucinated citations and malformed responses
- `eval/test_cache_isolation.py` — proves the cache can't leak across ACL boundaries
- `eval/calibrate_threshold.py` — re-derives the relevance threshold from labeled data
- `eval/benchmark.py`, `eval/benchmark_pipeline.py` — scaling and latency measurement

---

## Setup

```bash
cd 05-hybrid-rag-search-engine
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env

# Required: the API rejects every request until a key exists
python scripts/manage_keys.py create --name admin --groups management,public --can-ingest

# Verify — all three suites must pass
python run_all_tests.py
```

First run downloads two small local models (~100–200 MB, one-time): the embedding model and the cross-encoder reranker. No API key is required — `LLM_PROVIDER=none` runs in **extractive mode** (returns the actual retrieved passages), which costs nothing and cannot hallucinate, because nothing is generated.

For synthesized answers and everything else you must configure by hand, see [`MANUAL_STEPS.md`](MANUAL_STEPS.md).

---

## Try it (CLI, no server needed)

```bash
# Ingest the sample corpus — three documents, one of them access-restricted
python -m app.cli ingest data/sample_docs/employee_handbook.md --groups public
python -m app.cli ingest data/sample_docs/product_faq.md --groups public
python -m app.cli ingest data/sample_docs/exec_compensation_policy.md --groups management

# Ask a question a public user can see
python -m app.cli query "How many vacation days do employees get?" --groups public

# Ask about the restricted document as a public user — should find nothing
python -m app.cli query "How are executive bonuses calculated?" --groups public

# Same question as a management user — should find it, with a citation
python -m app.cli query "How are executive bonuses calculated?" --groups management
```

Or ingest a whole directory at once (all files get the same ACL groups):

```bash
python -m app.cli ingest data/sample_docs --groups public
```

---

## Run the API

```bash
uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 1
```

Endpoints: `POST /query`, `POST /ingest/file`, `POST /ingest/directory`, `DELETE /ingest/file`, `GET /health`, `GET /ready`. Interactive docs at `/docs`.

All endpoints except `/health` and `/ready` require an `X-API-Key` header:

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "X-API-Key: rag_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{"question":"How many vacation days do employees get?"}'
```

**There is no `user_groups` field in the request.** Groups come from the API key, server-side. An earlier version accepted them in the body, which meant any caller could claim `["management"]` and read restricted documents — correct SQL filtering on an unverified identity is not access control. Verified blocked:

```bash
# Low-privilege key claiming elevated groups — the injected fields are ignored
curl -X POST http://127.0.0.1:8000/query -H "X-API-Key: $LOW_PRIV_KEY" \
  -d '{"question":"How are executive bonuses calculated?","user_groups":["management"]}'
# -> "I couldn't find anything relevant in the indexed documents."
```

`--workers 1` is deliberate — the cache, vector index, and rate limiter are per-process, and SQLite allows one writer. See [DEPLOYMENT.md §3](DEPLOYMENT.md).

---

## Testing

```bash
python run_all_tests.py     # all suites — run before every deploy
```

| Suite | Covers |
|---|---|
| `eval/run_eval.py` | 31 cases — answerable / ACL / refusal, at retrieval level **and** end-to-end |
| `eval/test_generation.py` | Citation verification: hallucinated citations dropped, unsupported claims flagged, malformed responses handled |
| `eval/test_cache_isolation.py` | Cache cannot serve a privileged answer to an unprivileged caller |

Two passes exist in `run_eval.py` for a reason: the retrieval-only pass calls `hybrid_search()`/`rerank()` directly and **misses bugs in the relevance floor**, which lives between retrieval and generation. That gap hid a real bug once.

### Bugs this suite caught during development

Kept here because they're the argument for having the suite at all:

1. **Relevance floor used an absolute cutoff (`rerank_score >= 0`)** — silently discarded correct top-ranked answers, because the cross-encoder's logit scale isn't centered at zero (a correct match regularly scores ~-5).
2. **The fix for #1 then over-returned** — when *every* candidate was bad, the relative-gap floor let them all through, so an unanswerable question returned three irrelevant chunks instead of "not found".
3. **A threshold calibrated on 4 examples failed as soon as the eval set grew to 31** — now derived from labeled data by `eval/calibrate_threshold.py`, with the separating margin (0.43 points) documented as fragile.
4. **The reranker scored `raw_text`, hiding section titles** — a question about the "hero section" couldn't match the words "Hero Section" because they only existed in the contextual header.
5. **Changing chunking logic didn't re-index anything** — the content hash covered only the document, so improved chunking silently kept the old chunks. Fixed by including `PIPELINE_VERSION` in the hash.
6. **PDFs collapsed into one giant section** — markdown-only heading detection degraded to nothing on the format most real corpora arrive in. A real 5-page PDF went from 4 undifferentiated blobs to 17 correctly-titled sections.

---

## Add your own documents

```bash
python -m app.cli ingest /path/to/docs --groups public
python -m app.cli ingest /path/to/confidential --groups hr
python -m app.cli stats
```

Supported: `.md`, `.txt`, `.pdf`. Re-ingesting an unchanged file is a no-op; editing and re-ingesting replaces its chunks cleanly.

**Read the parse warnings.** Ingestion flags suspected extraction problems (scanned pages, garbled text, repeated headers). They're advisory — a badly-parsed document still gets indexed and still gets retrieved, as confident-looking nonsense.

**Recalibrate after changing your corpus** (`python eval/calibrate_threshold.py`) and replace `eval/eval_set.json` with questions about *your* documents. The eval set is what makes every other number here meaningful.

---

## Known limitations

- **No OCR.** Scanned/image-only PDFs yield no text. The parser warns; it can't fix it.
- **No table extraction.** Tables flatten into unstructured text.
- **No `.docx`/`.pptx`/`.html`.** Extend `app/parsing.py`.
- **No query understanding** — no synonym expansion, multi-query, or follow-up rewriting. Retrieval works only as well as the user's phrasing matches the corpus vocabulary.
- **Single-worker only.** Cache, vector index, and rate limiter are per-process; SQLite allows one writer. DEPLOYMENT.md §4.
- **Live LLM providers untested.** The code paths are covered by a deterministic mock; the actual HTTP calls and per-provider response shapes are not. See MANUAL_STEPS.md §2.
- **`ABSOLUTE_RELEVANCE_FLOOR` is knife-edge.** Calibrated to a 0.43-point margin on a 4-document corpus. It will not transfer to your data unchanged.

## Production upgrade path

See [DEPLOYMENT.md](DEPLOYMENT.md) for measured numbers behind each trigger.

| This build | Production swap | When |
|---|---|---|
| SQLite (FTS5 + brute-force vectors) | Postgres + `pgvector` + `tsvector` | Past ~100k chunks, or **any** need for concurrent writers / multiple workers |
| Brute-force vector search | `VECTOR_INDEX_BACKEND=hnsw` (implemented, needs `faiss-cpu`) | Past ~200k chunks. Measured: 20 ms at 50k vs a 161 ms reranker — not the bottleneck below that |
| Local cross-encoder reranker | Cohere Rerank, GPU, or fewer candidates | **This is the actual bottleneck (90.4% of query time).** Attack it before anything else |
| Local embeddings (MiniLM) | OpenAI `text-embedding-3-large` / Voyage AI | Retrieval quality on specialized vocabulary needs to improve |
| `LLM_PROVIDER=none` | Groq/Gemini free tier → GPT-4o / Claude Sonnet | Fluent synthesized answers needed |
| In-process cache + rate limiter | Redis | Multi-worker deployment |
| `keys.json` | Secrets manager (Vault, AWS Secrets Manager) | Production, or key rotation requirements |

**On ColBERT-style late interaction as a third retrieval signal:** it's a legitimate technique (Khattab & Zaharia's ColBERT / ColBERTv2, served efficiently via PLAID) — it precomputes a vector *per document token* instead of one pooled vector per chunk, and scores a query by summing each query token's max similarity against those document token vectors (MaxSim). That avoids the per-candidate transformer forward pass a cross-encoder needs at query time, so it can match cross-encoder-level accuracy at meaningfully lower query latency **once the corpus is large enough for that latency to matter**.

It is not a drop-in swap, and treat any specific accuracy-lift percentage you see quoted for it as unverified until it's got a citation — real benchmark numbers vary by dataset and setup, and round marketing figures ("8-15%", "the 2026 standard") are a signal to ask for the source, not to repeat it as fact. The real, load-bearing costs: storage grows ~10-30x over single-vector embeddings (a vector per token, not per chunk), it needs a dedicated library (RAGatouille, or native support in Vespa/Weaviate/Elasticsearch) and a proper token-level ANN index (PLAID) to serve at speed — a hand-rolled brute-force MaxSim over SQLite blobs would be slower than the current cross-encoder, not faster, at this project's scale. Reach for it when the corpus and query volume are large enough that cross-encoder rerank latency is an actual measured problem, not preemptively.
