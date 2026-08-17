# Autonomous Voice Receptionist

Category: **The 24/7 Frontline — AI Voice & Triage Agents**

An AI agent that answers inbound business calls 24/7, handles FAQs, books/reschedules/cancels appointments, routes urgent calls to a human, and logs every call as structured data.

---

## 1. What It Does

- Answers every inbound call instantly, no hold music, no missed calls after hours
- Understands natural speech, holds a real conversation (not menu/IVR trees)
- Checks calendar availability and books/moves/cancels appointments live
- Answers FAQs from a knowledge base (hours, pricing, location, policies)
- Detects urgency/keywords and transfers or texts a human when needed
- Sends a call summary + transcript to the business owner after every call
- Logs every call into a CRM/sheet for follow-up and analytics

---

## 2. Architecture

```
Caller ──▶ Telephony (Twilio number)
              │  (inbound webhook)
              ▼
        Voice Orchestration Layer
        (streams audio both ways)
              │
   ┌──────────┼───────────────┐
   ▼          ▼               ▼
  STT      LLM Agent        TTS
 (speech   (dialogue +      (text
  → text)   function        → speech)
            calling)
              │
   ┌──────────┼───────────────┐
   ▼          ▼               ▼
Calendar   Knowledge Base   CRM / Sheet
 API        (RAG lookup)    (call log)
              │
              ▼
     Human handoff (SMS/transfer)
```

The LLM agent is the brain: it holds conversation state, decides when to call a tool (check calendar, look up FAQ, transfer call), and generates the next reply — which gets converted back to speech and streamed to the caller in near-real-time (target: <800ms round trip).

---

## 3. Core Components

| Component | Role |
|---|---|
| Telephony | Receives/places calls, streams audio, handles transfers |
| STT (Speech-to-Text) | Converts caller audio to text, streaming, low latency |
| LLM Orchestrator | Conversation logic, tool/function calling, persona + guardrails |
| TTS (Text-to-Speech) | Converts agent replies to natural voice audio |
| Calendar Tool | Reads/writes availability (Google Calendar / Cal.com) |
| Knowledge Base | FAQ/policy answers via RAG over business docs |
| CRM / Logging | Stores transcript, caller info, outcome, tags |
| Escalation | SMS/call transfer to a human on trigger keywords or failure |

---

## 4. Tech Stack

### Phase 1 — Free / near-free (prototype, low call volume)

> ⚠️ **Learning use only — see §6.3.** Edge-TTS is an unofficial endpoint with no commercial licensing, local Whisper isn't streaming-native, and free-tier LLM rate limits cause dead air on concurrent calls. Do not ship this table to a paying client; go straight to the Phase 2 stack, which lands around $0.08–0.20/min all-in.

| Layer | Tool | Notes |
|---|---|---|
| Telephony | Twilio (trial credit) or Twilio pay-as-you-go | ~$1/mo per number + per-minute usage |
| Voice orchestration | Custom via Twilio Media Streams + WebSocket server | No platform fee, more build effort |
| STT | Deepgram free tier / `faster-whisper` self-hosted | Deepgram free tier ~$200 credit; Whisper local = $0 but needs a GPU/CPU box |
| LLM | Groq free tier (Llama 3.1) or Gemini Flash free tier | Fast + cheap enough for real-time dialogue |
| TTS | Edge-TTS (free, MS voices) or Coqui TTS (local) | Quality lower than commercial TTS |
| Calendar | Google Calendar API (free) | Direct booking |
| Knowledge base | Local embeddings (`sentence-transformers`) + SQLite/Chroma | RAG over a small FAQ doc |
| Orchestration/logging | n8n (self-hosted, free) + Google Sheets | Webhook glue + call log |

### Phase 2 — Paid / cheap-at-scale (production)

| Layer | Tool | Why |
|---|---|---|
| All-in-one voice agent platform | Vapi, Retell AI, or Bland AI | Handles telephony+STT+TTS+turn-taking orchestration; usage-based pricing (~$0.05–0.15/min) instead of building the real-time pipeline yourself |
| Telephony | Twilio (production number, SIP trunking if scaling) | |
| STT | Deepgram Nova-2 | Best latency/accuracy for phone audio |
| LLM | GPT-4o-mini or Claude Haiku | Cheap, fast, good enough for structured dialogue |
| TTS | ElevenLabs or PlayHT | Natural voice, low latency streaming |
| Calendar | Cal.com API or Google Calendar | Cal.com gives scheduling logic for free |
| Knowledge base | Pinecone/Qdrant Cloud + OpenAI embeddings | Scales past a few hundred docs |
| CRM/logging | Airtable, HubSpot, or Postgres + a small dashboard | Structured call records, tagging, follow-up |
| Monitoring | Twilio + platform dashboards, or a custom Grafana panel | Call success rate, avg handle time, transfer rate |

**Cost reality check:** the all-in-one platforms (Vapi/Retell) usually beat DIY once you account for engineering time on turn-taking, interruption handling, and latency — build DIY only as a learning/demo project or for very high volume where per-minute fees matter.

---

## 5. Build Sequence

1. **Define the call flows** — list every intent the receptionist must handle (booking, reschedule, FAQ, hours, transfer) as a flowchart before writing code.
2. **Stand up the telephony number** — Twilio number, forward existing business line or use as the new line.
3. **Prototype the pipeline with a platform** (Vapi/Retell free trial) to validate the conversation design fast, before committing to a DIY build.
4. **Write the system prompt + tool schemas** — booking tool, FAQ-lookup tool, transfer tool, each with strict input/output shapes.
5. **Connect calendar + knowledge base** — read/write test bookings, load FAQ docs into the vector store.
6. **Add escalation logic** — keyword/sentiment triggers ("emergency", repeated confusion) that transfer to a human or send an SMS alert.
7. **Test with real call scenarios** — happy path, interruptions, background noise, caller changes mind mid-booking.
8. **Wire logging** — every call writes transcript + summary + outcome to the CRM/sheet automatically.
9. **Add a daily/weekly digest** — email or Slack summary of call volume, bookings made, missed intents.
10. **Go live on a low-stakes line first**, monitor transfer rate and failed-intent rate, iterate the prompt before full rollout.

---

## 6. Reality Check — Why the Naive Build Fails, and the Fix

### 6.1 The DIY pipeline can't hit conversational latency
**Failure:** Twilio Media Streams → STT → LLM → TTS, wired by hand, realistically lands at **1.5–3s** per turn, not the <800ms in §2. Each hop adds: endpointing delay (~300–700ms waiting to be sure the caller stopped), STT finalization, LLM time-to-first-token, TTS time-to-first-byte, network. Callers interpret >1.2s of silence as a dropped call and start talking over the agent.

**Fix:** Latency is an architecture decision, not a tuning pass. Budget it per hop and design for streaming end to end — stream partial STT into the LLM, stream LLM tokens into TTS, stream TTS audio out before the sentence is finished. Do not build the real-time transport layer yourself. Use **LiveKit Agents** or **Pipecat** (both open-source, free) if you want control, or **Vapi/Retell** if you want it solved. These exist because turn-taking is the hard part, and it is not a weekend of work.

### 6.2 Turn-taking and barge-in were missing entirely
**Failure:** §2 has no endpointing or interruption handling. Without them the agent talks over people, or waits awkwardly, or reads a 30-second answer while the caller says "no, wait —" and is ignored. This alone makes a demo unusable in production.

**Fix:** Add an explicit turn-taking layer: **VAD + semantic endpointing** (don't just detect silence — detect whether the utterance sounds *finished*, since people pause mid-sentence when reciting phone numbers or addresses), and **barge-in**, where incoming caller audio immediately kills TTS playback and truncates the agent's context to what was actually spoken aloud. That last detail matters: if you don't truncate, the agent believes it said things the caller never heard.

### 6.3 The Phase 1 stack is not deployable to a paying client
- **Edge-TTS is an unofficial wrapper** around Microsoft Edge's read-aloud endpoint. No SLA, no commercial licensing, and it can break without warning. Fine for a personal demo, indefensible in a client deliverable.
- **Whisper is not streaming-native.** `faster-whisper` needs VAD-chunked pseudo-streaming and needs a GPU to keep up with real-time. On CPU it will not.
- **Free-tier LLM APIs rate-limit per minute.** Three concurrent calls will hit the ceiling, and the failure mode is dead air on a customer call.

**Fix:** Treat Phase 1 as *learning only*, and go to paid immediately for anything client-facing. The economics justify it: **Deepgram streaming STT** (~$0.004/min), a small fast model, and **Cartesia or ElevenLabs Flash** for TTS (sub-200ms first byte) puts you around **$0.08–0.20/min all-in**. That is cheaper than any human receptionist per hour and removes every item above.

### 6.4 Phone audio breaks models tuned on clean audio
**Failure:** Telephony is **8kHz μ-law narrowband**, often with codec artifacts and background noise. STT models benchmarked on clean 16kHz podcast audio degrade sharply on it, especially for names, addresses, spelled-out emails, and accented speech — exactly the data a receptionist must capture perfectly.

**Fix:** Use a telephony-tuned STT model (Deepgram's phonecall/Nova models). Add **keyword/phrase boosting** for the business's vocabulary (staff names, service names, local street names). For any high-stakes field — phone number, email, spelling of a name — **read it back and confirm**, and offer **DTMF keypad entry as a fallback** ("tap your number on the keypad"). Never let an unconfirmed transcription become a booking record.

### 6.5 The agent will confirm bookings that didn't happen
**Failure:** The LLM says "You're all set for Tuesday at 3" *before* or *regardless of* whether the calendar write succeeded. It's a language model — a plausible confirmation is the most likely next token whether or not the tool returned success. Two callers can also race for the same slot.

**Fix:** Make confirmation **structurally dependent on the tool result**, not on the model's discretion: the booking tool returns a confirmation ID, and the prompt forbids confirming without one. Add **idempotency keys** on the booking call and **slot locking** (reserve → confirm), so a concurrent caller can't double-book. Send an SMS/email confirmation from the *calendar system*, not from the agent — if the caller doesn't get the text, the booking didn't happen.

### 6.6 The escalation path dead-ends
**Failure:** §2's "human handoff" assumes a human exists. At 2am — the entire premise of a 24/7 receptionist — there is nobody to transfer to, and the caller hits a cold transfer into a ringing void.

**Fix:** Define the escalation ladder explicitly per time-of-day: business hours → warm transfer with a spoken context handoff; after hours → take a callback request with a stated response window, or route true emergencies to an on-call number. Add a **failure escape hatch**: after two consecutive misunderstood turns, stop trying and fall back to "let me take your details and have someone call you back." A graceful capture beats a clever agent looping.

### 6.7 Legal exposure was ignored
**Failure:** Two categories, both real: **call recording consent** (all-party-consent states in the US, GDPR in the EU) and **AI disclosure** — a growing set of jurisdictions now require telling people they're talking to a bot, and the EU AI Act carries explicit transparency obligations for this.

**Fix:** Bake it in rather than bolting it on: an opening line that discloses the agent is an AI assistant and that the call may be recorded, plus a documented retention policy for recordings and transcripts. Confirm the specifics with the client's counsel per jurisdiction — but architect for it now, since retrofitting consent onto stored recordings is not possible.

### 6.8 Success is unmeasured
**Failure:** §5 says "iterate the prompt," but there's no definition of working. Prompt changes then get judged on vibes and silently regress.

**Fix:** Build a **replay eval set** — 30–50 recorded real calls (with consent), replayed against the agent after every prompt change, scored on task completion, correct data captured, and unnecessary transfers. Track in production: containment rate (calls resolved without a human), booking accuracy, average handle time, and abandon rate. Abandon rate is the one that matters most — it's the caller hanging up on your agent.
