# Prompt-to-BI — Semantic-Layer Analytics

Ask a business question in plain English, get a correct answer with the SQL that produced it. Built around one decision that separates this from most "chat with your data" demos: **the LLM never writes SQL.**

Full rationale in [`08_Prompt_to_BI_Dashboards.md`](08_Prompt_to_BI_Dashboards.md). Self-contained; shares no code with the other portfolio projects.

---

## The premise change

Free-form NL→SQL is the obvious way to build this, and it's the wrong one. Even the best published systems land in the **~70s% execution accuracy** range on realistic schemas, and — the part that matters — **the failures are silent.** A wrong join produces a confident number with a chart, no error anywhere, and it goes into a board deck. That's worse than a broken dashboard, which at least announces itself.

So instead:

```
Question  →  LLM selects from a DEFINED vocabulary  →  deterministic SQL  →  result
             (metrics, dimensions, date ranges)          (built by code)
```

The model's job becomes **constrained selection over a known vocabulary**, not open-ended code generation. Three things follow:

1. **It can't invent a join.** Only joins declared in [`model/semantic_model.yaml`](model/semantic_model.yaml) are ever emitted, with prerequisites resolved.
2. **It can refuse.** Ask for churn and you get *"I don't have a metric for that — here's what I do have."* A model asked to write SQL will always write *some* SQL.
3. **Every metric means one thing.** When finance and marketing disagree about "revenue", that argument happens once, in a reviewable diff, not silently inside a generated query.

**The semantic model is the actual deliverable.** It's the part specific to a client, the part that makes any BI-on-LLM work, and the part no product ships out of the box.

---

## Status

| | |
|---|---|
| Test suite | **93 assertions + 24 golden queries + 4 consistency checks, all passing** |
| Read-only enforcement | Verified at the driver level — INSERT/UPDATE/DELETE/DROP/ATTACH all refused |
| SQL injection | Filter values are bound parameters; an injection payload is provably inert |
| Refusal | 5 golden cases assert a clean refusal that lists available metrics |
| Internal consistency | Every breakdown sums to its total — proves no join fans out and double-counts |
| Frozen reports | A pinned report's SQL is byte-identical across runs, even when interpretation drifts |

---

## Setup

```bash
cd 08-prompt-to-bi
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

python -m app.cli seed          # deterministic sample warehouse (6k orders)
python run_all_tests.py         # all 4 suites must pass
```

No API keys, no network, no ML dependencies — the default selector is rule-based over the semantic model's synonyms. Install time is seconds, not minutes.

---

## Demo script — the five moments worth showing

```bash
python -m app.cli seed
```

**1. A simple question, and the SQL behind it.** The point is the `--sql` flag: every number is checkable.
```bash
python -m app.cli ask "what was our revenue last month?" --sql
```

**2. The same figure two ways, and they agree.** Ask for the total, then the breakdown — the parts sum to the whole. That's the strongest correctness signal available without a second implementation.
```bash
python -m app.cli ask "net revenue last month"
python -m app.cli ask "net revenue by channel last month"
```

**3. THE KEY MOMENT — it refuses instead of guessing.**
```bash
python -m app.cli ask "what is our churn rate?"
python -m app.cli ask "what was our profit margin last month?"
```
No churn metric is defined and there's no cost data, so margin is uncomputable. It says so and lists what *is* available. **A free-form NL→SQL system would have produced a number here** — most likely by substituting revenue, silently.

**4. Filters vs. groupings are understood as different requests.**
```bash
python -m app.cli ask "how much revenue came from the mobile app last month?"   # one number
python -m app.cli ask "revenue by region from the mobile app last month"        # breakdown + filter
```

**5. Frozen reports don't drift.**
```bash
python -m app.cli report pin weekly-revenue "net revenue by week this year"
python -m app.cli report run weekly-revenue --sql
python -m app.cli report history weekly-revenue
```
The *selection* is pinned. Re-runs execute it; they never re-interpret the question — so a week-over-week trend can't move because the query quietly changed.

**Bonus, the strongest technical moment:**
```bash
python -m app.cli doctor        # actively attacks its own read-only boundary
python -m app.cli model         # the entire vocabulary — the boundary is visible
```

---

## Safety properties

### Read-only is enforced by the connection, not a prompt

Architecture doc §6.2: *"read-only enforced in a prompt is not read-only."* Two independent mechanisms, neither of which inspects the SQL string:

- SQLite `mode=ro` URI — writes fail at the driver
- A SQLite **authorizer callback** that denies anything that isn't a read, including `ATTACH` (which would otherwise let a query reach a writable file and escape the boundary entirely)

`python -m app.cli doctor` attempts five write statements and reports each refusal.

### Untrusted values never reach SQL text

Filter values come from the question, so they're the one part influenced by untrusted input. They're **bound parameters**, never concatenated. A test feeds `web'; DROP TABLE orders; --` through the LLM selector and asserts the payload is absent from the SQL text, present in the params, and that `orders` still exists afterwards.

### Cost ceiling before execution

`EXPLAIN QUERY PLAN` runs first; a query estimated to scan beyond the ceiling is **refused before running**, with a message saying how to narrow it. This is the stand-in for the bytes-scanned limit you'd set on BigQuery/Snowflake, where one accidental cross join costs real money.

### Nothing is shown without its derivation

Every answer carries the SQL, the metric definitions used, the filters applied, the resolved date range, the row count, and the data freshness. Defaults are disclosed as **assumptions** — *"date range defaulted to the last 30 days"* — because a silent default is how a number gets misread.

---

## Bugs found

Seven real defects. **Five came from an adversarial audit run against the working system, after the test suite was already green** — which is the argument for auditing separately from writing tests.

**Scheduled reports served stale cached data.** `reports.run()` went through the interactive result cache, so Monday's report could silently be Friday's numbers. Worse: my own frozen-report test asserted "repeated runs agree", which would have passed *because of the cache* rather than because of determinism. A test passing for the wrong reason.

**The result cache grew without bound.** Payloads hold full row sets; there was a TTL but no size cap. A slow memory leak that only surfaces once the process is large.

**A filtered dimension also became a grouping.** `"revenue by region from the phone channel"` grouped by channel *and* region, because the logic checked for `"by"` anywhere in the question. Now only dimensions named after a breakdown keyword count.

**Zero-row answers printed a blank table** — bare headers with nothing under them, which reads as a bug rather than an answer.

**The cost check was itself expensive** — four `COUNT(*)` scans per query to estimate cost.

Plus two found while building:

**"What was our revenue last month?" returned 31 daily rows instead of one number.** "Month" inside the date phrase matched the `order_date` dimension synonym, so a time *filter* was read as a *grouping*. It produced a plausible daily revenue chart — nobody would have questioned it.

**A test's control assertion was inert.** I patched `selector.select` to simulate drift, but `answer.py` does `from .selector import select` and holds its own reference. The check meant to prove the drift was real proved nothing — same class as the voice project's eval hiding a broken middle.

---

## Honest positioning

**Metabase, Cube, and Hex already ship natural-language querying and dashboards.** Rebuilding that from scratch would be expensive and worse.

The value here is **the semantic layer, the glossary, the guardrails, and the refusal behaviour** — the parts specific to a client's data and definitions, which no product supplies. In a real engagement this sits *on top of* Metabase/Cube rather than beside them. The honest pitch is *"we make your data queryable in plain English, correctly"* — and the correctness is the deliverable, not the chat box.

## Known limitations

See **[`PRODUCTION_GAPS.md`](PRODUCTION_GAPS.md)** for the full critical analysis. In short:

- **Rule-based selector by default.** Legible and free, but weaker on unusual phrasing than an LLM. The LLM path is implemented and constrained identically (it selects names; anything invented is rejected), but is untested against a live provider.
- **SQLite, not a real warehouse.** The guardrail *shapes* port to BigQuery/Snowflake; the specific mechanisms (`mode=ro`, authorizer, EXPLAIN heuristic) do not.
- **No row-level security.** Every question sees all data. A real deployment needs per-user RLS in the warehouse — not a prompt instruction.
- **Sample data, not a client's schema.** The semantic model is the artifact that must be rebuilt per client, and that's the work.
