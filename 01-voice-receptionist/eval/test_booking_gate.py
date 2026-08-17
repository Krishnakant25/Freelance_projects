"""
The booking gate — the single most important test in this project.

Architecture doc §6.5: "The LLM says 'You're all set for Tuesday at 3' before
or regardless of whether the calendar write succeeded. It's a language model —
a plausible confirmation is the most likely next token whether or not the tool
returned success."

The fix is structural: `_say_booked()` is the only function that produces a
booking-confirmed utterance, and it raises rather than lie if handed a failed
result. These tests attack that gate directly.

Run:  python eval/test_booking_gate.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

_harness.quiet_logs()

from app import calendar_tool, config, db  # noqa: E402
from app.agent import BookingGateViolation, Receptionist  # noqa: E402
from app.dialogue import State  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


BOOKED_PHRASES = ["you're booked", "you are booked", "all set", "confirmation code", "booked for"]


def sounds_booked(text: str) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in BOOKED_PHRASES)


def seed_slots(count: int = 3) -> list[int]:
    _harness.reset_db()
    ids = []
    base = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    with db.session() as conn:
        for i in range(count):
            cur = conn.execute(
                "INSERT INTO slots (starts_at, duration_minutes) VALUES (?, ?)",
                ((base + timedelta(minutes=30 * i)).isoformat(timespec="minutes"), 30),
            )
            ids.append(cur.lastrowid)
    return ids


def business_hours_now() -> datetime:
    """A datetime guaranteed to be inside configured business hours, so tests
    don't behave differently depending on when they run."""
    d = datetime.now().replace(hour=config.BUSINESS_HOURS_START + 1, minute=0, second=0, microsecond=0)
    while d.weekday() not in config.BUSINESS_DAYS:
        d += timedelta(days=1)
    return d


# --- 1. The gate refuses to fabricate ------------------------------------


def test_gate_raises_on_failed_result():
    """Directly hand _say_booked a FAILED result. It must raise, not speak."""
    print("\n[gate] _say_booked must refuse a failed booking result")
    seed_slots()
    agent = Receptionist(call_sid="gate-1", now=business_hours_now())

    failed = calendar_tool.BookingResult(success=False, reason="hold_expired", message="expired")
    raised = False
    try:
        agent._say_booked(failed)
    except BookingGateViolation:
        raised = True
    check("raises BookingGateViolation on success=False", raised, "the gate produced a confirmation anyway")


def test_gate_raises_on_missing_code():
    """success=True but no confirmation code is still not a booking."""
    print("\n[gate] _say_booked must refuse success=True with an empty code")
    seed_slots()
    agent = Receptionist(call_sid="gate-2", now=business_hours_now())

    bogus = calendar_tool.BookingResult(success=True, confirmation_code="")
    raised = False
    try:
        agent._say_booked(bogus)
    except BookingGateViolation:
        raised = True
    check("raises when confirmation_code is empty", raised, "the gate accepted a codeless booking")


# --- 2. Failure paths never sound like success --------------------------


def test_expired_hold_does_not_produce_confirmation():
    """The realistic version of the bug: caller takes a long time, the hold
    lapses, and the agent must NOT say they're booked."""
    print("\n[failure] an expired hold must not produce a booking confirmation")
    slot_ids = seed_slots()
    agent = Receptionist(call_sid="gate-3", now=business_hours_now())
    agent.greeting()
    agent.handle("I'd like to book an appointment")
    agent.handle("the first one")
    agent.handle("my name is Sarah Chen")
    agent.handle("555 123 4567")

    # Sabotage the hold between read-back and confirmation.
    with db.session() as conn:
        conn.execute("UPDATE slot_holds SET held_until = datetime('now', '-1 minute')")

    reply = agent.handle("yes that's right")

    check("reply is NOT a booking confirmation", not reply.booking_confirmed, reply.text[:100])
    check("no confirmation code returned", reply.confirmation_code == "", reply.confirmation_code)
    check(
        "the spoken text does not claim a booking",
        not sounds_booked(reply.text),
        f"said: {reply.text[:140]!r}",
    )
    check("state is not BOOKED", reply.state != State.BOOKED, reply.state.value)

    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM bookings WHERE status='confirmed'").fetchone()["c"]
    check("no booking exists in the database", count == 0, f"found {count}")


def test_slot_stolen_mid_call_does_not_produce_confirmation():
    """Another caller confirms the slot while this one is still talking."""
    print("\n[failure] losing the slot mid-call must not produce a confirmation")
    slot_ids = seed_slots()
    agent = Receptionist(call_sid="gate-4", now=business_hours_now())
    agent.greeting()
    agent.handle("can I book an appointment")
    agent.handle("first one please")
    agent.handle("my name is Tom Reed")
    agent.handle("5551234567")

    # Competing caller takes it: clear our hold, then book it out from under us.
    stolen_slot = agent.ctx.draft.slot_id
    with db.session() as conn:
        conn.execute("DELETE FROM slot_holds WHERE slot_id = ?", (stolen_slot,))
    calendar_tool.reserve_slot(stolen_slot, "other-call")
    other = calendar_tool.confirm_booking(
        slot_id=stolen_slot, call_sid="other-call",
        customer_name="Faster Caller", customer_phone="+15559999999",
    )
    check("competing caller genuinely got the slot", other.success, other.reason)

    reply = agent.handle("yes")
    check("our caller is NOT told they're booked", not reply.booking_confirmed, reply.text[:100])
    check("no confirmation code", reply.confirmation_code == "")
    check("text does not claim a booking", not sounds_booked(reply.text), reply.text[:140])
    check("caller is offered alternatives", "have" in reply.text.lower() or "another" in reply.text.lower(), reply.text[:140])


def test_incomplete_details_never_book():
    print("\n[failure] the agent cannot book without confirmed name and phone")
    seed_slots()
    agent = Receptionist(call_sid="gate-5", now=business_hours_now())
    agent.greeting()
    agent.handle("book me in")
    agent.handle("option 1")

    # Try to force a booking with no details captured.
    reply = agent._attempt_booking()
    check("no booking confirmed", not reply.booking_confirmed, reply.text[:100])
    check("agent asks for the missing detail instead", reply.state in {State.COLLECTING_NAME, State.COLLECTING_PHONE}, reply.state.value)

    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM bookings").fetchone()["c"]
    check("no booking row created", count == 0, f"found {count}")


def test_unconfirmed_phone_blocks_booking():
    """Read-back is a gate, not a formality (doc §6.4): an unconfirmed
    transcription must not become a booking record."""
    print("\n[readback] a phone number that wasn't confirmed cannot be booked")
    seed_slots()
    agent = Receptionist(call_sid="gate-6", now=business_hours_now())
    agent.greeting()
    agent.handle("appointment please")
    agent.handle("1")
    agent.handle("my name is Dana Lopez")
    agent.handle("555 987 6543")

    check("agent is in phone read-back state", agent.ctx.state == State.CONFIRMING_PHONE, agent.ctx.state.value)
    check("phone is captured but NOT yet confirmed", bool(agent.ctx.draft.phone) and not agent.ctx.draft.phone_confirmed)

    # Force a booking attempt while unconfirmed.
    reply = agent._attempt_booking()
    check("booking refused while phone unconfirmed", not reply.booking_confirmed, reply.text[:100])


# --- 3. The happy path still works --------------------------------------


def test_successful_booking_does_confirm():
    """The gate must not be so strict that legitimate bookings fail — otherwise
    it's just broken rather than safe."""
    print("\n[happy path] a genuine booking DOES get confirmed")
    seed_slots()
    agent = Receptionist(call_sid="gate-7", now=business_hours_now())
    agent.greeting()
    agent.handle("I'd like to book an appointment please")
    agent.handle("the first option")
    agent.handle("my name is Priya Nair")
    agent.handle("555 246 8100")
    reply = agent.handle("yes that's correct")

    check("booking_confirmed is True", reply.booking_confirmed, reply.text[:140])
    check("a confirmation code was issued", bool(reply.confirmation_code), reply.confirmation_code)
    check("the code is spoken back to the caller", " ".join(reply.confirmation_code) in reply.text, reply.text[:160])
    check("the spoken text confirms the booking", sounds_booked(reply.text), reply.text[:140])

    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM bookings WHERE confirmation_code = ?", (reply.confirmation_code,)
        ).fetchone()
    check("the booking exists in the database", row is not None)
    check("stored name matches", row and row["customer_name"] == "Priya Nair", row["customer_name"] if row else "")
    check("stored phone matches", row and row["customer_phone"] == "5552468100", row["customer_phone"] if row else "")


def test_booking_confirmed_flag_only_set_by_gate():
    """Scan every reply across a full call: booking_confirmed must be True at
    most once, and only alongside a real code."""
    print("\n[invariant] booking_confirmed appears exactly once, with a code")
    seed_slots()
    agent = Receptionist(call_sid="gate-8", now=business_hours_now())
    replies = [agent.greeting()]
    for utterance in [
        "hi I need an appointment",
        "option 2",
        "this is Marcus Webb",
        "555 333 2211",
        "yes",
        "no that's all thanks",
    ]:
        replies.append(agent.handle(utterance))

    confirmed = [r for r in replies if r.booking_confirmed]
    check("exactly one reply claims a booking", len(confirmed) == 1, f"{len(confirmed)} replies claimed a booking")
    check("that reply carries a code", confirmed and bool(confirmed[0].confirmation_code))
    check(
        "no reply claims a booking without the flag",
        not any(sounds_booked(r.text) and not r.booking_confirmed for r in replies),
        "a reply sounded like a confirmation without the flag set",
    )


def main():
    print("=" * 78)
    print("Booking gate: the agent cannot confirm a booking that doesn't exist")
    print("=" * 78)

    test_gate_raises_on_failed_result()
    test_gate_raises_on_missing_code()
    test_expired_hold_does_not_produce_confirmation()
    test_slot_stolen_mid_call_does_not_produce_confirmation()
    test_incomplete_details_never_book()
    test_unconfirmed_phone_blocks_booking()
    test_successful_booking_does_confirm()
    test_booking_confirmed_flag_only_set_by_gate()

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("No phantom confirmations: the gate holds on every failure path tested.")


if __name__ == "__main__":
    main()
