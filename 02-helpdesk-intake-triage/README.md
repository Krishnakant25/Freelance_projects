# IT Helpdesk Intake & Triage

Chat-based intake that extracts a structured ticket from free text, offers self-service deflection before creating a ticket, and computes priority with a deterministic rules engine — not an LLM guess. This is the buildable, non-medical port of [`02_Diagnostic_Triage_Kiosk_MEDICAL_REFERENCE.md`](02_Diagnostic_Triage_Kiosk_MEDICAL_REFERENCE.md); read [`02_Helpdesk_Intake_Triage.md`](02_Helpdesk_Intake_Triage.md) for the full architecture rationale.

This project is self-contained. It shares no code with `05-hybrid-rag-search-engine` — some modules (embeddings, brute-force cosine search) are deliberately near-duplicated rather than imported, so each project stays independently deployable.

---

## Status

| | |
|---|---|
| Test suite | **114 assertions across 5 suites, all passing** (`python run_all_tests.py`) |
| Priority rules engine | Exhaustively tested — every matrix cell, monotonicity, unknown-value escalation |
| Red-flag scanner | 28 cases — known-dangerous phrases caught, routine tickets not false-positived |
| Ready for | Internal demo, single-instance pilot with a client's real historical tickets |
| **Not** ready for | Public-facing deployment (no API auth — see Known Limitations), high ticket volume without a real ticketing system behind it |

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
python run_all_tests.py         # all 5 suites must pass
```

No API key needed to run anything — `LLM_PROVIDER=none` is the default and is what the test suite and demo script both use.

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

- **No API authentication.** Unlike the RAG project (which has API-key auth as a hard requirement), this build's API has none. That's fine for local testing; it is a real gap for any deployment where "who is submitting this ticket" matters, or where the queue/acknowledge/resolve endpoints shouldn't be open to anyone who can reach the port. Add auth before deploying past a local demo.
- **Rule-based extraction is genuinely weak on ambiguous text.** It's legible and free, but it's keyword matching, not understanding — a cleverly-phrased ticket can slip past every category/impact/urgency keyword and land on `unknown` (which the rules engine then safely escalates, so the failure mode is "over-cautious," not "silently wrong" — but it's still a real accuracy ceiling).
- **The red-flag list and KB deflection threshold are calibrated on a tiny demo set** (13 eval cases, 5 KB articles). Recalibrate against a client's real historical tickets before trusting the numbers — see MANUAL_STEPS.md.
- **No integration with a real ticketing system.** This stands alone; a real deployment sits in front of Zendesk/Jira/Freshservice as a smarter intake layer, not a replacement.
- **Single-instance only** (SQLite, in-process nothing to distribute) — fine for a pilot, not for multi-worker scale. Same tradeoffs as the RAG project's DEPLOYMENT.md, not re-documented here since the pattern is identical.

## Production upgrade path

| This build | Production swap | When |
|---|---|---|
| Rule-based extraction | Groq/Gemini free tier → GPT-4o-mini/Claude Haiku | Ambiguous free text needs real understanding |
| No API auth | API-key auth (same pattern as the RAG project's `app/auth.py`) | Before any non-local deployment |
| SQLite | Postgres | Concurrent writers / real production load |
| Standalone ticket store | Zendesk/Jira/Freshservice API integration | Client already has a ticketing system (usual case) |
| Slack webhook | PagerDuty/Opsgenie | Guaranteed delivery + on-call rotation for P1 |
