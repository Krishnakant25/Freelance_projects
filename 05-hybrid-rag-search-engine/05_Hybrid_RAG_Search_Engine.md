# Domain-Specific Hybrid Search Engine

Category: **The Data Engine — High-Precision RAG Pipelines**

A retrieval-augmented generation system built for one specific domain (legal, medical, technical docs, internal knowledge base), combining keyword search and vector search with reranking, so answers are both semantically relevant *and* precise on exact terms — and every answer is grounded with citations back to source documents.

---

## 2. Architecture

```
Source documents (PDFs, docs, wikis, DB records)
        │
        ▼
   Ingestion Pipeline
   - parsing/OCR
   - chunking (semantic, not fixed-size)
   - metadata extraction (source, date, section)
        │
   ┌────┴────────────┐
   ▼                  ▼
Embedding model    Keyword index
(dense vectors)    (BM25 / inverted index)
   │                  │
   ▼                  ▼
Vector DB          Keyword search engine
        │                  │
        └────────┬─────────┘
                  ▼
           Hybrid Retriever
           (fuses vector + keyword results,
            e.g. reciprocal rank fusion)
                  │
                  ▼
              Reranker
      (cross-encoder scores top-N by
       true relevance to the query)
                  │
                  ▼
           LLM Generation
      (answer grounded in retrieved
       chunks, with inline citations)
                  │
                  ▼
        Answer + source citations
```

Why hybrid, not just vector search: pure vector search misses exact-match needs (product codes, legal citations, error codes, drug names) where keyword/BM25 wins; pure keyword search misses paraphrased/semantic queries. Fusing both, then reranking, consistently beats either alone on domain-specific corpora.

---

## 3. Core Components

| Component | Role |
|---|---|
| Ingestion pipeline | Parses raw docs into clean, well-chunked text with metadata |
| Chunking strategy | Domain-aware splitting (by clause/section, not blind token windows) |
| Embedding model | Converts chunks + queries into dense vectors |
| Vector DB | Stores/searches dense embeddings by similarity |
| Keyword index | Exact/lexical search (BM25) for precise term matching |
| Hybrid retriever | Merges both result sets into one ranked candidate list |
| Reranker | Cross-encoder that re-scores top candidates for true relevance |
| Generator (LLM) | Produces the final answer, grounded and cited, refuses when evidence is insufficient |
| Evaluation harness | Measures retrieval precision/recall and answer faithfulness over a test set |

---

## 4. Tech Stack

### Phase 1 — Free / self-hosted

| Layer | Tool | Notes |
|---|---|---|
| Parsing | `unstructured`, PyMuPDF, or Docling (all open-source) | Handles PDFs, tables, scanned docs (with OCR add-on) |
| Embeddings | `sentence-transformers` (e.g. `bge-small`/`bge-base`) run locally | Free, good quality for domain text |
| Vector DB | Qdrant or Chroma, self-hosted (Docker) | Free, handles hundreds of thousands of chunks comfortably |
| Keyword index | Meilisearch or OpenSearch, self-hosted | Free, fast BM25-style search |
| Reranker | `bge-reranker-base` (open-source cross-encoder) | Free, runs on CPU for small batches |
| LLM generation | Ollama (Llama 3.1/Qwen) or Gemini Flash free tier | Free/near-free for prototyping |
| Evaluation | RAGAS (open-source) | Free, standard RAG eval metrics |

### Phase 2 — Paid / production-quality

| Layer | Tool | Why |
|---|---|---|
| Embeddings | OpenAI `text-embedding-3-large` or Voyage AI (domain-tuned models available) | Better retrieval quality, especially on specialized vocab |
| Vector DB | Pinecone, Qdrant Cloud, or Weaviate Cloud | Managed scaling, backups, multi-tenant isolation |
| Keyword index | Elasticsearch (managed) or Algolia | Enterprise-grade search infra |
| Reranker | Cohere Rerank API, or a ColBERT-style late-interaction retriever (RAGatouille / Vespa / Weaviate's native support) once query volume is high enough that cross-encoder latency is a measured problem | Best-in-class reranking quality, simple API (Cohere); lower query-time latency at large scale (ColBERT) at the cost of ~10-30x storage (a vector per token, not per chunk) and needing a proper token-level ANN index (PLAID) to serve fast — not a small-corpus optimization |
| LLM generation | GPT-4o or Claude Sonnet | Stronger grounding/citation accuracy, fewer hallucinations |
| Evaluation/monitoring | Langfuse or Arize Phoenix | Production tracing of retrieval + generation quality over time |

---

## 5. Build Sequence

1. **Collect and audit the corpus** — what documents exist, how messy are they (scanned PDFs? inconsistent formatting?), what's the update frequency.
2. **Build the ingestion pipeline** — parsing, cleaning, chunking with metadata (source, section, date) preserved on every chunk.
3. **Stand up the vector index** and get basic semantic search working end-to-end first, before adding hybrid complexity.
4. **Add the keyword index** in parallel, confirm it retrieves the exact-match cases vector search misses.
5. **Build the fusion + reranking layer** — combine both result sets, rerank, tune the number of candidates passed to the LLM.
6. **Build a labeled evaluation set** — 30–50 real domain questions with known correct source chunks, before tuning further. Nothing after this step should be judged by eyeballing.
7. **Wire the generation step** — strict prompting to only answer from retrieved context, cite sources, and say "not found" when evidence is weak.
8. **Run the eval harness**, tune chunk size, retrieval k, and reranker cutoff against it.
9. **Add a feedback loop** — thumbs up/down on answers, feed low-rated cases back into the eval set.
10. **Move to Phase 2 infra** once corpus size, query volume, or accuracy requirements outgrow the self-hosted stack.

---

## 6. Reality Check — Why the Naive Build Fails, and the Fix

### 6.1 Retrieval permissions are completely missing — this is a data breach
**Failure:** The biggest hole in the original design. §2 retrieves from a single flat index with no notion of *who is asking*. Point this at a company's internal documents and any employee can ask "what are the planned layoffs?" or "what's the CEO's compensation?" and the retriever will happily serve chunks from HR files they can't open in SharePoint. Filtering in the prompt ("only answer if the user has access") is not access control — it's a suggestion to a language model.

**Fix:** Permissions must be enforced **inside the retrieval query**, not after it:
- Carry the source system's ACLs into **chunk metadata at ingestion time**, and re-sync them (permissions change; your index won't notice unless you make it).
- Every query filters by the caller's identity/groups **as a pre-filter in the vector and keyword search**, so unauthorized chunks are never candidates. Post-filtering leaks through result counts and reranker behavior.
- Test it explicitly: a low-privilege user asking a question whose answer only exists in a restricted document must get "not found," not a summary.

This applies to the keyword index too — an easy thing to secure on one side and forget on the other.

### 6.2 The project fails at parsing, not retrieval
**Failure:** §5 gives ingestion one line and spends the rest on fusion and reranking. That's inverted. In practice, most domain RAG projects fail because a table got flattened into unreadable text, a two-column PDF interleaved its columns, a scanned contract OCR'd badly, or a section heading was lost so a chunk has no idea what it's about. No reranker recovers information destroyed at parse time.

**Fix:** Budget **most** of the timeline here. Manually inspect parser output on the worst 20 documents in the corpus before building anything downstream. Handle tables as a distinct type (extract to markdown/HTML and keep them intact — never split a table across chunks). Prepend **section/document context to every chunk** ("From *Employment Agreement 2024*, §4 Termination: …") so the chunk is interpretable standalone. Detect and route scanned documents to OCR. If the corpus is genuinely messy, a commercial parser (LlamaParse, Reducto, Azure Document Intelligence) pays for itself immediately.

### 6.3 Two search engines where one would do
**Failure:** §4 Phase 1 runs Qdrant *and* Meilisearch — two services to deploy, monitor, keep in sync, and reconcile when a document is deleted from one and not the other. That's real operational cost for no accuracy gain at this scale.

**Fix:** Use **one system that does both**. Postgres with `pgvector` + `tsvector`/`ParadeDB` gives dense and lexical search, transactional consistency, permission filtering, and your application data in a single database you already know how to back up. Qdrant and Weaviate also support hybrid natively. Reach for a dedicated search cluster only when scale actually forces it — and it usually doesn't, at freelance-project sizes.

### 6.4 Semantic chunking and other over-engineering
**Failure:** §3 specifies "semantic chunking, not fixed-size" as though it's settled. It frequently underperforms well-tuned fixed-size chunking with overlap, while costing an embedding pass over the whole corpus and adding a tuning surface with no clear objective.

**Fix:** Start with **structure-aware fixed-size chunking** — split on the document's own boundaries (headings, sections, clauses) with overlap, ~300–800 tokens depending on domain. Then spend the effort you saved on **contextual retrieval** instead: prepend a short LLM-generated summary of the parent document to each chunk before embedding. That reliably beats chunking-strategy micro-optimization, and it composes with everything else.

### 6.5 The query and the documents don't speak the same language
**Failure:** Not addressed at all in §2. Users ask "can I get fired for this?"; the contract says "termination for cause." Users type acronyms the docs spell out, and product nicknames that appear nowhere. Both dense and sparse retrieval miss, and the fused result is confidently empty.

**Fix:** Add a **query understanding stage** before retrieval:
- **Acronym/synonym expansion** from a domain glossary — cheap, deterministic, and the single highest-ROI addition for jargon-heavy corpora.
- **Multi-query**: generate 2–3 rephrasings and union the results.
- Optionally **HyDE** (embed a hypothetical answer rather than the question) — helpful when questions and documents are stylistically very different.
- For conversational use, **rewrite follow-ups into standalone queries** ("what about the second one?" retrieves nothing on its own).

### 6.6 The corpus changes and the index doesn't
**Failure:** §2 is a one-way pipeline. Nothing handles a document being **updated, superseded, or deleted**. In a legal or policy domain, answering from a superseded version isn't a degraded answer — it's a wrong one with confident citations.

**Fix:** Design ingestion as **incremental and idempotent** from the start: content-hash each source document, re-embed only what changed, and **hard-delete chunks whose source was deleted** (an orphaned chunk in a vector store is invisible until it shows up in an answer). Store `version` and `effective_date` in metadata, prefer current versions at retrieval, and let the generator say "this is from the 2022 policy, superseded in 2024."

### 6.7 A 50-question eval set can't detect regressions
**Failure:** §5 step 6 proposes 30–50 questions. That's enough to sanity-check, not enough to tell a real 3% improvement from noise — and RAGAS-style LLM-judged answer metrics are themselves noisy and expensive to run repeatedly.

**Fix:** Split the evaluation:
- **Retrieval metrics first** — label the correct chunk(s) for each question and track `recall@k` and MRR. These are deterministic, instant, free to run on every change, and retrieval is where most quality lives.
- **Answer metrics sparingly** — a smaller LLM-judged faithfulness set, run before releases, not on every commit.
- Grow the set continuously from **real production questions**, especially failures. 200+ questions harvested from actual use beats 50 invented ones.

### 6.8 Citations are generated, not verified
**Failure:** §5 step 7 asks the LLM to cite sources. It will — including citing chunk 3 for a claim that came from chunk 7, or synthesizing a plausible-looking citation. Since citations are the entire trust mechanism of this product, an unverified one is worse than none.

**Fix:** **Verify citations programmatically after generation.** Check that each cited chunk ID was actually in the retrieved set, and that the claim's key terms overlap the cited chunk (or run a cheap NLI/entailment check). Drop or flag citations that fail. In the UI, link every claim to the **source document at the exact page/section** so a user can confirm in one click — and make "I couldn't find this in the documents" a first-class, well-styled response rather than a failure state the model avoids.
