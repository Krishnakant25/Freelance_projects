# Production Gaps — Critical Analysis

An adversarial review: **what breaks when this points at a real warehouse with real users.**

- **§1 — Fixed.** Defects found while building, repaired, and pinned by tests.
- **§2 — Deferred.** Real production failures needing infrastructure or client data, documented rather than faked. Each has a trigger.

The demo is honest about what it is: **the semantic layer and its guardrails, over a sample warehouse.** §2 is what sits between that and a system a finance team would trust for a board number.

---

## 1. Fixed

### 1.1 A time filter was read as a time grouping

`"what was our revenue last month?"` returned **31 daily rows instead of one number.** The word "month" inside the date phrase "last month" matched the `order_date` dimension synonym, so the selector added a breakdown nobody asked for.

A time **filter** and a time **grouping** are different requests, and the phrase expressing one must not be readable as the other. Fixed by masking date-range and grain phrases before dimension matching. Pinned by the golden case `revenue-last-month-single-number`.

Worth noting *why this one matters disproportionately*: it produced a plausible-looking chart. Nobody would have questioned a daily revenue line — they'd just have answered a different question than the one asked.

### 1.2 A test's control assertion was inert

In the frozen-report test I patched `selector.select` to simulate interpretation drift, then asserted that an ad-hoc query *did* change (proving the drift was genuine and the pinned report's stability wasn't a false negative).

But `answer.py` does `from .selector import select`, binding its own reference — so the patch never reached it. The control passed no information. Fixed by patching both names, and the assertions are now labelled `CONTROL:` so their purpose is obvious to whoever reads a failure.

This is the same class of mistake as the voice project's eval hiding a broken middle: **a check that looks like proof and isn't.** Two projects in, it's clearly a pattern worth watching for rather than a one-off.

### 1.3 Scheduled reports served STALE CACHED data

`reports.run()` went through the interactive result cache, so running a report twice inside the TTL returned rows from the earlier run without re-querying.

A scheduled report exists to deliver **fresh** data. Serving it from cache means Monday's report can silently be Friday's numbers — a stale figure that still looks authoritative, which is the same class of failure as the query drift this module was built to prevent.

Worse, my own frozen-report test asserted *"repeated runs of a frozen report agree"* — which would have passed **because of the cache** rather than because of determinism, if the harness hadn't happened to disable caching. A test passing for the wrong reason.

**Fixed:** `run_selection(use_cache=False)` for reports. Interactive questions still cache (verified separately, so the fix didn't disable caching wholesale).

### 1.4 The result cache grew without bound

Cached payloads contain **full row sets**, and the cache had a TTL but no size limit. A busy day of distinct questions grew it indefinitely and never released the memory until restart — a slow leak that only surfaces once the process is large.

**Fixed:** `CACHE_MAX_ENTRIES` with LRU eviction, plus `cache_stats()` for observability. Verified with 25 distinct queries against a cap of 5: entries stayed at 5, evictions fired.

### 1.5 A filtered dimension also became a grouping

`"revenue by region from the phone channel"` grouped by **channel AND region**. The logic checked whether the question contained `"by"` *anywhere*, so the literal word "channel" plus a `by` produced an unwanted grouping — answering a different question than the one asked.

**Fixed:** only dimensions named *after* a breakdown keyword (`by`, `per`, `split by`…) and before the next clause boundary count as groupings. Verified that `"revenue by channel"` still groups (the fix didn't over-correct) and that an explicit `"by channel from the mobile app"` still groups on channel deliberately.

### 1.6 Zero-row answers printed a blank table

An empty result showed bare column headers with nothing underneath, which reads like a bug rather than an answer. The query was valid; the period simply had no matching rows.

**Fixed:** an explicit *"No data matched that question — the query ran successfully"* message, while still showing the derivation, because "why is this empty?" is almost always a question about the filters.

### 1.7 The cost check was itself expensive

`estimate_scan()` ran four `COUNT(*)` scans on **every query** to estimate cost — so the guardrail meant to prevent expensive queries was one of the more expensive things the system did.

**Fixed:** memoized per process, invalidated on re-seed (verified — memoizing without invalidation would leave the estimator using stale table sizes forever).

### 1.8 Also handled during the build

- **Synonym collisions are rejected at model load.** If "revenue" mapped to two metrics, selection would be order-dependent and therefore unpredictable. `_validate()` refuses to load such a model.
- **Longest-synonym-first matching**, so `"average order value"` doesn't resolve to `order_count` because it contains "order". Pinned by `aov-longest-synonym-wins`.
- **Filters vs. groupings disambiguated.** `"revenue from the mobile app"` is one number; `"revenue by region from the mobile app"` is a filtered breakdown.
- **Defaults are disclosed as assumptions**, never applied silently.

---

## 2. Deferred

### 2.1 SQLite is not a warehouse — the guardrail *mechanisms* don't port

The guardrail **shapes** are right and transfer directly. The **implementations** do not:

| This build | Real warehouse |
|---|---|
| `mode=ro` URI + SQLite authorizer | A dedicated **read-only role** with `SELECT`-only grants on specific schemas |
| `EXPLAIN QUERY PLAN` row-count heuristic | BigQuery dry-run / Snowflake `EXPLAIN` with a **bytes-scanned ceiling** |
| `timeout=` on the connection | Server-side `statement_timeout` |
| Injected `LIMIT` | Same, plus result-set byte caps |

**Trigger:** the first real client warehouse. Budget this as real work — the abstraction is clean but every mechanism is replaced.

### 2.2 No row-level security — **the sharpest gap**

Every question currently sees all data. Point this at a real warehouse containing salaries, per-rep performance, or customer PII and **any user can query anything.**

This cannot be solved with a prompt instruction, and it can't be solved fully in the semantic layer either: the correct fix is **row-level security in the warehouse**, keyed to the requesting user's identity, so the database itself refuses rows the caller isn't entitled to. The semantic layer can add a second filter, but it must not be the only one.

The RAG project in this portfolio solved the equivalent problem (ACL-filtered retrieval enforced as a SQL pre-filter, verified by tests) — the same discipline applies here and is not yet implemented.

**Trigger:** before any user other than the operator can query, and unconditionally before the warehouse contains anything sensitive.

### 2.3 The LLM selector is untested against a live provider

The `groq`/`gemini` paths are implemented and **constrained identically** to the rule-based one — the model returns metric and dimension *names*, and `validate_selection()` rejects anything it invented, so a hallucinated metric becomes a refusal rather than a guess. That gate is tested via the `mock` provider, including a hallucinated-metric case and an injection attempt.

What's untested is the actual HTTP call and each provider's real response shape.

**Trigger:** enabling an LLM selector. Verify with the golden set — accuracy should be *measurable*, not assumed.

### 2.4 No accuracy measurement against a live LLM selector

The golden set measures the **rule-based** selector. The interesting number — how often an LLM picks the right metric — needs a live provider and a larger question set drawn from real usage.

**What to do:** log every question, the selection produced, and whether it was refused. The refusals are the most valuable data: they tell you exactly which metrics to define next.

### 2.5 No authentication on the API

`/ask` will run queries for anyone who can reach the port; `/reports` lets anyone pin or repin a scheduled report. Given §2.2, this compounds — no auth *and* no RLS means no access control at all.

Copy the pattern from the helpdesk project (`app/auth.py`: API key → roles, hashed at rest).

**Trigger:** any non-local deployment.

### 2.6 Semantic model has no CI validation or schema-drift detection

`_validate()` runs at load, which catches a malformed model. It does **not** catch a metric referencing a column that no longer exists — that surfaces as a runtime SQL error, or worse, a silently wrong number if a column was repurposed rather than dropped.

**What's needed:** a CI job that loads the model, runs every metric against the live schema with `LIMIT 0`, and fails on any reference that no longer resolves. In a real deployment the model should be generated from (or validated against) dbt docs so it can't drift from the warehouse.

### 2.7 No scheduled delivery

Reports can be pinned and run, but nothing runs them on a schedule or delivers them. Needs a scheduler plus email/Slack delivery. The frozen-selection design is the hard part and it's done; delivery is plumbing.

### 2.8 Clarification is designed but not wired

`Clarification` exists as a first-class result type and the glossary resolves the classic ambiguities (unqualified "revenue" → net; "last month" → calendar month). But no path currently *returns* a Clarification — genuinely ambiguous questions resolve via the glossary default and disclose it as an assumption instead of asking.

That's a defensible interim behaviour (the assumption is always shown) but asking is better for high-stakes figures. **Trigger:** a client with genuinely contested metric definitions.

### 2.9 Single-process, in-memory cache

Same deferred bottleneck as the other projects: the result cache is per-process. Multi-worker needs Redis. Also note the cache key covers SQL + params but **not user identity** — which becomes a leak the moment §2.2 is implemented and different users see different rows. The RAG project hit exactly this and keyed its cache on ACL groups; do the same here.

**Trigger:** implementing RLS, or multi-worker deployment. Whichever comes first.

### 2.10 Chart rendering is a spec, not a picture

`app/charts.py` returns a chart *specification* (type, axes, formats). Nothing renders it. Deliberate — rendering belongs to whatever front-end consumes the API, and the useful decision (which chart the data shape warrants) is what's implemented.

---

## 3. Honest summary

**What this proves:** questions map to defined metrics or get refused; SQL is deterministic and inspectable; the warehouse can't be written to; untrusted values can't reach SQL text; breakdowns reconcile with their totals; and scheduled reports can't silently drift.

**What it isn't:** a BI product. It's the semantic layer and correctness machinery that makes one trustworthy — which is the part clients don't get from Metabase, and the part worth being paid for.

| | Count |
|---|---|
| Defects found and fixed | 7 (5 from the adversarial audit) + 4 handled inline |
| Production gaps documented | 10 |
| Of those, security-critical | 2 (§2.2 row-level security, §2.5 auth) |
| Of those, needing client data/schema | 3 (§2.4, §2.6, §2.8) |

**If a client signed tomorrow, the order would be:** row-level security → auth → their real semantic model → warehouse migration → measurement against a live selector.
