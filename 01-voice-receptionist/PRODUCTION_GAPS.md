# Production Gaps — Critical Analysis

A deliberately adversarial review of this build: **what will break when real callers hit it.**

Split into two parts:
- **§1 — Fixed.** Demo-visible defects found during the audit and repaired, because a viewer would hit them.
- **§2 — Deferred.** Real production failures that require infrastructure (telephony, servers, paid services) and are therefore *documented, not implemented*. These are not oversights; each has a named trigger and an implementation note.

The demo is honest about what it is: **the agent's reasoning and booking safety, driven from a console instead of a phone line.** Everything in §2 sits between that and a system a business could actually point its phone number at.

---

## 1. Fixed — defects a demo viewer would have hit

All six were found by auditing paths a person naturally tries, not by the test suite passing.

### 1.1 A requested day/time was silently ignored

`datetime_hint` was extracted from the caller's speech and then **never used**. Asking *"can I book something Thursday afternoon?"* offered Monday 9am with no acknowledgement — which reads as the agent not listening, the single most damaging impression a voice agent can make.

**Fixed:** `calendar_tool.slots_matching_hint()` filters by weekday, relative day, time-of-day, and explicit hour. When the hint genuinely can't be met, the agent *says so* (*"I don't have anything matching Thursday afternoon, but I do have…"*) rather than pretending it wasn't asked.

### 1.2 Declining offered times looped forever

The "none of those work" branch computed a later window, **threw it away**, and re-offered the identical three slots. Dead code that looked correct. A caller declining twice heard the same times repeated.

**Fixed:** a pagination cursor (`slot_offset`) advances through real availability, wrapping to the earliest with an explicit *"that's everything I have in the diary"* rather than dead-ending.

### 1.3 "None of those work" wasn't recognised as a refusal — **and the eval hid it**

`^(no)\b` does not match `"none"`. So the most natural way to decline offered times was classified as *confusion*, pushing the caller toward the escape hatch.

The instructive part: **the replay script for this case passed.** It asserted only `outcome == "booked"`, which stayed true because the *next* turn picked a slot. A correct ending concealed a broken middle.

**Fixed twice over:** refusal patterns extended (`none of those`, `neither`, `doesn't work`, `something later`, …), *and* the eval harness now asserts on the middle of a call — `expect_no_confusion` and `expect_distinct_slot_offers`. Asserting outcomes alone is not enough, which is a lesson worth more than the bug.

### 1.4 Ordinary words parsed as confirmation codes

The code alphabet (`ACDEFGHJKLMNPQRTUVWXY34679`) spells common words. `"cancel my appointment"` parsed `CANCEL` as a confirmation code, the lookup failed, and the caller got a needless *"I couldn't find that."*

**Fixed:** codes are only accepted when introduced by "code"/"reference", when they *are* the whole utterance, when they contain a digit, or when explicitly uppercase — plus a false-positive stoplist.

### 1.5 First FAQ question stalled the call for ~11 seconds

The embedding model loaded lazily, so the first question a caller asked paid the full load **mid-conversation**. On a phone line that is dead air; in a recording it looks like a hang.

**Fixed:** `app/cli.py` warms the model before the call starts and prints how long it took.

### 1.6 Dead `BUSINESS_TIMEZONE` config

Declared in `config.py`, documented in `.env.example`, and **never read by any code path** — implying a capability that didn't exist. Removed, with the naive-local-time assumption stated explicitly (and the real requirement tracked in §2.4).

### 1.7 Also fixed while auditing

- **Confirmation-code collisions** surfaced as *"that time was taken"* — a misleading message for an unrelated cause. Now retries generation.
- **Turns arriving after a call ended** restarted the conversation and overwrote the recorded outcome. Now ignored, with a guard in `handle()`.

---

## 2. Deferred — production failures needing infrastructure

Each of these is a genuine way the product fails with real callers. None can be built or verified without paid services or a server, so they're specified rather than faked.

### 2.1 There is no telephony — **the biggest gap by far**

**What breaks:** nothing works over an actual phone. There is no phone number, no audio, no call connection.

**What's needed:** a Twilio number plus a real-time media pipeline. Per the architecture doc §6.1, **do not hand-roll this** — use **Vapi**, **Retell**, or **LiveKit Agents**. They exist because turn-taking is the hard part, and a DIY `Twilio Media Streams → STT → LLM → TTS` loop realistically lands at 1.5–3s per turn, where callers interpret >1.2s of silence as a dropped call.

**Cost reality:** roughly **$0.08–0.20/min all-in**. The "free" Phase-1 stack in the architecture doc is not commercially deployable — Edge-TTS is an unofficial endpoint with no licensing, local Whisper isn't streaming-native, and free LLM tiers rate-limit into dead air on concurrent calls.

**Trigger:** the first real client call.

### 2.2 No turn-taking, endpointing, or barge-in

**What breaks — and this alone makes an otherwise-perfect agent unusable:**
- **No endpointing.** Nothing decides when the caller has *finished* speaking. People pause mid-sentence reciting phone numbers; naive silence detection cuts them off halfway through their own number.
- **No barge-in.** The agent talks over interruptions. Worse, without truncating context on interruption, it believes it said things the caller never heard — so it references information they don't have.

**What's needed:** VAD plus **semantic** endpointing (is the utterance *finished*, not merely silent), and barge-in that kills TTS playback *and* truncates the agent's context to what was actually spoken aloud.

**Trigger:** same as §2.1 — this is part of the transport, which is why using a platform matters.

### 2.3 Phone audio will break the digit capture

**What breaks:** telephony is **8kHz μ-law narrowband**. STT models benchmarked on clean 16kHz audio degrade sharply on it — exactly on names, addresses, spelled-out emails, and phone numbers, which is the data a receptionist must capture perfectly.

**Partially mitigated already:** read-back confirmation for phone numbers, digits spoken individually, a DTMF keypad fallback offered on failure, and confirmation codes that avoid `0/O`, `1/I`, `5/S`, `8/B`.

**Still needed:** a telephony-tuned STT model (Deepgram's phonecall/Nova models), **keyword boosting** for the business's vocabulary (staff names, service names, local street names), and actual DTMF wiring — the agent *offers* the keypad but nothing captures the tones.

**Trigger:** §2.1. The DTMF gap is the sharpest: the agent currently makes an offer it can't fulfil over a real line.

### 2.4 No timezone handling

**What breaks:** all datetimes are naive local time on the host. A server in UTC serving a business in another zone offers **wrong appointment times** — silently, and consistently.

**What's needed:** store slots in UTC, render in the business's zone via `zoneinfo`, and handle DST transitions (a 30-minute slot at a DST boundary is a genuine edge case).

**Trigger:** deployment to any server not in the business's local timezone — i.e. essentially any cloud deployment.

### 2.5 No SMS confirmation

**What breaks:** callers expect written confirmation. The gate is currently *off* so the agent doesn't promise what it can't deliver (see §1 of the README) — but the expectation still exists.

**What's needed:** Twilio SMS, then `SMS_CONFIRMATIONS_ENABLED=true`. Send from the **calendar/booking system**, not the agent: if the caller doesn't get the text, the booking didn't happen — the same principle as gating the spoken confirmation on the tool result.

### 2.6 Local calendar, not the client's

**What breaks:** bookings land in a local SQLite table nobody in the business looks at. Staff would need to watch a separate system, so double-bookings return via the *human* side.

**What's needed:** Google Calendar or Cal.com API as the system of record. The slot-locking design ports, but the atomic claim has to be re-established against the remote API's semantics — most calendar APIs offer no equivalent of `BEGIN IMMEDIATE`, so this needs a local reservation table fronting the remote calendar with reconciliation.

**Trigger:** any real client, who already has a calendar.

### 2.7 Recording storage and retention are unimplemented

**What breaks:** `RECORDING_ENABLED` controls only the spoken *disclosure*. No audio is stored, and `TRANSCRIPT_RETENTION_DAYS` is **not enforced** — transcripts accumulate forever. For a business with retention obligations, that's a compliance problem that gets worse with time.

**What's needed:** encrypted storage, a retention job that actually deletes, and a documented policy. Note that consent **cannot be retrofitted** onto recordings already taken, which is why disclosure is structural in the greeting today.

### 2.8 No monitoring, alerting, or on-call path

**What breaks:** if the agent starts failing at 2am — provider outage, model down, database locked — **nobody knows**, and every caller hits a broken line. The metrics exist (`app/cli.py metrics`) but nothing watches them.

**What's needed:** alert on abandon-rate spikes, containment-rate drops, transfer-rate spikes, and failed bookings; health checks on the STT/TTS/LLM providers; and a fallback that forwards to voicemail or a human line when the agent is unhealthy. **A broken voice agent must fail toward a human, not toward silence.**

### 2.9 Single-process SQLite

**What breaks:** one writer. Real concurrent calls contend, and multi-worker deployment isn't possible.

**Partially mitigated:** WAL + `busy_timeout` are configured, and the 8-way concurrency test passes — so contention degrades gracefully rather than erroring.

**What's needed:** Postgres for concurrent writers. Same deferred bottleneck as the other portfolio projects, same trigger: measured contention, not speculation.

### 2.10 Rule-based NLU will mis-parse real speech

**What breaks:** `app/nlu.py` is keyword rules. Real callers ramble, self-correct, use regional phrasing, and say things no pattern anticipates. **Four of the six bugs in §1 were NLU gaps** — that's the honest cost of this choice, and real traffic will find more.

**Why it's still right for now:** doc §6.1 budgets the whole turn under ~800ms and an LLM round trip spends most of that before the caller hears anything; and when the agent mishears, you want to read the rule that fired. Ambiguity routes to a clarifying question or the escape hatch rather than a guess, so the failure mode is *over-cautious*, not *silently wrong*.

**What's needed at scale:** a small fast LLM for intent classification only, with the safety-critical paths (emergency detection, the booking gate) kept as deterministic rules. Never let intent classification become the thing that decides whether a booking is confirmed.

**Trigger:** measured mis-parse rate on real call transcripts — which requires §2.1 first.

### 2.11 Latency is unmeasured where it matters

**What breaks:** the 18ms average in the README is **agent decision time only**. A real turn adds endpointing (~300–700ms), STT finalisation, TTS time-to-first-byte, and network. The real number is unknown and will be 50–100× larger.

**What's needed:** end-to-end instrumentation from caller-stopped-speaking to first-audio-out, per hop. The doc's point stands: <800ms is an architecture decision, not something tuned into existence afterwards.

### 2.12 Legal review not done

**What breaks:** call-recording consent (all-party-consent states in the US, GDPR in the EU) and AI-disclosure requirements (a growing set of jurisdictions; EU AI Act Article 50 carries explicit transparency obligations).

**Mitigated structurally:** disclosure is in the opening turn, config-driven, and recorded per call on the `calls` row so it's auditable.

**Still needed:** actual review by the client's counsel for their jurisdictions. This section is engineering guidance, not legal advice.

---

## 3. Honest summary for a client conversation

**What this demo proves:** the reasoning and the safety properties. It cannot double-book (verified with 8 concurrent callers racing one slot), cannot confirm a booking that doesn't exist (verified on every failure path), and cannot strand a caller (0% abandon across 16 replayed calls). Those are the properties that make a voice agent trustworthy, and they're the expensive ones to get right.

**What it is not:** a phone system. Everything in §2 stands between this and a number a business can advertise — most of it purchased and configured rather than invented, but real work with real cost.

**The honest framing:** the risky part is built and tested; the remaining work is integration with paid infrastructure, and it should be quoted as such.

| | Count |
|---|---|
| Demo-visible defects found and fixed | 7 |
| Production gaps documented, not implemented | 12 |
| Of those, needing paid services | 6 (§2.1, 2.2, 2.3, 2.5, 2.6, 2.8) |
| Of those, needing engineering only | 6 (§2.4, 2.7, 2.9, 2.10, 2.11, 2.12) |
