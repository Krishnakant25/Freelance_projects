# "Prompt-to-BI" Reporting & Dashboard Automation

Category: **The Growth Engine — Autonomous Marketing Systems** *(cross-applies to any client with a data warehouse)*

A system where a user (or a schedule) asks a question in plain English and gets back a correct chart/dashboard/report — natural language → SQL → visualization → scheduled delivery — grounded in the real schema so it doesn't hallucinate columns or metrics.

---

## 2. Architecture

```
User question ("what were signups by channel last month?")
   or scheduled trigger
        │
        ▼
   Schema-Aware NL→SQL Agent
   - retrieves relevant table/column context (RAG over schema + docs)
   - generates SQL
        │
        ▼
   SQL Validator
   - syntax check, dry-run/EXPLAIN
   - guardrails: read-only, row/cost limits
        │
        ▼
   Query Execution (warehouse)
        │
        ▼
   Result → Chart Selector
   (picks chart type based on data shape:
    time series → line, categorical → bar, etc.)
        │
        ▼
   Dashboard/Report Renderer
        │
   ┌────┴────┐
   ▼          ▼
 Live         Scheduled delivery
 dashboard    (email/Slack PDF or
 (web)        image snapshot)
```

The schema-grounding step (RAG over table/column definitions, business glossary, and past validated queries) is what keeps the NL→SQL layer honest — without it, the LLM guesses column names and silently returns wrong numbers, which is worse than no automation at all.

> ⚠️ **Schema grounding alone is not enough — see §6.1.** Free-form NL→SQL tops out in the ~70s% accuracy range even in the best published systems, and silent wrong answers are this project's defining risk. The production design replaces open-ended SQL generation with a **semantic layer**: the LLM selects from a defined set of metrics, dimensions, and filters, and the SQL is generated deterministically from that selection. Read §6.1 before implementing §3–§5.

---

## 3. Core Components

| Component | Role |
|---|---|
| Schema knowledge base | Table/column descriptions, relationships, business term glossary |
| NL→SQL agent | Translates the question into SQL grounded in the schema context |
| SQL validator/guardrails | Enforces read-only access, checks syntax, limits cost/row scans before execution |
| Query executor | Runs the validated query against the warehouse |
| Chart selector | Chooses appropriate visualization for the result shape |
| Dashboard renderer | Web dashboard and/or static report generation |
| Scheduler/delivery | Sends recurring reports to email/Slack automatically |
| Query memory | Stores validated question→SQL pairs to improve future accuracy and catch drift |

---

## 4. Tech Stack

### Phase 1 — Free / self-hosted

| Layer | Tool | Notes |
|---|---|---|
| Warehouse | DuckDB or SQLite (local), or Postgres free tier (Supabase) | Free, plenty for small-to-mid datasets |
| Schema knowledge base | Manually written table/column docs + local embeddings (Chroma) | Free, worth the manual effort — this is the accuracy-critical piece |
| NL→SQL LLM | Gemini Flash / Groq free tier, or a local model fine-tuned prompt with few-shot schema examples | Cheap; NL→SQL is a well-suited task for smaller models with good context |
| Validator | Custom Python: `EXPLAIN` check, `SELECT`-only allow-list, row-limit clause injection | Free, essential regardless of budget |
| Dashboard | Metabase (self-hosted, free) or Streamlit | Metabase gives a real BI UI for free |
| Scheduling/delivery | Metabase's built-in scheduled email/Slack, or n8n + cron | Free |

### Phase 2 — Paid / production

| Layer | Tool | Why |
|---|---|---|
| Warehouse | Snowflake, BigQuery, or Postgres (RDS) | Scale, concurrency, proper access control |
| Schema knowledge base | Maintained via a dbt-generated docs/glossary, embedded automatically on schema change | Keeps grounding accurate as schema evolves |
| NL→SQL LLM | GPT-4o or Claude Sonnet | Materially better at complex joins/window functions |
| Validator | Query cost estimation via warehouse's own EXPLAIN/dry-run + a hard cost ceiling | Prevents runaway warehouse bills from a bad generated query |
| Dashboard | Hex, Metabase Cloud, or Retool | Managed hosting, better collaboration/sharing features |
| Scheduling/delivery | Native platform scheduling + Slack app integration | Reliable delivery, richer formatting |
| Observability | Log every generated query + result, review for drift/errors weekly | Catches silent accuracy regressions early |

---

## 5. Build Sequence

1. **Document the schema and business glossary first** — this is the single highest-leverage step; NL→SQL accuracy lives or dies here, not in prompt engineering.
2. **Build the validator before the generator** — read-only enforcement, row/cost limits, and `EXPLAIN`-based sanity checks, tested against known-bad queries.
3. **Build the NL→SQL agent** with few-shot examples of real validated question→SQL pairs from the business (not generic examples).
4. **Test against a fixed set of real business questions** with known-correct answers, before trusting it on novel questions.
5. **Add the chart selector and dashboard renderer**, keep chart logic simple/rule-based rather than another LLM call.
6. **Wire scheduled delivery** for the highest-value recurring reports first (weekly ops summary, daily metrics digest).
7. **Add a query memory store** — every validated question→SQL pair gets saved and reused as few-shot context, improving accuracy over time.
8. **Add a human-review step for new/unusual questions** before they become part of an automated scheduled report.
9. **Move to Phase 2 infra** once warehouse size or concurrent-user count outgrows the free stack.
10. **Set up a weekly accuracy review** — sample recent generated queries, verify against manual analysis, catch schema-drift or metric-definition errors early.

---

## 6. Reality Check — Why the Naive Build Fails, and the Fix

### 6.1 Free-form NL→SQL is not accurate enough to trust, and never silently fails
**Failure:** The defining problem of this project, and §1–§5 treat it as a tuning exercise. Even the best published systems land in the **~70s% execution accuracy** on realistic benchmarks, and real enterprise schemas — inconsistent naming, soft deletes, undocumented join keys, three tables that all look like "orders" — are harder than benchmarks. A BI tool that is quietly wrong 20–30% of the time is **worse than no tool**: it produces a confident number with a chart, someone puts it in a board deck, and there is no error message anywhere. Compare that to a broken dashboard, which announces itself.

**Fix — constrain the generation surface. This is the single most important change in this document.** Do not let the LLM author arbitrary SQL against raw tables. Put a **semantic layer** in between:

```
Question → LLM selects from a DEFINED set:
             metric(s) + dimension(s) + filters + time grain
                          │
                          ▼
              Semantic layer (dbt metrics / Cube /
              a hand-built metric registry)
                          │
                          ▼
              Deterministic SQL generation ── executes
```

The LLM's job becomes **classification over a known vocabulary**, not open-ended code generation. Accuracy jumps dramatically because the space of possible outputs is small and validatable; every metric means exactly one thing because it's defined once, in code, and reviewed by a human; and the system can **refuse** cleanly — "there's no defined metric for churn; here's what I do have" — which free-form generation can never do, because it will always produce *some* SQL.

Keep an escape hatch for analysts who genuinely want free-form SQL generation, clearly labeled as unverified, with the SQL always shown. But the scheduled reports and the exec-facing dashboards run on the semantic layer only.

### 6.2 "Read-only" enforced in a prompt is not read-only
**Failure:** §4 Phase 1's validator is a Python allow-list checking that the query starts with `SELECT`. That's bypassable in more ways than it's worth enumerating (CTEs, stacked statements depending on driver, functions with side effects), and it's the wrong layer entirely.

**Fix:** Enforce at the **database**: a dedicated role with `SELECT`-only grants, on the specific schemas allowed, with a statement timeout and a query cost ceiling set server-side. Then the application-level validator is defense in depth rather than the only defense. Same for sensitive data — **row-level security** in the warehouse, tied to the requesting user's identity, so an LLM cannot generate a query that returns rows that user isn't entitled to. Never pass a service account with broad access and rely on the prompt to be careful.

### 6.3 Ambiguity gets silently resolved the wrong way
**Failure:** "Revenue last month" contains at least two landmines. *Revenue* — gross, net, bookings, recognized, excluding refunds? *Last month* — the previous calendar month, or trailing 30 days? The LLM will pick one, not mention it, and produce a number that's defensible under one definition and wrong under the other. Finance will notice; trust will not recover.

**Fix:** The semantic layer resolves this by construction — each metric has exactly one definition. Beyond that: build a **business glossary** as a first-class artifact (it's also the highest-value deliverable you'll produce for the client, independent of the AI); make the system **ask a clarifying question** when a term maps to multiple metrics rather than guessing; and **always display the assumptions with the answer** ("Net revenue, excluding refunds, calendar month of July").

### 6.4 Never show a number without its derivation
**Failure:** §2 ends at a chart. Once a number is rendered as a chart with no visible provenance, nobody can check it, and the errors from §6.1 and §6.3 become invisible and permanent.

**Fix:** Make every result **auditable by default**: show the generated SQL (collapsed but one click away), the metric definitions used, the filters applied, the row count, and the data freshness timestamp. Add a "verify this" affordance that lets an analyst run it themselves. This is also what makes the tool defensible in a client meeting — you're not asking anyone to trust a model, you're showing your work.

### 6.5 Scheduled reports drift silently
**Failure:** §5 step 6 schedules recurring reports, but if each run **regenerates the SQL from the question**, the query can change between runs — a different join, a different date boundary — and the week-over-week trend moves for reasons unrelated to the business. That's the worst possible failure in a recurring executive report.

**Fix:** **Freeze scheduled reports.** Once a question is approved for recurring delivery, pin the generated query (or the semantic-layer selection) as a **versioned artifact**. Re-run the pinned query, don't regenerate it. Any change to it is a reviewed, logged change with a visible version bump on the report. Regeneration is for ad-hoc exploration only.

### 6.6 Cost and concurrency will surprise someone
**Failure:** §4 mentions cost limits for Phase 2, but on a consumption-priced warehouse a single bad generated query — an accidental cross join over a large fact table — can cost hundreds of dollars in one execution. Multiply by users who retry when an answer looks wrong.

**Fix:** Layer the controls: **dry-run/EXPLAIN with a bytes-scanned ceiling before execution** (BigQuery and Snowflake both support this), per-user daily quotas, mandatory `LIMIT` injection on exploratory queries, and **result caching** so the same question within a window doesn't re-execute. Also: point the tool at pre-aggregated tables rather than raw event data wherever possible — cheaper, faster, and much easier for the LLM to get right.

### 6.7 Be honest about what you're actually building
**Failure:** §4 proposes Metabase, which already ships natural-language querying, dashboards, and scheduled delivery. Rebuilding that from scratch is expensive and the result will be worse.

**Fix:** Position the work where the value actually is: **the semantic layer, the business glossary, the data modeling, and the guardrails**. Those are what make *any* BI-on-top-of-LLM work, they're specific to the client, and they're the part no product ships out of the box. Build on Metabase/Cube/Hex rather than beside them. The honest pitch is "we make your data queryable in plain English, correctly" — and the correctness is the deliverable, not the chat box.

### 6.8 Accuracy needs a test suite, not a weekly eyeball
**Failure:** §5 step 10's "sample recent queries weekly" catches problems late, inconsistently, and depends on someone caring enough to do it every week. They won't.

**Fix:** Build a **golden query set**: 50–100 real business questions with human-verified correct answers, run automatically on every prompt, model, schema, or semantic-layer change, failing the deploy on regression. Alert on schema drift (a column the semantic layer references disappears) before users find it. Log every question — including the ones the system refused — because the refusals tell you exactly which metrics to define next.
