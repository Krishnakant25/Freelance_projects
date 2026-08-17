# Production Notes

Findings from a production-readiness audit of this project, what was fixed, and what is deliberately still open. Written after the fact, from an actual code audit — every item below was a real defect or gap found in code that was already passing its own test suite, which is the useful part.

---

## 1. Defects found and fixed

These all passed the existing tests. That's the point: a green suite proves the behaviours you thought to test, not the absence of these.

### 1.1 The test suite deleted the production database — **data loss**

`eval/run_eval.py` called `config.DB_PATH.unlink()` in its setup, and the other suites wrote directly to the real DB. Running the tests destroyed whatever tickets and KB articles existed.

This was actively causing confusion during development — the demo database kept "mysteriously" emptying, and it was being manually re-seeded each time rather than recognised as a bug.

**Fix:** `eval/_harness.py` redirects `config.DB_PATH` to a per-process temp file before any `app.*` import, and also forces `SLACK_WEBHOOK_URL=""` and `LLM_PROVIDER=none` so tests can never make real network calls. A test now asserts the active DB path is *not* the real one.

### 1.2 Network I/O inside database transactions — **serialised all writes**

`intake._create_ticket()` ran `extract_incident()` (an LLM call, up to a 30s timeout) and `send_p1_alert()` (a Slack call, 10s) *inside* `with db.session()`. SQLite allows one writer, so the write path was held open across two separate network round-trips. Every concurrent ticket submission queued behind whichever request was waiting on an external API — one slow LLM response stalled the entire helpdesk.

`alerting.check_escalations()` had the same shape, looping Slack calls while holding a session.

**Fix:** both restructured into read → network → write phases, so transactions contain only local work. A regression test starts a deliberately slow extractor and asserts a concurrent write still completes in under a second (it does — the test would have taken 2s+ before).

### 1.3 KB cache never invalidated on ingest — **silently stale search**

`kb.ingest_kb_article()` did not invalidate the in-process search cache. A newly added article was invisible to deflection search until the process restarted: the article was in the database, visible to an admin, and users were still told there was no self-service answer.

**Fix:** invalidate on ingest. Also replaced the pattern where `search()` read cache globals *after* releasing the lock — a concurrent invalidation could set them to `None` mid-query and crash the request — with a snapshot that can't be torn out from under the caller.

### 1.4 XSS in the voice UI — **reflected markup**

`voice.html` interpolated server values into `innerHTML`. The `reasoning` string embeds the red-flag `matched_phrase`, which is a regex match against **user-supplied text**, and several red-flag patterns contain wildcards (e.g. `\bvirus\b.{0,15}\b(detected)\b`). A crafted description therefore reflected attacker-controlled markup into the page.

Confirmed live, not theoretically: submitting `virus <img src=x> detected on my laptop` produced the server reasoning `matched 'security' phrase 'virus <img src=x> detected'`.

**Fix:** the UI builds DOM nodes and assigns `.textContent`. Re-tested with the same payload: 0 `<img>` elements in the DOM, markup rendered as inert visible text. A static test now fails the build if any `innerHTML` assignment contains `${...}` interpolation.

### 1.5 No input length limits — **CPU/memory amplification**

`/report` accepted unbounded `description`. The endpoint runs an embedding model over that text, so a multi-MB body is cheap to send and expensive to serve.

**Fix:** `MAX_DESCRIPTION_CHARS` (4000) and `MAX_REQUESTER_CHARS` (120), enforced by Pydantic with a blank-string validator, plus a matching client-side check for a clearer message. Verified: 5000 chars → HTTP 422.

### 1.6 Audit log was mutable — **unfounded assurance**

The schema comment said "append-only. Never UPDATE or DELETE" but nothing enforced it. An audit log that a bug or a person can quietly rewrite provides no more assurance than none, while looking like it does.

**Fix:** `BEFORE UPDATE` / `BEFORE DELETE` triggers that `RAISE(ABORT)`. Tests assert both are rejected and the row survives.

### 1.7 Other

- **`P1_ESCALATION_MINUTES` was f-string-interpolated into SQL.** A config value, not user input, so not exploitable — but replaced with a parameterised query and `int()` coercion regardless.
- **Malformed numeric env vars crashed at import.** `KB_DEFLECTION_THRESHOLD=abc` took the process down on boot. Now warns and falls back to the default.
- **Unbounded `SELECT` on `/tickets`.** A table that only grows; pagination is now enforced (capped at 500) with a `total` count returned.
- **Single alert attempt.** A dropped page on a security incident is this system's most expensive failure. Now retries with backoff (3 attempts) and always falls back to an `ERROR` log.
- **Rate-limiter memory leak.** The key map grew one entry per distinct client forever. Now pruned opportunistically; a test seeds 50 clients and asserts they're reclaimed.

---

## 2. Scale changes

### 2.1 SQLite concurrency

Connections had no PRAGMAs at all. Added:

| PRAGMA | Why |
|---|---|
| `journal_mode=WAL` | Readers no longer block the writer. Without it a dashboard refresh could fail a concurrent ticket submission. |
| `busy_timeout=10s` | Without it, contention raises `database is locked` **immediately** — surfacing as a 500 to a user submitting a ticket. |
| `synchronous=NORMAL` | Safe with WAL, substantially faster than FULL. Trade-off: a hard OS crash can lose the last transaction(s). Acceptable for helpdesk tickets. |
| `foreign_keys=ON` | Off by default in SQLite; the schema declares FKs that were silently unenforced. |

**Measured:** 8 threads × 10 writes = 80 concurrent writes, zero errors, all rows persisted. That test failed to exist before, and the configuration would not have survived it.

### 2.2 Cold start: ~30s → 29ms

The embedding model loaded lazily on first request. The first real user waited roughly **30 seconds** — observed during browser testing, where it read as a hung page rather than a slow one.

**Fix:** `embeddings.warmup()` at startup (loads the model *and* runs one encode, since the first forward pass does additional lazy init). Model loading is now also mutex-guarded — concurrent first requests each built their own model before one won the assignment.

**Measured:** startup pays 11.8s; first real request now **29ms**.

### 2.3 Observability

Added `/ready` (distinct from `/health`), which reports ticket counts, KB article count, whether the model is actually warmed, alerting mode, and explicit warnings such as an empty KB. A process that is "up" but hasn't loaded its model will serve a 30-second first request, so liveness alone is a misleading signal for a load balancer to route on.

Also added structured logging (`LOG_JSON=true`) and `X-Request-ID` on every response, echoing a caller-supplied value when present.

---

## 3. Second pass — the remaining gaps, closed

A follow-up pass closed everything from the original "still open" list except the one genuine infrastructure ceiling (§4).

### 3.1 Staff endpoints now require authentication — **closed**

`app/auth.py` adds API-key auth with two roles, hashed at rest (`scripts/manage_keys.py` to manage them).

The two-tier split is the design, not a compromise:

| Tier | Endpoints | Auth |
|---|---|---|
| **Intake** | `/report`, `/report/file-anyway`, `/deflection/feedback` | **Anonymous by design** — public submission surface for a widget/kiosk; exposes nothing but your own ticket id. Protected by input caps + rate limiting. |
| **Staff** | `/tickets`, `/tickets/{id}`, acknowledge, resolve, `/stats` | `staff` role. These expose every ticket's contents — in a helpdesk that includes whatever users typed, up to credentials they shouldn't have pasted. |
| **Admin** | `/admin/*` | `admin` role (implies staff). Separate so a read-mostly key can't fire pages or reload credentials. |

Verified on a live server: anonymous → 401, staff key → 200, staff key on an admin endpoint → 403, and intake still works with no key at all. State changes now record the acting principal, so "who acknowledged this" has an answer.

### 3.2 Alert delivery is now crash-safe — **closed**

The alert **intent** is written to `alert_outbox` in the **same transaction as the ticket**, before any network call. Delivery is a separate retryable step, `flush_outbox()` runs on startup and on every scheduler tick, and a permanently undeliverable alert becomes a `failed` row surfaced in `/ready` rather than a gap in the logs.

Tested by monkeypatching delivery to raise mid-flow: the ticket is still created, a `pending` row survives the simulated crash, and a later flush delivers it.

### 3.3 Escalation now actually runs — **closed**

`app/scheduler.py` runs an in-process periodic sweep, so the "unacknowledged P1s escalate" guarantee holds without external cron. Previously nothing called `check_escalations()` — the feature worked when tested by hand and silently never fired otherwise, which is the worst kind of gap because the demo passes.

Verified live: with an 8s interval, an unacknowledged P1 escalated on its own without any manual trigger.

### 3.4 Escalation alert spam — **fixed** (found while building the scheduler)

`check_escalations()` had no cooldown, so it re-alerted the **same ticket on every invocation**. On a one-minute scheduler an unacknowledged P1 would page Slack every minute indefinitely — alert fatigue that trains people to mute the channel, defeating the entire point of a P1.

Fixed with a per-ticket cooldown (`ESCALATION_COOLDOWN_MINUTES`, default 30) tracked via the outbox. Verified live across 9 scheduler ticks: **1 escalation alert, not 9.**

---

## 4. Deferred — the one real bottleneck left

### 4.1 Single-process only (SQLite + in-memory state)

**This is deliberately not fixed.** Three pieces of state are per-process — the rate limiter, the KB cache, and the scheduler — and SQLite allows a single writer.

Run with **`--workers 1`**. Within that, the measured headroom is genuinely fine for the target deployment: 80 concurrent writes with zero lock errors, 29ms request latency, and an embedding model that only needs loading once.

**What crossing this costs:** Postgres (concurrent writers) + Redis (shared limiter and cache) + running the scheduler in exactly one place. That's a real migration, not a config change, and it is the correct thing to defer until there's evidence of need.

**Trigger conditions — revisit when any is true:**
- Sustained request volume that one worker can't serve (the reranker-equivalent bottleneck here is the embedding model; measure before assuming)
- Ticket volume where SQLite write contention shows up as latency, not just theory
- A requirement for zero-downtime deploys or horizontal redundancy
- More than one instance needed for availability reasons

Interim mitigations that avoid the migration: enforce rate limits at the reverse proxy instead of in-process, and set `SCHEDULER_ENABLED=false` with external cron hitting `POST /admin/check-escalations` if you do run multiple workers. The per-ticket escalation cooldown also caps multi-worker duplicate pages at one per cooldown window rather than unbounded, so the failure mode degrades gracefully rather than becoming spam.

### 4.2 Calibration on a small set — needs client data, not code

13 eval cases, 5 KB articles. The red-flag list and `KB_DEFLECTION_THRESHOLD` can't be meaningfully tuned without a client's real historical tickets. Not a defect; an input we don't have yet.

### 4.3 CORS is wildcard

`allow_origins=["*"]` with `allow_credentials=False`. Coherent while intake is anonymous and credentials are never sent cross-origin — the staff endpoints authenticate via an explicit header, not cookies, so there's no CSRF surface. Tighten to specific front-end domains when the intake page is deployed to a known origin.

---

## 4. Verification

```bash
python run_all_tests.py    # 7 suites
```

| Suite | Assertions |
|---|---|
| Priority rules engine (exhaustive) | 43 |
| Red-flag keyword scanner | 28 |
| Structured extraction | 28 |
| Alerting + escalation (incl. cooldown + crash-safe outbox) | 20 |
| Production hardening regressions | 33 |
| API validation / rate limiting / probes / **auth** | 50 |
| End-to-end intake pipeline | 13 cases |
| **Total** | **~208** |

The hardening and API suites exist specifically to cover the defects above — each would have passed silently before its fix.

Also verified on a live server rather than only in tests: auth tiers (401/403/200), autonomous scheduler escalation, cooldown suppressing repeats across 9 ticks, warmup (11.9s at startup → 29ms first request), and a real XSS payload rendering inert.

---

## 6. Summary

| | Found | Fixed | Deferred |
|---|---|---|---|
| Data loss / correctness | 4 | 4 | — |
| Security | 3 | 3 | — |
| Reliability | 4 | 4 | — |
| Scale / infra | 3 | 2 | 1 (multi-worker) |

Eleven defects, all in code that was passing its own full test suite before the audit. The one deferred item is a genuine infrastructure migration with documented trigger conditions, not an unresolved bug.
