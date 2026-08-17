"""
Dialogue state machine for the receptionist.

Explicit states rather than free-form LLM conversation, for the reason the
architecture doc gives in §6.5: the failure mode that matters here is the agent
*saying* something happened that didn't. A state machine can be made
structurally incapable of announcing a booking it doesn't hold a confirmation
code for. A prompt can only be asked nicely.

THE CENTRAL INVARIANT, enforced in agent.py and tested in
eval/test_booking_gate.py:

    The agent may only produce a booking-confirmed utterance while in state
    BOOKED, and BOOKED is only reachable by receiving a BookingResult with
    success=True and a non-empty confirmation_code from calendar_tool.

Everything else here — read-back, DTMF fallback, the confusion escape hatch —
is a state transition, so it can be tested by driving the machine rather than
by inspecting prose.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class State(str, Enum):
    # Opening
    GREETING = "greeting"                    # discloses AI + recording (doc §6.7)
    LISTENING = "listening"                  # awaiting intent

    # Booking flow
    OFFERING_SLOTS = "offering_slots"
    COLLECTING_NAME = "collecting_name"
    COLLECTING_PHONE = "collecting_phone"
    CONFIRMING_PHONE = "confirming_phone"    # read-back (doc §6.4)
    CONFIRMING_BOOKING = "confirming_booking"
    BOOKED = "booked"                        # ONLY reachable with a real confirmation code

    # Reschedule / cancel
    COLLECTING_CODE = "collecting_code"
    CONFIRMING_CANCEL = "confirming_cancel"
    CANCELLED = "cancelled"

    # Other outcomes
    ANSWERING_FAQ = "answering_faq"
    ESCALATING = "escalating"                # transferring to a human
    TAKING_CALLBACK = "taking_callback"      # after-hours / escape hatch
    CALLBACK_TAKEN = "callback_taken"
    CLOSING = "closing"
    ENDED = "ended"


# States that mean the call reached a definite, useful conclusion. Used by the
# eval harness to compute containment rate (doc §6.8).
TERMINAL_SUCCESS_STATES = {
    State.BOOKED, State.CANCELLED, State.CALLBACK_TAKEN, State.ENDED,
}

# What the agent is waiting for in each state. Fed to nlu.understand() so a bare
# "yes" or "Sarah Chen" is interpreted against the question actually asked.
EXPECTING = {
    State.GREETING: "consent",
    State.OFFERING_SLOTS: "slot_choice",
    State.COLLECTING_NAME: "name",
    State.COLLECTING_PHONE: "phone",
    State.CONFIRMING_PHONE: "confirm_phone",
    State.CONFIRMING_BOOKING: "confirm_slot",
    State.COLLECTING_CODE: "confirmation_code",
    State.CONFIRMING_CANCEL: "confirm_details",
    State.CLOSING: "anything_else",
}


@dataclass
class BookingDraft:
    """In-progress booking details. Note `confirmation_code` starts empty and is
    ONLY populated from a successful calendar_tool result — never from anything
    the agent inferred or the caller said."""
    slot_id: Optional[int] = None
    slot_spoken: str = ""
    name: str = ""
    phone: str = ""
    phone_confirmed: bool = False
    service: str = ""
    confirmation_code: str = ""

    def is_complete(self) -> bool:
        return bool(self.slot_id and self.name and self.phone and self.phone_confirmed)

    def missing(self) -> list[str]:
        gaps = []
        if not self.slot_id:
            gaps.append("slot")
        if not self.name:
            gaps.append("name")
        if not self.phone:
            gaps.append("phone")
        elif not self.phone_confirmed:
            gaps.append("phone_confirmation")
        return gaps


@dataclass
class ConversationContext:
    call_sid: str
    state: State = State.GREETING
    draft: BookingDraft = field(default_factory=BookingDraft)
    offered_slots: list = field(default_factory=list)
    consecutive_confusions: int = 0
    turn_count: int = 0
    disclosed_ai: bool = False
    disclosed_recording: bool = False
    cancel_code: str = ""
    outcome: str = ""
    escalation_reason: str = ""
    # Set when the caller asked something the FAQ couldn't answer, so the
    # callback captured at the end has real context instead of "wanted something".
    unanswered_question: str = ""
    dtmf_offered: bool = False
    # Spoken day/time preference ("thursday afternoon"). Kept on the context so
    # it survives across turns — a caller shouldn't have to repeat "Thursday"
    # every time they decline a set of options.
    slot_hint: str = ""
    # Pagination cursor into the available-slot list. Without this, declining
    # the offered times re-offered the identical three slots in a loop.
    slot_offset: int = 0
    # Cumulative confusion count (never reset) and the history of slot sets
    # offered. Both exist so the replay eval can assert on the MIDDLE of a call
    # rather than only its final outcome — a correct ending hid a broken decline
    # path once already.
    confusion_events: int = 0
    offer_history: list = field(default_factory=list)

    def expecting(self) -> Optional[str]:
        return EXPECTING.get(self.state)

    def register_confusion(self) -> None:
        self.consecutive_confusions += 1
        self.confusion_events += 1

    def clear_confusion(self) -> None:
        self.consecutive_confusions = 0

    def should_escape(self, max_confusions: int) -> bool:
        """The escape hatch from doc §6.6: after N consecutive misunderstood
        turns, stop trying to be clever and capture a callback. A graceful
        capture beats a clever agent looping."""
        return self.consecutive_confusions >= max_confusions
