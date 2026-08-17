"""
The receptionist agent: turn handling, state transitions, and the safety gates.

Three invariants are enforced HERE rather than left to prompting, because each
one is a failure the architecture doc identifies as expensive and silent:

  §6.5  BOOKING GATE. `_say_booked()` is the only function that produces a
        booking-confirmed utterance, and it refuses to run without a non-empty
        confirmation code from calendar_tool. There is no code path where the
        agent can tell a caller they're booked based on how the conversation
        felt.

  §6.6  NO DEAD ENDS. Escalation is time-of-day aware — during hours it
        transfers, outside hours it takes a callback rather than transferring
        into a phone nobody answers. And after N consecutive misunderstood
        turns the agent stops trying and captures a callback.

  §6.7  DISCLOSURE. The opening turn discloses AI and (if enabled) recording,
        and the flags are recorded on the call row. This cannot be retrofitted
        onto recordings already taken, so it's structural.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import calendar_tool, config, db, faq
from .dialogue import ConversationContext, State
from .nlu import Intent, understand

logger = logging.getLogger(__name__)


@dataclass
class AgentReply:
    text: str
    state: State
    end_call: bool = False
    transfer_to: str = ""
    latency_ms: float = 0.0
    # Set True only by _say_booked(). The transport/UI can trust this flag to
    # mean a booking genuinely exists.
    booking_confirmed: bool = False
    confirmation_code: str = ""
    debug: dict = field(default_factory=dict)


class BookingGateViolation(RuntimeError):
    """Raised if code attempts a booking-confirmed utterance without a real
    confirmation code. This is a programming error, not a runtime condition —
    it means someone added a path around the gate."""


def is_within_business_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if now.weekday() not in config.BUSINESS_DAYS:
        return False
    return config.BUSINESS_HOURS_START <= now.hour < config.BUSINESS_HOURS_END


class Receptionist:
    def __init__(self, call_sid: Optional[str] = None, caller_number: str = "", now: Optional[datetime] = None):
        self.call_sid = call_sid or f"local-{uuid.uuid4().hex[:12]}"
        self.caller_number = caller_number
        self._now_override = now
        self.ctx = ConversationContext(call_sid=self.call_sid)
        self.call_id: Optional[int] = None
        self._ensure_call_row()

    def _now(self) -> datetime:
        return self._now_override or datetime.now()

    def _ensure_call_row(self) -> None:
        with db.session() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO calls (call_sid, caller_number) VALUES (?, ?)",
                (self.call_sid, self.caller_number),
            )
            if cur.lastrowid:
                self.call_id = cur.lastrowid
            else:
                row = conn.execute("SELECT id FROM calls WHERE call_sid = ?", (self.call_sid,)).fetchone()
                self.call_id = row["id"] if row else None

    # --- persistence helpers ---------------------------------------------

    def _record_turn(self, speaker: str, text: str, latency_ms: float = 0.0) -> None:
        with db.session() as conn:
            conn.execute(
                """INSERT INTO turns (call_id, turn_index, speaker, text, state, latency_ms)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (self.call_id, self.ctx.turn_count, speaker, text, self.ctx.state.value, latency_ms),
            )
            conn.execute(
                "UPDATE calls SET turns = ?, confusion_count = ? WHERE id = ?",
                (self.ctx.turn_count, self.ctx.consecutive_confusions, self.call_id),
            )

    def _finish_call(self, outcome: str, summary: str = "") -> None:
        self.ctx.outcome = outcome
        with db.session() as conn:
            conn.execute(
                """UPDATE calls
                   SET ended_at = datetime('now'), outcome = ?, summary = ?,
                       disclosed_ai = ?, disclosed_recording = ?, transferred = ?
                   WHERE id = ?""",
                (
                    outcome, summary,
                    int(self.ctx.disclosed_ai), int(self.ctx.disclosed_recording),
                    int(self.ctx.state == State.ESCALATING),
                    self.call_id,
                ),
            )
            db.log_event(conn, "call_ended", {"outcome": outcome, "turns": self.ctx.turn_count}, call_sid=self.call_sid)

    # --- THE BOOKING GATE (doc §6.5) -------------------------------------

    def _say_booked(self, result: calendar_tool.BookingResult) -> AgentReply:
        """The ONLY function permitted to tell a caller they are booked.

        It validates the calendar result rather than trusting the caller of this
        function. If a future change routes here without a real confirmation
        code, this raises instead of producing a false confirmation — a crash in
        development is strictly better than a caller who thinks they have an
        appointment and doesn't.
        """
        if not result.success or not result.confirmation_code:
            raise BookingGateViolation(
                f"_say_booked called without a confirmed booking "
                f"(success={result.success}, code={result.confirmation_code!r}, reason={result.reason!r})"
            )

        self.ctx.draft.confirmation_code = result.confirmation_code
        self.ctx.state = State.BOOKED
        spoken_code = " ".join(result.confirmation_code)
        when = result.slot.spoken() if result.slot else "the time we discussed"
        parts = [
            f"You're booked for {when}.",
            f"Your confirmation code is {spoken_code}.",
        ]
        # Only promise an SMS if one can actually be sent. Telling a caller
        # "we'll text you" when no SMS provider is configured is a small lie
        # that produces a real support call when the text never arrives — the
        # same class of problem as confirming a booking that didn't happen,
        # just cheaper. Gate the claim on the capability existing.
        if config.SMS_CONFIRMATIONS_ENABLED:
            parts.append(f"We'll also text that to {self._spoken_phone(self.ctx.draft.phone)}.")
        parts.append("Anything else I can help with?")
        text = " ".join(parts)
        self.ctx.state = State.CLOSING
        return AgentReply(
            text=text,
            state=State.CLOSING,
            booking_confirmed=True,
            confirmation_code=result.confirmation_code,
            debug={"gate": "passed", "slot_id": result.slot.id if result.slot else None},
        )

    @staticmethod
    def _spoken_phone(phone: str) -> str:
        """Digits read individually — on an 8kHz line (doc §6.4) "five five five"
        survives where "555" can be misheard."""
        return " ".join(phone) if phone else ""

    # --- escalation (doc §6.6) ------------------------------------------

    def _escalate(self, reason: str, emergency: bool = False) -> AgentReply:
        """Time-of-day aware escalation.

        The dead-end this avoids: transferring at 2am to a phone nobody answers.
        The whole premise of a 24/7 receptionist is that there often ISN'T a
        human, so 'transfer to a human' cannot be the only escalation path.
        """
        self.ctx.escalation_reason = reason
        in_hours = is_within_business_hours(self._now())

        if emergency and config.EMERGENCY_ONCALL_NUMBER:
            self.ctx.state = State.ESCALATING
            self._finish_call("transferred", f"emergency: {reason}")
            return AgentReply(
                text="This sounds urgent — I'm putting you through to our on-call line right now. Please stay on the line.",
                state=State.ESCALATING,
                transfer_to=config.EMERGENCY_ONCALL_NUMBER,
                end_call=True,
                debug={"escalation": "emergency_oncall", "in_hours": in_hours},
            )

        if in_hours and config.HUMAN_TRANSFER_NUMBER:
            self.ctx.state = State.ESCALATING
            self._finish_call("transferred", reason)
            return AgentReply(
                text="Of course — let me put you through to a colleague now. One moment.",
                state=State.ESCALATING,
                transfer_to=config.HUMAN_TRANSFER_NUMBER,
                end_call=True,
                debug={"escalation": "warm_transfer", "in_hours": True},
            )

        # Out of hours, or no transfer number configured: take a callback.
        self.ctx.state = State.TAKING_CALLBACK
        # Fresh confusion allowance for the new question (see _escape_hatch).
        self.ctx.clear_confusion()
        closing = "We're closed at the moment"
        if emergency:
            return AgentReply(
                text=(
                    f"{closing}, and this sounds urgent. If this is a medical emergency please hang up "
                    f"and call your local emergency number. Otherwise, let me take your number and "
                    f"someone will call you back as a priority. What's the best number for you?"
                ),
                state=State.TAKING_CALLBACK,
                debug={"escalation": "after_hours_urgent_callback", "in_hours": False},
            )
        return AgentReply(
            text=(
                f"{closing}, so I can't put you through to anyone right now. "
                f"I can take your name and number and have someone call you back "
                f"when we open. What's the best number for you?"
            ),
            state=State.TAKING_CALLBACK,
            debug={"escalation": "after_hours_callback", "in_hours": False},
        )

    def _capture_callback(self, reason: str, urgency: str = "normal") -> AgentReply:
        with db.session() as conn:
            conn.execute(
                """INSERT INTO callbacks (call_sid, customer_name, customer_phone, reason, urgency)
                   VALUES (?, ?, ?, ?, ?)""",
                (self.call_sid, self.ctx.draft.name, self.ctx.draft.phone, reason, urgency),
            )
            db.log_event(conn, "callback_captured", {"reason": reason, "urgency": urgency}, call_sid=self.call_sid)
        self.ctx.state = State.CALLBACK_TAKEN
        self._finish_call("callback_requested", reason)
        when = "first thing when we open" if not is_within_business_hours(self._now()) else "shortly"
        return AgentReply(
            text=(
                f"Thank you. I've got {self.ctx.draft.name or 'your details'} on "
                f"{self._spoken_phone(self.ctx.draft.phone)}, and someone will call you back {when}. "
                f"Sorry I couldn't sort it out directly. Goodbye."
            ),
            state=State.CALLBACK_TAKEN,
            end_call=True,
            debug={"callback": True, "urgency": urgency},
        )

    def _escape_hatch(self) -> AgentReply:
        """Doc §6.6: after repeated misunderstanding, stop being clever.

        The failure this replaces is the agent cheerfully re-prompting forever
        while the caller gets angrier. Capturing a callback is a worse outcome
        than solving it and a much better outcome than a loop.
        """
        self.ctx.state = State.TAKING_CALLBACK
        # Reset the counter on entry. Caught by the replay eval: without this,
        # the callback capture INHERITS the confusions that triggered the escape
        # hatch, so it is already at its limit and gives up on the caller's very
        # first attempt to say their number — turning a recoverable call into a
        # "failed" outcome. The caller deserves a fresh allowance for the new,
        # much simpler question we're now asking.
        self.ctx.clear_confusion()
        if self.ctx.draft.phone:
            return self._capture_callback(
                reason=self.ctx.unanswered_question or "agent could not understand caller",
                urgency="normal",
            )
        return AgentReply(
            text=(
                "I'm sorry — I'm having trouble understanding, and I don't want to waste your time. "
                "Let me take a number and have a colleague call you back. What's the best number for you?"
            ),
            state=State.TAKING_CALLBACK,
            debug={"escape_hatch": True, "confusions": self.ctx.consecutive_confusions},
        )

    # --- opening turn (doc §6.7) ----------------------------------------

    def greeting(self) -> AgentReply:
        """Opening line. Disclosure is assembled from config rather than being a
        hardcoded string a future edit could quietly drop."""
        started = time.perf_counter()
        parts = [f"Thanks for calling {config.BUSINESS_NAME}."]

        if config.DISCLOSE_AI:
            parts.append("You're speaking with an automated assistant.")
            self.ctx.disclosed_ai = True
        if config.DISCLOSE_RECORDING and config.RECORDING_ENABLED:
            parts.append("This call may be recorded.")
            self.ctx.disclosed_recording = True

        parts.append("How can I help you today?")
        self.ctx.state = State.LISTENING

        with db.session() as conn:
            conn.execute(
                "UPDATE calls SET disclosed_ai = ?, disclosed_recording = ? WHERE id = ?",
                (int(self.ctx.disclosed_ai), int(self.ctx.disclosed_recording), self.call_id),
            )
            db.log_event(
                conn, "greeting",
                {"disclosed_ai": self.ctx.disclosed_ai, "disclosed_recording": self.ctx.disclosed_recording},
                call_sid=self.call_sid,
            )

        text = " ".join(parts)
        latency = (time.perf_counter() - started) * 1000
        self._record_turn("agent", text, latency)
        return AgentReply(text=text, state=self.ctx.state, latency_ms=latency)

    # --- main turn handler ----------------------------------------------

    def handle(self, caller_text: str) -> AgentReply:
        started = time.perf_counter()

        # Guard against turns arriving after the call concluded. A real
        # telephony transport disconnects, so this shouldn't happen — but if a
        # transport bug kept feeding audio, the agent would restart the
        # conversation and overwrite the recorded outcome. Found while fixing a
        # test that (wrongly) kept calling handle() past end_call and saw the
        # agent begin a fresh callback capture on an already-failed call.
        if self.ctx.state == State.ENDED:
            return AgentReply(
                text="",
                state=State.ENDED,
                end_call=True,
                debug={"ignored": "call already ended"},
            )

        self.ctx.turn_count += 1
        self._record_turn("caller", caller_text)

        if self.ctx.turn_count > config.MAX_TURNS:
            reply = self._capture_callback("call exceeded maximum turns")
            reply.latency_ms = (time.perf_counter() - started) * 1000
            self._record_turn("agent", reply.text, reply.latency_ms)
            return reply

        parsed = understand(caller_text, expecting=self.ctx.expecting())
        reply = self._route(parsed, caller_text)

        reply.latency_ms = (time.perf_counter() - started) * 1000
        reply.debug.setdefault("intent", parsed.intent.value)
        reply.debug.setdefault("state", reply.state.value)
        self._record_turn("agent", reply.text, reply.latency_ms)
        return reply

    def _route(self, parsed, caller_text: str) -> AgentReply:
        # Emergency and explicit human requests override whatever flow we're in.
        if parsed.intent == Intent.EMERGENCY:
            self.ctx.clear_confusion()
            return self._escalate("caller reported an emergency", emergency=True)
        if parsed.intent == Intent.HUMAN:
            self.ctx.clear_confusion()
            return self._escalate("caller asked for a human")

        handler = {
            State.GREETING: self._on_listening,
            State.LISTENING: self._on_listening,
            State.OFFERING_SLOTS: self._on_offering_slots,
            State.COLLECTING_NAME: self._on_collecting_name,
            State.COLLECTING_PHONE: self._on_collecting_phone,
            State.CONFIRMING_PHONE: self._on_confirming_phone,
            State.CONFIRMING_BOOKING: self._on_confirming_booking,
            State.COLLECTING_CODE: self._on_collecting_code,
            State.CONFIRMING_CANCEL: self._on_confirming_cancel,
            State.TAKING_CALLBACK: self._on_taking_callback,
            State.CLOSING: self._on_closing,
            State.BOOKED: self._on_closing,
        }.get(self.ctx.state, self._on_listening)

        return handler(parsed, caller_text)

    # --- state handlers -------------------------------------------------

    def _on_listening(self, parsed, caller_text: str) -> AgentReply:
        if parsed.intent == Intent.BOOK:
            self.ctx.clear_confusion()
            # Carry any day/time preference from the opening request, e.g.
            # "can I book something Thursday afternoon?"
            return self._offer_slots(hint=parsed.datetime_hint or "")
        if parsed.intent == Intent.CANCEL:
            self.ctx.clear_confusion()
            self.ctx.state = State.COLLECTING_CODE
            return AgentReply(
                text="I can help with that. Do you have your confirmation code? If not, I can look it up by phone number.",
                state=self.ctx.state,
            )
        if parsed.intent == Intent.RESCHEDULE:
            self.ctx.clear_confusion()
            self.ctx.state = State.COLLECTING_CODE
            return AgentReply(
                text="No problem. What's your confirmation code, or the phone number on the booking?",
                state=self.ctx.state,
            )
        if parsed.intent == Intent.FAQ:
            return self._answer_faq(caller_text)
        if parsed.intent == Intent.GOODBYE:
            self.ctx.state = State.ENDED
            # Preserve an outcome already established earlier in the call.
            # Caught by the replay eval: this branch used to hardcode
            # "faq_answered", so a call that successfully CANCELLED an
            # appointment and then said goodbye was recorded as an FAQ call —
            # silently corrupting the outcome metrics the whole eval depends on.
            outcome = self.ctx.outcome or (
                "booked" if self.ctx.draft.confirmation_code
                else ("faq_answered" if self.ctx.turn_count > 1 else "abandoned")
            )
            self._finish_call(outcome, "caller ended")
            return AgentReply(text="Thanks for calling. Goodbye.", state=State.ENDED, end_call=True)

        # Unrecognised — try the FAQ before admitting defeat, since many
        # questions don't match an intent pattern but do match an answer.
        match = faq.best_answer(caller_text)
        if match:
            return self._answer_faq(caller_text)

        self.ctx.register_confusion()
        self.ctx.unanswered_question = caller_text
        if self.ctx.should_escape(config.MAX_CONSECUTIVE_CONFUSIONS):
            return self._escape_hatch()
        return AgentReply(
            text=(
                "Sorry, I didn't catch that. I can book an appointment, change or cancel one, "
                "or answer questions about our hours and services. Which would you like?"
            ),
            state=self.ctx.state,
        )

    def _offer_slots(self, hint: str = "", advance: bool = False) -> AgentReply:
        """Offers up to 3 real slots, honouring a spoken day/time hint.

        `advance=True` pages FORWARD through the list — needed because the
        "none of those work" path previously re-offered the identical three
        times, so a caller declining twice heard the same options in a loop.
        """
        if advance:
            self.ctx.slot_offset += 3
        else:
            self.ctx.slot_offset = 0
        if hint:
            self.ctx.slot_hint = hint

        slots, hint_ok = calendar_tool.slots_matching_hint(
            self.ctx.slot_hint, limit=3, after=self._now(), offset=self.ctx.slot_offset
        )

        if not slots and self.ctx.slot_offset > 0:
            # We ran off the end of the list while paging. Start over rather
            # than dead-ending with "no appointments" when there clearly are some.
            self.ctx.slot_offset = 0
            slots, hint_ok = calendar_tool.slots_matching_hint(
                self.ctx.slot_hint, limit=3, after=self._now(), offset=0
            )
            if slots:
                options = "; ".join(f"{i + 1}, {s.spoken()}" for i, s in enumerate(slots))
                self.ctx.offered_slots = slots
                self.ctx.state = State.OFFERING_SLOTS
                return AgentReply(
                    text=(
                        f"That's everything I have in the diary. Going back to the earliest: "
                        f"{options}. Would any of those do, or shall I have someone call you?"
                    ),
                    state=self.ctx.state,
                    debug={"offered": [s.id for s in slots], "wrapped": True},
                )

        if not slots:
            self.ctx.state = State.TAKING_CALLBACK
            self.ctx.clear_confusion()
            return AgentReply(
                text=(
                    "I don't have any free appointments in the system right now. "
                    "Let me take your number and have someone call you back to sort a time. "
                    "What's the best number for you?"
                ),
                state=self.ctx.state,
                debug={"no_slots": True},
            )

        self.ctx.offered_slots = slots
        self.ctx.offer_history.append(list(slots))
        self.ctx.state = State.OFFERING_SLOTS
        options = "; ".join(f"{i + 1}, {s.spoken()}" for i, s in enumerate(slots))

        # Acknowledge a hint we couldn't satisfy rather than silently ignoring
        # it — being offered Monday after asking for Thursday reads as the agent
        # not listening, which is worse than an honest "I don't have that".
        prefix = f"I have {options}."
        if self.ctx.slot_hint and not hint_ok:
            prefix = (
                f"I don't have anything matching {self.ctx.slot_hint}, "
                f"but I do have {options}."
            )
        elif self.ctx.slot_offset > 0:
            prefix = f"No problem. I also have {options}."

        return AgentReply(
            text=f"{prefix} Which of those works for you?",
            state=self.ctx.state,
            debug={
                "offered": [s.id for s in slots],
                "hint": self.ctx.slot_hint,
                "hint_satisfied": hint_ok,
                "offset": self.ctx.slot_offset,
            },
        )

    def _on_offering_slots(self, parsed, caller_text: str) -> AgentReply:
        choice = parsed.slot_choice

        # A new day/time preference mid-selection ("actually, Thursday?") should
        # re-filter rather than be ignored.
        if parsed.datetime_hint and parsed.datetime_hint != self.ctx.slot_hint:
            self.ctx.clear_confusion()
            return self._offer_slots(hint=parsed.datetime_hint)

        if choice is None and parsed.intent == Intent.DENY:
            self.ctx.clear_confusion()
            # advance=True pages forward. Previously this recomputed a later
            # window, threw it away, and re-offered the same three slots.
            return self._offer_slots(advance=True)

        if choice is None:
            self.ctx.register_confusion()
            if self.ctx.should_escape(config.MAX_CONSECUTIVE_CONFUSIONS):
                return self._escape_hatch()
            options = "; ".join(f"{i + 1}, {s.spoken()}" for i, s in enumerate(self.ctx.offered_slots))
            return AgentReply(
                text=f"Sorry, which one — {options}? You can say the number, or press it on your keypad.",
                state=self.ctx.state,
                debug={"dtmf_offered": True},
            )

        self.ctx.clear_confusion()
        idx = len(self.ctx.offered_slots) - 1 if choice == -1 else choice - 1
        if idx < 0 or idx >= len(self.ctx.offered_slots):
            return AgentReply(
                text=f"I only have {len(self.ctx.offered_slots)} options. Which number would you like?",
                state=self.ctx.state,
            )

        slot = self.ctx.offered_slots[idx]
        # Take the hold NOW, before collecting details — otherwise another
        # caller can take the slot while this one spells their name.
        reserve = calendar_tool.reserve_slot(slot.id, self.call_sid)
        if not reserve.success:
            return AgentReply(
                text=f"{reserve.message} " + self._offer_slots().text,
                state=self.ctx.state,
                debug={"reserve_failed": reserve.reason},
            )

        self.ctx.draft.slot_id = slot.id
        self.ctx.draft.slot_spoken = slot.spoken()
        self.ctx.state = State.COLLECTING_NAME
        return AgentReply(
            text=f"Great, I'll hold {slot.spoken()} for you. Can I take your full name?",
            state=self.ctx.state,
            debug={"held_slot": slot.id},
        )

    def _on_collecting_name(self, parsed, caller_text: str) -> AgentReply:
        name = parsed.name
        if not name and parsed.intent == Intent.PROVIDE_INFO:
            name = " ".join(w.capitalize() for w in caller_text.split()[:3])
        if not name:
            self.ctx.register_confusion()
            if self.ctx.should_escape(config.MAX_CONSECUTIVE_CONFUSIONS):
                return self._escape_hatch()
            return AgentReply(text="Sorry, could I get your name again?", state=self.ctx.state)

        self.ctx.clear_confusion()
        self.ctx.draft.name = name
        self.ctx.state = State.COLLECTING_PHONE
        return AgentReply(
            text=f"Thanks {name.split()[0]}. And the best phone number to reach you on?",
            state=self.ctx.state,
        )

    def _on_collecting_phone(self, parsed, caller_text: str) -> AgentReply:
        phone = parsed.phone or (parsed.digits if parsed.digits and len(parsed.digits) >= 10 else None)
        if not phone:
            self.ctx.register_confusion()
            if self.ctx.should_escape(config.MAX_CONSECUTIVE_CONFUSIONS):
                return self._escape_hatch()
            # DTMF fallback (doc §6.4) — digits are where phone-audio STT is
            # weakest, so offer the keypad rather than asking a third time.
            self.ctx.dtmf_offered = True
            return AgentReply(
                text="Sorry, I didn't get that number. Could you say it again slowly, or type it on your keypad?",
                state=self.ctx.state,
                debug={"dtmf_offered": True},
            )

        self.ctx.clear_confusion()
        self.ctx.draft.phone = phone
        self.ctx.draft.phone_confirmed = False
        self.ctx.state = State.CONFIRMING_PHONE
        # Read-back (doc §6.4). An unconfirmed transcription must never become a
        # booking record — a wrong number means an unreachable customer.
        return AgentReply(
            text=f"Let me read that back: {self._spoken_phone(phone)}. Is that right?",
            state=self.ctx.state,
        )

    def _on_confirming_phone(self, parsed, caller_text: str) -> AgentReply:
        if parsed.intent == Intent.AFFIRM:
            self.ctx.clear_confusion()
            self.ctx.draft.phone_confirmed = True
            return self._attempt_booking()

        if parsed.intent == Intent.DENY or parsed.phone or parsed.digits:
            self.ctx.clear_confusion()
            if parsed.phone:
                self.ctx.draft.phone = parsed.phone
                return AgentReply(
                    text=f"Let me try again: {self._spoken_phone(parsed.phone)}. Is that right?",
                    state=State.CONFIRMING_PHONE,
                )
            self.ctx.draft.phone = ""
            self.ctx.state = State.COLLECTING_PHONE
            return AgentReply(text="Sorry about that. What's the correct number?", state=self.ctx.state)

        self.ctx.register_confusion()
        if self.ctx.should_escape(config.MAX_CONSECUTIVE_CONFUSIONS):
            return self._escape_hatch()
        return AgentReply(
            text=f"Sorry — is {self._spoken_phone(self.ctx.draft.phone)} correct? Please say yes or no.",
            state=self.ctx.state,
        )

    def _attempt_booking(self) -> AgentReply:
        """Calls the calendar and routes on the RESULT, never on optimism."""
        draft = self.ctx.draft
        if not draft.is_complete():
            missing = draft.missing()
            logger.warning("Booking attempted with missing fields: %s", missing)
            if "name" in missing:
                self.ctx.state = State.COLLECTING_NAME
                return AgentReply(text="Before I book that, can I take your name?", state=self.ctx.state)
            self.ctx.state = State.COLLECTING_PHONE
            return AgentReply(text="I still need a phone number for the booking.", state=self.ctx.state)

        result = calendar_tool.confirm_booking(
            slot_id=draft.slot_id,
            call_sid=self.call_sid,
            customer_name=draft.name,
            customer_phone=draft.phone,
            service=draft.service,
        )

        if result.success:
            return self._say_booked(result)

        # Booking FAILED. The caller must be told the truth and re-offered a
        # time. This branch is the one that, done wrong, produces a phantom
        # confirmation — so it deliberately cannot reach _say_booked().
        logger.info("Booking failed for call %s: %s", self.call_sid, result.reason)
        self.ctx.draft.slot_id = None
        if result.reason in {"hold_expired", "already_booked", "held_by_other", "race_lost"}:
            follow_up = self._offer_slots()
            return AgentReply(
                text=f"{result.message} {follow_up.text}",
                state=self.ctx.state,
                debug={"booking_failed": result.reason},
            )
        self.ctx.state = State.TAKING_CALLBACK
        return AgentReply(
            text=(
                f"{result.message} I'm having trouble completing that booking. "
                f"Let me have someone call you back to confirm — is "
                f"{self._spoken_phone(self.ctx.draft.phone)} the best number?"
            ),
            state=self.ctx.state,
            debug={"booking_failed": result.reason},
        )

    def _on_confirming_booking(self, parsed, caller_text: str) -> AgentReply:
        if parsed.intent == Intent.AFFIRM:
            return self._attempt_booking()
        if parsed.intent == Intent.DENY:
            self.ctx.clear_confusion()
            return self._offer_slots()
        self.ctx.register_confusion()
        if self.ctx.should_escape(config.MAX_CONSECUTIVE_CONFUSIONS):
            return self._escape_hatch()
        return AgentReply(text="Shall I go ahead and book that? Yes or no.", state=self.ctx.state)

    def _on_collecting_code(self, parsed, caller_text: str) -> AgentReply:
        booking = None
        if parsed.confirmation_code:
            booking = calendar_tool.find_booking(confirmation_code=parsed.confirmation_code)
        if booking is None and parsed.phone:
            booking = calendar_tool.find_booking(phone=parsed.phone)

        if booking is None:
            self.ctx.register_confusion()
            if self.ctx.should_escape(config.MAX_CONSECUTIVE_CONFUSIONS):
                return self._escape_hatch()
            return AgentReply(
                text="I couldn't find a booking with that. Could you give me the six-character code, or the phone number on the booking?",
                state=self.ctx.state,
            )

        self.ctx.clear_confusion()
        self.ctx.cancel_code = booking["confirmation_code"]
        when = calendar_tool.Slot(
            id=booking["slot_id"], starts_at=booking["starts_at"],
            duration_minutes=booking["duration_minutes"],
        ).spoken()
        self.ctx.state = State.CONFIRMING_CANCEL
        return AgentReply(
            text=f"I've found your appointment for {when}, under {booking['customer_name']}. Shall I cancel it?",
            state=self.ctx.state,
        )

    def _on_confirming_cancel(self, parsed, caller_text: str) -> AgentReply:
        if parsed.intent == Intent.AFFIRM:
            result = calendar_tool.cancel_booking(self.ctx.cancel_code, call_sid=self.call_sid)
            if result.success:
                self._finish_call("cancelled", f"cancelled {self.ctx.cancel_code}")
                # ctx.state must match the state reported on the reply. Setting
                # ctx.state = CANCELLED while returning CLOSING left the next
                # turn routing through an unmapped state and falling back to
                # _on_listening — which is how the cancelled outcome got
                # overwritten. Keep them in lockstep.
                self.ctx.state = State.CLOSING
                return AgentReply(
                    text="That's cancelled. Is there anything else I can help with?",
                    state=State.CLOSING,
                )
            return AgentReply(
                text=f"{result.message} Let me get someone to help — what's the best number for you?",
                state=State.TAKING_CALLBACK,
            )
        if parsed.intent == Intent.DENY:
            self.ctx.clear_confusion()
            self.ctx.state = State.CLOSING
            return AgentReply(
                text="No problem, I've left it as it is. Anything else?", state=self.ctx.state
            )
        self.ctx.register_confusion()
        if self.ctx.should_escape(config.MAX_CONSECUTIVE_CONFUSIONS):
            return self._escape_hatch()
        return AgentReply(text="Would you like me to cancel it? Yes or no.", state=self.ctx.state)

    def _on_taking_callback(self, parsed, caller_text: str) -> AgentReply:
        phone = parsed.phone or (parsed.digits if parsed.digits and len(parsed.digits) >= 10 else None)
        if phone:
            self.ctx.draft.phone = phone
        if parsed.name:
            self.ctx.draft.name = parsed.name

        if not self.ctx.draft.phone:
            self.ctx.register_confusion()
            if self.ctx.consecutive_confusions >= config.MAX_CONSECUTIVE_CONFUSIONS + 1:
                # Even the callback capture is failing. End honestly rather than
                # loop — the caller should go find another channel.
                self.ctx.state = State.ENDED
                self._finish_call("failed", "could not capture a callback number")
                return AgentReply(
                    text=(
                        "I'm sorry, I'm still not getting that. Please call back during business hours "
                        "and a colleague will help you directly. Goodbye."
                    ),
                    state=State.ENDED,
                    end_call=True,
                    debug={"capture_failed": True},
                )
            return AgentReply(
                text="Sorry, I didn't catch the number. Could you say it slowly, or type it on your keypad?",
                state=self.ctx.state,
                debug={"dtmf_offered": True},
            )

        urgency = "urgent" if self.ctx.escalation_reason.startswith("caller reported an emergency") else "normal"
        return self._capture_callback(
            reason=self.ctx.escalation_reason or self.ctx.unanswered_question or "callback requested",
            urgency=urgency,
        )

    def _answer_faq(self, caller_text: str) -> AgentReply:
        match = faq.best_answer(caller_text)
        if match is None:
            self.ctx.register_confusion()
            self.ctx.unanswered_question = caller_text
            if self.ctx.should_escape(config.MAX_CONSECUTIVE_CONFUSIONS):
                return self._escape_hatch()
            return AgentReply(
                text=(
                    "I don't have an answer for that one. I can book you in, or take your number "
                    "and have a colleague call you back with an answer. Which would you prefer?"
                ),
                state=self.ctx.state,
                debug={"faq_miss": True},
            )
        self.ctx.clear_confusion()
        self.ctx.state = State.CLOSING
        return AgentReply(
            text=f"{match.answer} Anything else I can help with?",
            state=self.ctx.state,
            debug={"faq_hit": match.entry_id, "score": round(match.score, 3)},
        )

    def _on_closing(self, parsed, caller_text: str) -> AgentReply:
        if parsed.intent in {Intent.DENY, Intent.GOODBYE}:
            self.ctx.state = State.ENDED
            outcome = self.ctx.outcome or (
                "booked" if self.ctx.draft.confirmation_code else "faq_answered"
            )
            self._finish_call(outcome, "caller finished")
            return AgentReply(
                text=f"Thanks for calling {config.BUSINESS_NAME}. Goodbye.",
                state=State.ENDED,
                end_call=True,
            )
        # Anything else — treat it as a fresh request.
        self.ctx.state = State.LISTENING
        return self._on_listening(parsed, caller_text)

    # --- teardown --------------------------------------------------------

    def hang_up(self, reason: str = "caller hung up") -> None:
        """Called by the transport on disconnect. Releases any held slot so an
        abandoned call doesn't sit on inventory until the hold expires."""
        if self.ctx.draft.slot_id and not self.ctx.draft.confirmation_code:
            calendar_tool.release_slot(self.ctx.draft.slot_id, self.call_sid)
        if not self.ctx.outcome:
            outcome = "booked" if self.ctx.draft.confirmation_code else "abandoned"
            self._finish_call(outcome, reason)
