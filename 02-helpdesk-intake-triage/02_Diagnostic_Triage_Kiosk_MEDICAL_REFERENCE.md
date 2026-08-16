# AI-Powered Diagnostic Intake & Triage Kiosk

Category: **The 24/7 Frontline — AI Voice & Triage Agents**

A self-service kiosk/web app (voice or text) that interviews a patient/customer before they see staff, structures their intake into a summary, applies a triage protocol to flag urgency, and hands the staff a ready-to-use summary instead of a blank form.

> Note: if this targets real medical use, treat it as **decision support only** — it must not diagnose, and any clinical-facing deployment needs a licensed clinician to validate the triage logic and applicable compliance (HIPAA in the US) before going live.

---

## 2. Architecture

```
Patient/Customer (kiosk touchscreen or tablet)
        │
        ▼
Conversational Intake UI (chat or voice)
        │
        ▼
   Intake Agent (LLM)
   - structured question flow
   - free-text follow-ups
        │
   ┌────┼─────────────┐
   ▼    ▼              ▼
Triage   RAG over       Structured
Rules    protocol docs  data extractor
Engine   (symptom refs)  (JSON schema)
   │
   ▼
Risk level (e.g. Low/Medium/High/Urgent)
   │
   ▼
Staff Dashboard ── urgent → SMS/alert to staff
   │
   ▼
Optional: push to EHR/CRM
```

Two-layer design is deliberate: the **LLM** handles natural conversation and extraction; a **deterministic rules/scoring layer** (not the LLM) makes the final urgency call, using a documented protocol (e.g., a symptom-severity checklist), so triage decisions are auditable and not "vibes from a language model."

---

## 3. Core Components

| Component | Role |
|---|---|
| Kiosk/Web UI | Touch or voice intake interface, multi-language optional |
| Intake Agent | Asks structured + adaptive follow-up questions |
| Extraction layer | Converts conversation into a structured JSON record (symptoms, duration, severity, history) |
| Triage rules engine | Deterministic scoring against a documented protocol → risk tier |
| Protocol knowledge base | RAG store of the reference protocol/policy docs the rules engine and agent cite |
| Staff dashboard | Queue of intakes sorted by urgency, full transcript on demand |
| Alerting | SMS/push to staff for high-urgency cases |
| Audit log | Immutable record of every intake + triage decision, for compliance review |

---

## 4. Tech Stack

### Phase 1 — Free / prototype

> ⚠️ **Synthetic data only — see §6.3.** Every row below is unusable with real patient data: the Web Speech API ships audio to Google's servers, free-tier LLM APIs won't sign a BAA and may train on inputs, and the free database tier carries no BAA either. Real PHI requires BAA-covered infrastructure or a fully on-prem/on-device stack. Also read §6.1 before building the triage output at all.

| Layer | Tool | Notes |
|---|---|---|
| UI | Next.js/React + Web Speech API (browser STT/TTS) | No telephony needed, runs on any tablet browser |
| LLM | Gemini Flash free tier / Groq Llama 3.1 free tier | Cheap enough for long structured conversations |
| Extraction | LLM function-calling → JSON schema, validated with Pydantic/Zod | Enforce structure, reject malformed output |
| Rules engine | Plain code (Python/TS) implementing a published protocol's decision tree | Free — the protocol itself may need a licensed clinical reviewer |
| Knowledge base | Local embeddings + Chroma/SQLite | Small protocol/FAQ corpus |
| Dashboard | Streamlit or a simple Next.js admin page | Free, fast to build |
| Storage | Supabase free tier (Postgres) | Row-level auth for staff-only access |
| Alerting | Twilio SMS pay-as-you-go (fractions of a cent/msg) | Only real ongoing cost at this phase |

### Phase 2 — Paid / production, compliance-aware

| Layer | Tool | Why |
|---|---|---|
| LLM | GPT-4o or Claude Sonnet | Better instruction-following on structured, high-stakes extraction |
| Hosting | AWS/GCP with a signed BAA (if handling PHI) | Required for HIPAA-relevant deployments |
| Database | Postgres (RDS/Cloud SQL), encrypted at rest | Compliance-grade storage |
| Identity/access | Auth0 or Cognito with role-based staff access | Least-privilege access to patient data |
| Alerting | Twilio + PagerDuty/Opsgenie for critical-tier escalation | Guaranteed delivery for urgent cases |
| EHR integration | HL7/FHIR connector (e.g. via Redox or a direct FHIR API) | Only if pushing into an existing clinical system |
| Monitoring/audit | Structured logging to a SIEM (e.g. Datadog) | Required for compliance audit trails |

---

## 5. Build Sequence

1. **Get the triage protocol locked down first** — source a validated protocol (or have a domain expert define one); this is the compliance-critical piece, build it before any UI.
2. **Encode the protocol as deterministic rules**, separate from the LLM, with full test coverage of edge cases.
3. **Design the structured JSON schema** the LLM must extract into (symptoms, onset, severity, red-flag answers).
4. **Build the conversational intake flow** — adaptive follow-ups, but every path must terminate in a schema-complete record.
5. **Wire the rules engine to consume the extracted record** and output a risk tier + reasoning trace.
6. **Build the staff dashboard** — queue sorted by urgency, full transcript, one-click escalate.
7. **Add alerting** for high-urgency cases with a guaranteed-delivery channel.
8. **Add an audit log** — every decision, input, and model version stored immutably.
9. **Pilot with synthetic/test cases first**, then a supervised real pilot with a clinician reviewing every triage decision.
10. **Only after validated accuracy**, move to Phase 2 infra and unsupervised production use.

---

## 6. Reality Check — Why the Naive Build Fails, and the Fix

This is the most dangerous project in the set. Not technically — architecturally it's the easiest of the nine — but because **the buildable version and the sellable version are separated by a regulatory wall** that §1–§5 above walks straight into.

### 6.1 A patient-facing tool that outputs an urgency level is probably a regulated medical device
**Failure:** §2 outputs "Low/Medium/High/Urgent." In the US, the Clinical Decision Support exemption carved out by the 21st Century Cures Act is written for **healthcare professionals** who can independently review the basis of the recommendation — it does not cover a kiosk handing an acuity assessment to a patient. Under EU MDR, triage software is typically **Class IIa**. That means notified-body review, clinical evaluation, QMS under ISO 13485, and a multi-year timeline. No freelance studio ships that as a project.

**Fix — change the output, not the tech.** Build it as a **structured intake and pre-registration tool**, not a triage engine:
- It captures history, symptoms, duration, medications, and consent forms into a structured record.
- It **does not compute or display an acuity score to anyone.**
- It surfaces **verbatim red-flag phrases** to staff ("patient reported chest pain radiating to left arm") — reporting what was said, not what it means.
- Clinical judgment stays entirely with the clinician, who is now reading a complete structured history instead of a blank form.

That version keeps ~90% of the client value (staff time saved, better data capture, shorter queues) and sits far outside device classification. Confirm with the client's regulatory/compliance counsel regardless — this section is engineering guidance, not legal advice.

### 6.2 The triage protocols aren't yours to encode
**Failure:** §5 step 1 says "source a validated protocol." The established ones — ESI, Manchester Triage System, Schmitt-Thompson — are **copyrighted and commercially licensed**, with terms covering derivative works and required user training. You cannot read one and encode it into a client's product. And writing your own protocol means you have invented an unvalidated clinical instrument, which is worse.

**Fix:** Either license one properly (a real budget line and a real contract, usually only viable for a funded healthcare client), or apply §6.1 and don't implement triage logic at all. There is no free path through this.

### 6.3 The entire Phase 1 stack is illegal for real patient data
**Failure:** This is the sharpest error in the original doc. If the system touches PHI:
- **Web Speech API** in Chrome ships audio to Google's servers for recognition. That's an unauthorized PHI disclosure.
- **Free-tier LLM APIs** generally reserve the right to train on inputs and **will not sign a BAA**. Free tiers in particular are usually excluded from enterprise data terms.
- **Supabase free tier** doesn't come with a BAA either.

The Phase 1 table is fine for a synthetic-data demo and unusable the moment a real patient types a real symptom into it.

**Fix:** Two clean paths. Either **(a)** keep Phase 1 strictly for synthetic data and demos, and move to BAA-covered infrastructure (Azure OpenAI / AWS Bedrock / Google Cloud with a signed BAA, on-prem or in-VPC STT) before a single real user; or **(b)** run the intake **fully on-device/on-prem** — local Whisper for speech, a local model for extraction — so PHI never leaves the client's network. Option (b) is often the easier sell to a clinic anyway, because "your data never leaves the building" closes the objection.

### 6.4 Ambiguity resolves the wrong way
**Failure:** LLM extraction of severity from free text will sometimes miss a red flag. In this domain the error cost is wildly asymmetric — a false "low urgency" on a cardiac event is catastrophic; a false "needs a human" is a mild inconvenience.

**Fix:** Make the system **structurally biased toward escalation**. Any extraction below a confidence threshold, any unrecognized symptom, any contradiction between answers, any expression of distress → flag for immediate human review. Also enumerate **hard-coded red-flag phrases** (chest pain, trouble breathing, suicidal ideation, stroke symptoms) that trigger an immediate staff alert via keyword match **before and independently of** any LLM call — a regex you can prove works beats a model you have to trust.

### 6.5 A kiosk queue is not a waiting room
**Failure:** §2 ends at "staff dashboard sorted by urgency." A patient who deteriorates after completing intake is now an idle row in a queue, and the system has created a false sense that someone is watching.

**Fix:** The kiosk must never be the only observation layer. Alerts push to staff (audible/paged), not just into a dashboard someone might refresh. High-flag intakes require an **acknowledgment click** with escalation if unacknowledged within N minutes. And the physical deployment must keep the kiosk area in staff line-of-sight.

### 6.6 A meaningful share of users can't use it
**Failure:** Elderly patients, low-literacy users, non-native speakers, people in pain, people with visual/motor impairments, people without the dexterity for a touchscreen. Designing for the median user excludes exactly the population most likely to need urgent care.

**Fix:** The kiosk is **always optional**, never the only intake path — a staffed alternative must exist and be visible, not buried. Build to WCAG 2.1 AA (screen-reader support, large-touch targets, high contrast), support the languages of the actual local patient population, and note that federally funded US facilities have language-access obligations under Title VI. Also plan for hygiene on a shared touchscreen, and for the kiosk being physically out of service.

### 6.7 The higher-margin version of this project isn't medical at all
**Failure:** Medical is where this idea is most obvious and least buildable — longest sales cycle, heaviest compliance, highest liability, and a client who cannot sign without legal review.

**Fix — port the identical architecture to unregulated verticals**, where it ships in weeks instead of quarters:
- **IT helpdesk triage** — structured incident intake, priority routing (the exact same rules-engine pattern, zero regulatory load)
- **Insurance / legal intake** — first-notice-of-loss, case qualification
- **Home services / trades** — job scoping and urgency before dispatch
- **Veterinary** — same clinical shape, dramatically lighter regulation

Build one of these first. It proves the architecture, generates revenue and a case study, and gives you something real to show a healthcare client later — when you'll have the budget to do the regulated version properly.
