# IT Helpdesk Intake & Triage

Chat-based intake that extracts a structured ticket from free text, offers self-service deflection before creating a ticket, and computes priority with a deterministic rules engine — not an LLM guess. This is the buildable, non-medical port of [`02_Diagnostic_Triage_Kiosk_MEDICAL_REFERENCE.md`](02_Diagnostic_Triage_Kiosk_MEDICAL_REFERENCE.md); read [`02_Helpdesk_Intake_Triage.md`](02_Helpdesk_Intake_Triage.md) for the full architecture rationale.

This project is self-contained. It shares no code with `05-hybrid-rag-search-engine` — some modules (embeddings, brute-force cosine search) are deliberately near-duplicated rather than imported, so each project stays independently deployable.

---

## Status

| | |
|---|---|
| Test suite | **~208 assertions across 7 suites, all passing** (`python run_all_tests.py`) |
| Priority rules engine | Exhaustively tested — every matrix cell, monotonicity, unknown-value escalation |
| Red-flag scanner | 28 cases — known-dangerous phrases caught, routine tickets not false-positived |
| Auth | API-key, two roles. Staff/admin endpoints verified rejecting anonymous (401) and wrong-role (403) |
| Alerting | Crash-safe durable outbox; per-ticket cooldown prevents alert spam |
| Escalation | In-process scheduler — verified escalating an unacknowledged P1 autonomously |
| Concurrency | 80 concurrent writes across 8 threads, zero lock errors (WAL + busy_timeout) |
| Cold start | 29ms first request (was ~30s before startup warmup) |
| Ready for | Single-instance deployment (`--workers 1`) on an internal network |
| **Not** ready for | Multi-worker / horizontally-scaled deployment — see the deferred bottleneck below |

A production-readiness audit was run after the initial build, then a second pass to close what it found. Between them: **eleven real defects, all in code that was already passing its full test suite** — a test harness that deleted the production database, network calls held inside DB transactions, a reflected XSS, escalation that never actually ran, and alert logic that would have paged Slack every minute forever once it did. All fixed with regression tests. See **[`PRODUCTION_NOTES.md`](PRODUCTION_NOTES.md)** for the full findings, measurements, and the one bottleneck deliberately deferred.

---

## Roadmap & Tradeoffs

| Decision | Why | Upgrade trigger |
|---|---|---|
| **Rule-based extraction** (`LLM_PROVIDER=none`) as default | Free, no key, and legible — every classification traces to a readable keyword list, not a black box | Genuinely ambiguous free text needs real language understanding; switch to Groq/Gemini (see MANUAL_STEPS.md) |
| **SQLite** over Postgres | Zero infra for a demo/pilot scale | Concurrent writers needed, or ticket volume outgrows a single file |
| **Brute-force KB search** over hybrid/ANN | 5-article demo KB; exact, no index to maintain | KB grows past a few hundred articles (unlikely for most helpdesks — see the RAG project's own benchmark for why this threshold is much higher than intuition suggests) |
| **No API authentication** | Scoped out for this build stage — see below | **Before any deployment reachable by more than the person testing it.** This is a real gap, not a stylistic choice; see Known Limitations |
| **Log-fallback alerting** when no Slack webhook is set | Never silently drops a P1 alert even with zero configuration | A client provides a real Slack/PagerDuty webhook |

**The rules engine is the credibility argument for this whole project**, same principle carried over from the medical version: the LLM extracts, deterministic code decides. It's exhaustively tested (every cell of the Impact × Urgency matrix, monotonicity in both axes, and — the part that matters most — unknown/ambiguous input always escalates, never de-escalates).

---

## What's implemented

- **Red-flag scanner** (`app/redflag.py`) — regex match on security/outage phrases, runs *before* extraction and overrides it entirely. A ransomware report doesn't wait on correct LLM behavior to get flagged P1.
- **Priority rules engine** (`app/rules_engine.py`) — deterministic ITIL-style Impact × Urgency matrix. Unknown values resolve to the *worse* of the two known values on that axis, never the best case.
- **Structured extraction** (`app/extraction.py`) — pluggable provider (rule-based / mock / Groq / Gemini), always validated: an LLM returning an invalid enum value falls back to `unknown` rather than being silently accepted as a wrong-but-valid classification.
- **KB deflection** (`app/kb.py`) — local semantic search offers a self-service article before a ticket is created, only above a confidence threshold (no low-confidence guesses).
- **Alerting + escalation** (`app/alerting.py`) — P1 pages immediately (Slack, or structured logging if unconfigured); unacknowledged P1s re-alert after a time window.
- **Audit log** (`app/db.py`) — append-only record of every classification decision, red-flag match, and alert.
- **CLI + API** (`app/cli.py`, `app/api.py`) — full intake/queue/acknowledge/resolve flow via either interface.

### Bugs the test suite caught during development

1. **"Not urgent" was classified as HIGH urgency.** The word "urgent" is a substring of "not urgent" — checking HIGH-urgency phrases before LOW ones meant negated phrases matched the wrong bucket. Fixed by checking LOW first; regression test added.
2. **"Virus was detected" didn't match the virus red-flag pattern.** The original regex required "virus detected" adjacent; real phrasing has words in between. Fixed with a bounded gap (`\bvirus\b.{0,15}\bdetected\b`).

Both were caught by the test suite before either reached a demo — exactly the argument for having 114 assertions on a project this size, not a smaller "good enough" set.

---

## Setup

```bash
cd 02-helpdesk-intake-triage
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env
python run_all_tests.py         # all 7 suites must pass
```

No LLM key needed — `LLM_PROVIDER=none` is the default and is what the test suite and demo script both use.

**For the API only**, create a key for the staff endpoints (the CLI doesn't need one — it has direct local DB access):

```bash
python scripts/manage_keys.py create --name "helpdesk-team" --roles staff
python scripts/manage_keys.py create --name "ops-lead" --roles admin
```

Intake endpoints stay anonymous by design; only `/tickets`, acknowledge/resolve, and `/admin/*` require a key.

---

## Try it (CLI)

```bash
python -m app.cli ingest-kb data/kb_articles

# Deflectable — offers a self-service article, no ticket created
python -m app.cli report "my VPN keeps dropping every few minutes"

# Org-wide, urgent — P1, alerted immediately
python -m app.cli report "everyone in the office can't access the shared drive, this is blocking all of us" --requester bob

# Red flag — P1 regardless of how casually it's phrased
python -m app.cli report "quick one when you get a chance, I think I clicked a phishing link" --requester carol

python -m app.cli queue
python -m app.cli ack 1
python -m app.cli stats
```

See [`MANUAL_STEPS.md`](MANUAL_STEPS.md) for a full demo script and what (if anything) you need to configure.

---

## Run the API

```bash
uvicorn app.api:app --reload
```

`POST /report`, `GET /tickets`, `POST /tickets/{id}/acknowledge`, `POST /tickets/{id}/resolve`, `POST /admin/check-escalations`. Interactive docs at `/docs`.

**No authentication is implemented on this API.** See Known Limitations below before deploying it anywhere reachable by more than the person testing it.

---

## Known limitations

### Deferred bottleneck: single-process only

**Run with `--workers 1`.** The rate limiter, KB cache, and scheduler are per-process, and SQLite allows one writer. This is a deliberate deferral, not an oversight — measured headroom within one worker is genuinely adequate for the target deployment (80 concurrent writes, zero errors, 29ms latency).

Crossing it means Postgres + Redis + running the scheduler in one place: a real migration, correctly deferred until there's evidence of need. [`PRODUCTION_NOTES.md` §4.1](PRODUCTION_NOTES.md) lists the explicit trigger conditions and interim mitigations (proxy-level rate limiting, external cron for escalation).

### Genuine accuracy ceilings

- **Rule-based extraction is weak on ambiguous text.** Legible and free, but keyword matching, not understanding. A cleverly-phrased ticket can miss every keyword and land on `unknown` — which the rules engine then *escalates*, so the failure mode is over-cautious rather than silently wrong. Still a real ceiling; switch to Groq/Gemini for better handling (MANUAL_STEPS.md).
- **Red-flag list and deflection threshold are calibrated on 13 cases and 5 KB articles.** Can't be meaningfully tuned without a client's real historical tickets. An input we don't have, not a defect.

### Scope

- **No integration with a real ticketing system.** This stands alone; a real deployment sits in front of Zendesk/Jira/Freshservice as a smarter intake layer, not a replacement.
- **CORS is wildcard** (`allow_credentials=False`). Coherent while intake is anonymous and staff auth uses an explicit header rather than cookies, so there's no CSRF surface. Tighten to known origins once the intake page has a fixed deployment domain.

## Production upgrade path

| This build | Production swap | When |
|---|---|---|
| Rule-based extraction | Groq/Gemini free tier → GPT-4o-mini/Claude Haiku | Ambiguous free text needs real understanding |
| SQLite + in-process state | Postgres + Redis | **The deferred bottleneck** — see PRODUCTION_NOTES.md §4.1 for trigger conditions |
| Standalone ticket store | Zendesk/Jira/Freshservice API integration | Client already has a ticketing system (usual case) |
| Slack webhook | PagerDuty/Opsgenie | On-call rotation for P1 (the durable outbox already handles delivery reliability) |
| `keys.json` | Secrets manager (Vault, AWS Secrets Manager) | Key rotation requirements, or more than a handful of keys |
