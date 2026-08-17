# Autonomous Voice Receptionist

Answers inbound calls, books/cancels appointments against a real calendar, answers FAQs, and escalates to a human — with the safety properties that make a voice agent trustworthy rather than merely impressive: **it cannot double-book, cannot confirm a booking that doesn't exist, and cannot strand a caller in a loop.**

Full architecture rationale in [`01_Voice_Receptionist.md`](01_Voice_Receptionist.md). Self-contained; shares no code with the other portfolio projects.

---

## Honest scope — read this first

**What is built and tested:** the agent core. Dialogue state machine, calendar with atomic slot locking, booking-confirmation gate, FAQ, escalation ladder, call logging, and a replay eval harness. All of it runs and is verified locally with **120 assertions + 14 scripted call replays**.

**What is NOT built:** the telephony transport. Real phone calls need Twilio (per-minute cost, paid account) plus streaming speech-to-text and text-to-speech. I have no account, so rather than write an unverifiable adapter and imply it works, the transport boundary is a seam and the agent runs over a **console transport** that drives the identical `Receptionist` class a phone call would.

**Why that split is the right one, not a cop-out:** per the architecture doc's own §6.1, the transport layer is precisely the part you *shouldn't* hand-roll — turn-taking, endpointing, and barge-in are why LiveKit/Pipecat/Vapi exist. The part that carries the business risk, and the part a client is actually buying, is everything above it: does it double-book, does it lie about confirmations, does it strand people. That's what's tested here.

| Layer | Status |
|---|---|
| Dialogue / booking / escalation logic | **Built and tested** |
| Calendar with slot locking + idempotency | **Built and tested** (8-way concurrency race) |
| Booking confirmation gate | **Built and tested** (every failure path) |
| FAQ retrieval | **Built and tested** |
| Call logging + metrics | **Built and tested** |
| Console transport | **Built and tested** |
| Telephony (Twilio) | **Not built** — needs a paid account |
| Streaming STT / TTS | **Not built** — see §6.3 of the architecture doc on why the "free" options aren't deployable |
| Measured real-call latency | **Not measured** — the numbers below are agent decision time only |

---

## Status

| | |
|---|---|
| Test suite | **120 assertions + 14 replay scripts across 4 suites, all passing** |
| Double-booking | Impossible — verified with 8 concurrent callers racing one slot |
| Phantom confirmations | Impossible — the gate raises rather than lie, verified on every failure path |
| Abandon rate (replay set) | **0%** — no scripted call ends with the caller stranded |
| Containment rate (replay set) | 71% — the rest are intentional transfers (emergency / human request) |
| Agent decision latency | 18ms average, 31ms worst — **excludes STT/TTS/network** |

---

## The three safety properties

### 1. It cannot double-book (`app/calendar_tool.py`)

A voice agent talks to several people at once, so "check availability, then write the booking" is a read-then-write race: both callers hear "3pm is free", both get told they're booked, one shows up to nothing.

Fixed with a **two-phase reserve → confirm**, where the reserve is an atomic conditional claim inside a `BEGIN IMMEDIATE` transaction, plus a **partial unique index** (`WHERE status = 'confirmed'`) so even a logic bug upstream cannot produce two confirmed bookings for one slot.

Verified: 8 concurrent callers, one slot → **exactly one booking**, every loser told a specific reason, and a direct `INSERT` bypassing the tool entirely is still rejected by the database.

### 2. It cannot confirm a booking that doesn't exist (`app/agent.py`)

The architecture doc's §6.5: *"a plausible confirmation is the most likely next token whether or not the tool returned success."*

`_say_booked()` is the **only** function that produces a booking-confirmed utterance, and it **raises** if handed a failed result or an empty confirmation code. A crash in development is strictly better than a caller who thinks they have an appointment.

Verified against: expired holds, slots stolen mid-call, incomplete details, and unconfirmed phone numbers. In every case the agent tells the truth and re-offers — and a scan across full call transcripts confirms no reply ever *sounds* like a confirmation without the flag set.

### 3. It cannot strand a caller (`app/agent.py`)

- **Time-of-day escalation.** In hours → warm transfer. Out of hours → callback, because the entire premise of a 24/7 receptionist is that there often *isn't* a human, and transferring at 2am rings a phone nobody answers.
- **Emergency override.** Distress routes to on-call immediately and never enters a booking flow. With no on-call configured out of hours, the caller is pointed at emergency services rather than offered an appointment.
- **Confusion escape hatch.** After N consecutive misunderstood turns the agent stops trying to be clever and captures a callback. If even that fails, it ends honestly and records the outcome as `failed` rather than pretending the call was handled.

---

## Bugs the test suites caught

Kept here because it's the argument for building the eval harness at all — **five of these came from the replay harness, not the unit tests.**

1. **Bare `"1"` wasn't recognised as a slot choice** — only `"first"` / `"option 1"`. The agent explicitly offers *"press it on your keypad"*, and DTMF produces exactly a bare digit, so the fallback it offered didn't work. A caller answering "1" fell through to the escape hatch instead of booking.
2. **"What are your opening hours?" was classified as a booking request** — `opening` was a booking keyword (as in "an available opening"), checked before FAQ. The FAQ had a 0.84-scoring answer that never got a chance; the caller got offered appointment times instead.
3. **The confusion counter wasn't reset when entering the escape hatch** — so callback capture inherited the confusions that triggered it, started already at its limit, and gave up on the caller's *first* attempt to say their number. Turned recoverable calls into `failed`.
4. **A successful cancellation was recorded as `faq_answered`** — `ctx.state` was left as `CANCELLED` while the reply reported `CLOSING`, so the next turn fell through to a handler whose goodbye branch hardcoded the outcome. Silently corrupted the metrics the eval depends on.
5. **"Great, can I book an appointment then" was read as a bare "yes"** — a leading pleasantry matched an affirm pattern, discarding the actual request. Root cause: open questions ("anything else?") and closed yes/no questions were given the same intent precedence, when they need opposite precedence.
6. **Turns arriving after a call ended restarted the conversation**, overwriting the recorded outcome. Found while fixing a test that wrongly kept feeding turns past `end_call`.

---

## Setup

```bash
cd 01-voice-receptionist
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

python -m app.cli setup         # generate slots + load the FAQ
python run_all_tests.py         # all 4 suites must pass
```

No API keys, no telephony account, no network. The FAQ uses a local embedding model (~80MB, downloaded once).

---

## Try it

```bash
python -m app.cli reset         # clean slate (use between demo takes)
python -m app.cli call          # interactive simulated call
python -m app.cli call --at 02:00   # simulate an AFTER-HOURS call
python -m app.cli call --debug      # show state + latency per turn

python -m app.cli bookings      # upcoming appointments
python -m app.cli calls         # call log with disclosure audit
python -m app.cli metrics       # containment / abandon rates
```

### Demo script — the five moments worth showing

Run `python -m app.cli reset` first so the diary is clean.

**1. A booking that works.** Shows read-back and a real confirmation code.
```
I'd like to book an appointment
the first one
my name is Sarah Chen
555 123 4567
yes that's right
no that's all
```

**2. It actually listens to what you asked for.** Ask for a specific day.
```
can I book something on Thursday afternoon
```
→ offers *Thursday afternoon* slots. Ask for a day with nothing free and it says so rather than quietly offering something else.

**3. Declining shows real alternatives.** Say `none of those work` three times — each set is different, not the same three looped.

**4. Emergencies never enter a booking flow.**
```
I have severe pain and swelling, this is an emergency
```
→ straight to on-call, never mentions appointments.

**5. After hours it doesn't transfer into the void.** `python -m app.cli call --at 02:00`, then `can I speak to someone` → takes a callback, because at 2am there's nobody to transfer to.

**Bonus, the strongest technical moment:** `python run_all_tests.py` and point at the concurrency test — 8 callers racing one slot, exactly one wins, and the database rejects a double-booking even when the application logic is bypassed entirely.

---

## Production gaps

A deliberately adversarial review of what breaks with real callers is in **[`PRODUCTION_GAPS.md`](PRODUCTION_GAPS.md)** — 7 demo-visible defects found and fixed, and 12 production failures documented rather than faked (telephony, endpointing/barge-in, timezones, SMS, calendar integration, monitoring, retention).

Worth reading one entry in particular: the eval script for "caller declines offered times" **passed while the decline path was broken**, because it asserted only the final outcome. A correct ending concealed a broken middle. The harness now asserts on the middle of a call too.

---

## Testing

```bash
python run_all_tests.py
```

| Suite | Covers | Assertions |
|---|---|---:|
| `eval/test_calendar_safety.py` | Concurrency race, idempotent retries, hold expiry, phone-friendly codes | 36 |
| `eval/test_booking_gate.py` | Every path where a phantom confirmation could occur | 28 |
| `eval/test_safety_paths.py` | Disclosure, escalation ladder, escape hatch, hang-up cleanup | 42 |
| `eval/run_eval.py` | 14 scripted call replays + production metrics | 14 scripts |

The replay harness is the one that pays for itself. Scripts assert **outcome and captured data**, not exact wording — so copy changes don't break it, but a broken booking flow does.

---

## Known limitations

- **Rule-based NLU** (`app/nlu.py`), not an LLM. Deliberate: doc §6.1 budgets the whole turn under ~800ms and an LLM round trip spends most of that before the caller hears anything; and when the agent mishears, you want to read the rule that fired. The tradeoff is real — unusual phrasing is weaker. Mitigated by routing ambiguity to a clarifying question or the escape hatch rather than a guess. Four of the six bugs above were NLU gaps, which is the honest cost of this choice.
- **Latency numbers exclude the expensive parts.** 18ms average is agent decision time. A real call adds endpointing (~300–700ms), STT finalisation, TTS time-to-first-byte, and network — which is exactly why doc §6.1 says <800ms is an architecture decision, not a tuning pass.
- **No barge-in or endpointing.** Both belong to the transport layer that isn't built. They are the hardest part of a voice agent and the reason to use LiveKit/Pipecat/Vapi rather than hand-roll.
- **Single-process SQLite.** Same deferred bottleneck as the other projects: fine for one instance, needs Postgres for concurrent workers.
- **No SMS confirmation.** No provider is wired, so `SMS_CONFIRMATIONS_ENABLED` defaults to `false` and the agent **does not promise a text it can't send**. Set it to `true` only once a provider is connected — the config gate exists so the promise and the capability can't drift apart.
- **FAQ threshold (0.50) calibrated on 12 entries.** Recalibrate against a client's real question log.

## Production path

| This build | Production | When |
|---|---|---|
| Console transport | **Vapi / Retell / LiveKit Agents** + Twilio number | Real calls. Doc §6.1: don't hand-roll the media pipeline |
| — | Deepgram (phonecall model) + Cartesia/ElevenLabs Flash | ~$0.08–0.20/min all-in; the free options in doc §6.3 aren't commercially deployable |
| Rule-based NLU | Small fast LLM for intent only, rules kept for safety-critical paths | Phrasing variety becomes the measured failure mode |
| Local SQLite calendar | Google Calendar / Cal.com API | Client already has a calendar (the usual case) |
| No SMS (promise gated off) | Twilio SMS + `SMS_CONFIRMATIONS_ENABLED=true` | Callers expect a written confirmation |
| Local FAQ file | Client's real KB, synced | Deployment |
