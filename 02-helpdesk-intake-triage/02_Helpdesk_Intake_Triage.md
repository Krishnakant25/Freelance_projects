# IT Helpdesk Intake & Triage

Category: **The 24/7 Frontline — AI Voice & Triage Agents** *(non-medical port of [`02_Diagnostic_Triage_Kiosk_MEDICAL_REFERENCE.md`](02_Diagnostic_Triage_Kiosk_MEDICAL_REFERENCE.md) — see that file §6.7 for why)*

A chat-based intake that turns a free-text incident description into a structured, correctly-prioritized ticket — with self-service deflection for issues that don't need a human at all, deterministic (not LLM) priority scoring, and hard-coded escalation for security/outage keywords that must never depend on a model getting it right.

Same architecture shape as the medical version — LLM for conversation, deterministic rules engine for the decision that matters — but here the rules engine has **no regulatory wall**: ITIL-style Impact × Urgency priority matrices are open industry practice, not a licensed clinical instrument. That single difference is what makes this shippable in weeks instead of quarters.

---

## 1. What It Does

- User describes a problem in plain language (chat widget, Slack bot, or email-in)
- System extracts a structured incident record: category, affected system, business impact, urgency signals
- Checks a knowledge base first — if a known fix exists, offers it before creating a ticket (**deflection**, the actual ROI story for a helpdesk client)
- If unresolved, creates a ticket with a **deterministically computed priority** (P1–P4), not an LLM guess
- Security/outage keywords force P1 **independently of the LLM**, before it even runs
- Staff see a priority-sorted queue with full context already captured — no blank-form triage
- P1 tickets alert immediately and escalate if unacknowledged
- Every classification decision is logged, so "why is this a P2" has a real answer

---

## 2. Architecture

```
User (chat widget / Slack / email-in)
        │
        ▼
Red-Flag Keyword Scan (regex, no LLM, runs first)
        │
   ┌────┴─────┐
   │ matched?  │──yes──▶ Force P1, skip straight to ticket creation
   └────┬─────┘
        │ no
        ▼
Intake Agent (LLM)
   - structured question flow
   - free-text extraction
        │
        ▼
Extraction Layer → JSON schema
 (category, system, impact, urgency, description)
        │
        ▼
KB Deflection Search (local semantic search over KB articles)
        │
   ┌────┴─────┐
   │ good match?│──yes──▶ Offer self-service article. User confirms resolved? → done, no ticket
   └────┬─────┘
        │ no / not resolved
        ▼
Priority Rules Engine (deterministic, Impact × Urgency matrix)
        │
        ▼
   Ticket created (P1–P4)
        │
   ┌────┴─────┐
   │ P1?       │──yes──▶ Alert on-call (Slack/webhook) → escalate if unacknowledged in N minutes
   └────┬─────┘
        │
        ▼
Staff Dashboard (queue sorted by priority, full transcript, audit trail)
```

The same two-layer principle as the medical version: the **LLM extracts**, a **deterministic engine decides**. The rules engine is plain code with full test coverage, auditable, and — critically — the red-flag scan runs *before* the LLM and can override it, so a security incident doesn't depend on correct model behavior to get flagged.

---

## 3. Core Components

| Component | Role |
|---|---|
| Red-flag scanner | Regex match on security/outage phrases, forces P1 before any LLM call |
| Intake agent | Conversational extraction of the incident into structured fields |
| Extraction schema | Validated JSON: category, affected_system, business_impact, urgency, description, requester |
| KB deflection search | Local semantic search over KB articles; suggests a fix before a ticket is created |
| Priority rules engine | Deterministic Impact × Urgency → P1–P4, industry-standard ITIL pattern |
| Ticket store | SQLite: tickets, KB articles, immutable audit log |
| Alerting | Slack webhook (or log fallback) for P1, with unacknowledged-escalation |
| Dashboard/API | Priority-sorted queue, full transcript and classification reasoning per ticket |

---

## 4. Tech Stack

### Phase 1 — Free / self-hosted

| Layer | Tool | Notes |
|---|---|---|
| LLM (extraction) | Groq/Gemini free tier, or `none` extractive fallback for zero-cost testing | Same pluggable-provider pattern as the RAG project |
| KB search | Local `sentence-transformers` embeddings, cosine similarity | Small corpus (tens–hundreds of KB articles); no need for hybrid search or ANN at this scale |
| Rules engine | Plain Python, fully unit tested | Free, and this is the piece that must be provably correct |
| Storage | SQLite | Tickets + KB + audit log in one file, same rationale as the RAG project |
| Alerting | Slack incoming webhook (free) | Or just structured logging if no Slack workspace yet |
| API/Dashboard | FastAPI + a minimal queue view | Free |

### Phase 2 — Paid / integrated with a real ticketing system

| Layer | Tool | Why |
|---|---|---|
| Ticketing backend | Zendesk / Jira Service Management / Freshservice API | Most clients already have one — this becomes a smarter intake layer in front of it, not a replacement |
| LLM | GPT-4o-mini or Claude Haiku | Cheap, fast, good enough for structured extraction at volume |
| KB search | Same corpus, synced from the client's real KB (Confluence, Notion, Zendesk Guide) | Keeps deflection current without manual duplication |
| Alerting | PagerDuty/Opsgenie | Guaranteed delivery + on-call rotation for P1 |
| Identity | SSO tied to the client's directory | Staff-only dashboard access |

---

## 5. Build Sequence

1. **Define the priority matrix first** — Impact (single user / department / org-wide) × Urgency (can wait / blocking work / critical) → P1–P4. This is the auditable core; write it and test its edge cases before anything else.
2. **Build the red-flag keyword list** with the client (security terms, outage terms) — this list is domain knowledge they have and you don't; don't invent it in a vacuum.
3. **Build extraction** — LLM turns free text into the structured schema, validated, with a safe default (unknown → treat as higher urgency, never silently drop a field).
4. **Wire the rules engine to consume extraction output** and produce a priority + a visible reasoning trace ("Impact: department-wide, Urgency: blocking work → P1").
5. **Add KB deflection** — semantic search over a small starter KB, offer-before-ticket flow, track how often it actually resolves things (this is the number that sells the project).
6. **Add the audit log** — every classification decision stored immutably.
7. **Add alerting** for P1 with an escalation timer.
8. **Build the dashboard** — priority queue, transcript, one-click reassign/escalate.
9. **Pilot against real historical tickets** — feed in 50–100 real past incident descriptions, compare the engine's priority to what a human actually assigned, tune the matrix and red-flag list from real disagreements.
10. **Integrate with the client's real ticketing system** (Phase 2) once the standalone version proves accurate.

---

## 6. What Carries Over From the Medical Version's Lessons (Applied Here)

- **The rules engine, not the LLM, makes the priority call** — same principle as §2 of the medical doc, for the same reason: auditability. Here it's cheaper to get right because there's no licensed protocol to violate.
- **Asymmetric error cost still applies, just lower-stakes.** A missed security incident is expensive; a wrongly-escalated P4 is a mild annoyance. Bias the red-flag scanner toward false positives, same logic as the medical doc's §6.4, lower consequence.
- **A ticket queue is not monitoring**, same as the medical doc's §6.5 — a P1 needs a real page, not a dashboard someone might refresh, and unacknowledged P1s must escalate.
- **No regulatory wall here** — this is precisely the §6.7 "port to an unregulated vertical" move the medical doc recommends, done.
