"""
Escalation, disclosure, and escape-hatch tests — the paths that stop a caller
being stranded.

Covers architecture doc §6.6 (escalation must not dead-end, and the agent must
stop looping) and §6.7 (AI/recording disclosure, which cannot be retrofitted
onto recordings already taken).

Run:  python eval/test_safety_paths.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

_harness.quiet_logs()

from app import config, db  # noqa: E402
from app.agent import Receptionist, is_within_business_hours  # noqa: E402
from app.dialogue import State  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


def in_hours() -> datetime:
    d = datetime.now().replace(hour=config.BUSINESS_HOURS_START + 1, minute=0, second=0, microsecond=0)
    while d.weekday() not in config.BUSINESS_DAYS:
        d += timedelta(days=1)
    return d


def out_of_hours() -> datetime:
    """2am on a business day — the case the doc calls out specifically: the
    entire premise of a 24/7 receptionist is that there's often nobody to
    transfer to."""
    d = datetime.now().replace(hour=2, minute=0, second=0, microsecond=0)
    while d.weekday() not in config.BUSINESS_DAYS:
        d += timedelta(days=1)
    return d


# --- Disclosure (doc §6.7) -----------------------------------------------


def test_greeting_discloses_ai():
    print("\n[disclosure] the opening turn must disclose it's an AI")
    _harness.reset_db()
    agent = Receptionist(call_sid="disc-1", now=in_hours())
    reply = agent.greeting()

    lowered = reply.text.lower()
    check(
        "greeting says it's automated/AI",
        any(w in lowered for w in ["automated", "ai ", "virtual assistant", "assistant"]),
        reply.text,
    )
    check("disclosure flag set on context", agent.ctx.disclosed_ai)

    with db.session() as conn:
        row = conn.execute("SELECT * FROM calls WHERE call_sid = 'disc-1'").fetchone()
    check("disclosure recorded on the call row (auditable)", row and row["disclosed_ai"] == 1)


def test_recording_disclosure_follows_config():
    """If recording is on, the greeting must say so. Consent can't be added to
    a recording after the fact, which is why this is config-driven and
    asserted rather than left to whoever edits the greeting string."""
    print("\n[disclosure] recording notice appears only when recording is enabled")
    _harness.reset_db()

    original = config.RECORDING_ENABLED
    try:
        config.RECORDING_ENABLED = True
        agent = Receptionist(call_sid="disc-2", now=in_hours())
        reply = agent.greeting()
        check("mentions recording when enabled", "record" in reply.text.lower(), reply.text)
        check("recording disclosure flag set", agent.ctx.disclosed_recording)

        config.RECORDING_ENABLED = False
        agent2 = Receptionist(call_sid="disc-3", now=in_hours())
        reply2 = agent2.greeting()
        check("does NOT mention recording when disabled", "record" not in reply2.text.lower(), reply2.text)
        check("recording flag stays false", not agent2.ctx.disclosed_recording)
    finally:
        config.RECORDING_ENABLED = original


# --- Escalation ladder (doc §6.6) ---------------------------------------


def test_human_request_transfers_during_hours():
    print("\n[escalation] in business hours, a human request transfers")
    _harness.reset_db()
    agent = Receptionist(call_sid="esc-1", now=in_hours())
    agent.greeting()
    reply = agent.handle("I'd like to speak to a real person please")

    check("transfer number is set on the reply", bool(reply.transfer_to), reply.transfer_to)
    check("state is ESCALATING", reply.state == State.ESCALATING, reply.state.value)
    check("call ends (handed off)", reply.end_call)
    check("caller is told what's happening", "colleague" in reply.text.lower() or "through" in reply.text.lower(), reply.text)


def test_after_hours_does_not_transfer_into_the_void():
    """THE DEAD-END THIS PREVENTS: at 2am there is nobody to transfer to. The
    original design's 'human handoff' box assumed a human exists."""
    print("\n[escalation] after hours, must take a callback instead of transferring")
    _harness.reset_db()
    agent = Receptionist(call_sid="esc-2", now=out_of_hours())
    agent.greeting()
    reply = agent.handle("can I talk to someone please")

    check("does NOT set a transfer number", not reply.transfer_to, f"would have transferred to {reply.transfer_to}")
    check("state is TAKING_CALLBACK", reply.state == State.TAKING_CALLBACK, reply.state.value)
    check("call does not end yet (still capturing details)", not reply.end_call)
    check("explains that they're closed", "closed" in reply.text.lower(), reply.text)
    check("asks for a number", "number" in reply.text.lower(), reply.text)

    # And the callback must actually be captured.
    follow = agent.handle("my name is Alex Kim, number is 555 111 2222")
    check("callback captured", follow.state == State.CALLBACK_TAKEN, follow.state.value)
    check("call ends after capture", follow.end_call)

    with db.session() as conn:
        row = conn.execute("SELECT * FROM callbacks WHERE call_sid = 'esc-2'").fetchone()
    check("callback row exists in the database", row is not None)
    check("phone was stored", row and row["customer_phone"] == "5551112222", row["customer_phone"] if row else "")


def test_emergency_routes_to_oncall_immediately():
    print("\n[escalation] an emergency must never be routed into a booking flow")
    _harness.reset_db()
    agent = Receptionist(call_sid="esc-3", now=in_hours())
    agent.greeting()
    reply = agent.handle("I'm in severe pain and my face is swelling, this is an emergency")

    check("routed to the on-call number", reply.transfer_to == config.EMERGENCY_ONCALL_NUMBER, reply.transfer_to)
    check("state is ESCALATING", reply.state == State.ESCALATING, reply.state.value)
    check("caller told it's urgent and being handled", "urgent" in reply.text.lower(), reply.text)
    check("never mentions booking an appointment", "appointment" not in reply.text.lower(), reply.text)


def test_after_hours_emergency_gives_safety_advice():
    """Out of hours with no on-call transfer available, the agent must not just
    take a message — it should tell the caller how to get real help."""
    print("\n[escalation] after-hours emergency with no on-call gives safe advice")
    _harness.reset_db()
    original = config.EMERGENCY_ONCALL_NUMBER
    try:
        config.EMERGENCY_ONCALL_NUMBER = ""
        agent = Receptionist(call_sid="esc-4", now=out_of_hours())
        agent.greeting()
        reply = agent.handle("this is an emergency, I'm bleeding badly")

        check("does not transfer (nobody to transfer to)", not reply.transfer_to)
        check(
            "points the caller at emergency services",
            "emergency number" in reply.text.lower() or "hang up" in reply.text.lower(),
            reply.text,
        )
        check("still offers a priority callback", "call you back" in reply.text.lower(), reply.text)
    finally:
        config.EMERGENCY_ONCALL_NUMBER = original


# --- Escape hatch (doc §6.6) --------------------------------------------


def test_repeated_confusion_triggers_escape_hatch():
    """The failure this replaces: the agent cheerfully re-prompting forever
    while the caller gets angrier."""
    print("\n[escape hatch] after repeated misunderstanding, stop and take a callback")
    _harness.reset_db()
    agent = Receptionist(call_sid="esc-5", now=in_hours())
    agent.greeting()

    gibberish = "wubba lubba dub dub zzzxqq"
    replies = [agent.handle(gibberish) for _ in range(config.MAX_CONSECUTIVE_CONFUSIONS + 1)]

    final = replies[-1]
    check(
        "agent stops trying and moves to callback capture",
        final.state in {State.TAKING_CALLBACK, State.CALLBACK_TAKEN, State.ENDED},
        final.state.value,
    )
    check(
        "agent acknowledges the trouble rather than repeating the menu",
        "trouble" in final.text.lower() or "sorry" in final.text.lower(),
        final.text,
    )
    check(
        "agent does not loop the same prompt forever",
        replies[0].text != final.text,
        "final reply identical to the first — still looping",
    )


def test_confusion_counter_resets_on_success():
    """A caller who's misunderstood once then understood shouldn't be dumped to
    a callback on their next stumble — the counter must be CONSECUTIVE."""
    print("\n[escape hatch] the confusion counter resets after a successful turn")
    _harness.reset_db()
    agent = Receptionist(call_sid="esc-6", now=in_hours())
    agent.greeting()

    agent.handle("zzzxqq nonsense")
    check("confusion registered", agent.ctx.consecutive_confusions >= 1, str(agent.ctx.consecutive_confusions))

    agent.handle("what are your opening hours?")
    check("counter reset after a understood turn", agent.ctx.consecutive_confusions == 0, str(agent.ctx.consecutive_confusions))


def test_callback_capture_failure_ends_honestly():
    """If even the callback capture can't get a number, the agent must end and
    tell the caller to try another channel — not loop indefinitely."""
    print("\n[escape hatch] a failing callback capture ends the call honestly")
    _harness.reset_db()
    agent = Receptionist(call_sid="esc-7", now=out_of_hours())
    agent.greeting()
    agent.handle("let me speak to someone")   # -> TAKING_CALLBACK

    # Stop at end_call, the way a real telephony transport does — it hangs up
    # rather than continuing to send audio. Feeding turns past the end made an
    # already-concluded call start over, which is what surfaced the
    # post-end-turn guard now in agent.handle().
    replies = []
    for _ in range(config.MAX_CONSECUTIVE_CONFUSIONS + 6):
        reply = agent.handle("mumble mumble")
        replies.append(reply)
        if reply.end_call:
            break

    final = replies[-1]
    check("call ends rather than looping", final.end_call, f"state={final.state.value} after {len(replies)} turns")
    check("gave up in a reasonable number of turns", len(replies) <= 6, f"took {len(replies)} turns")
    check("caller is told what to do instead", "call back" in final.text.lower() or "business hours" in final.text.lower(), final.text)

    with db.session() as conn:
        row = conn.execute("SELECT outcome FROM calls WHERE call_sid = 'esc-7'").fetchone()
    check("outcome recorded as failed (honest, not 'handled')", row and row["outcome"] == "failed", row["outcome"] if row else "")


# --- Hang-up cleanup ----------------------------------------------------


def test_hangup_releases_held_slot():
    """An abandoned call must not sit on inventory. Without this, a caller who
    hangs up mid-booking blocks a slot until the hold expires."""
    print("\n[cleanup] hanging up mid-booking releases the held slot")
    _harness.reset_db()
    base = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    with db.session() as conn:
        conn.execute("INSERT INTO slots (starts_at, duration_minutes) VALUES (?, 30)", (base.isoformat(timespec="minutes"),))

    agent = Receptionist(call_sid="esc-8", now=in_hours())
    agent.greeting()
    agent.handle("book an appointment")
    agent.handle("1")

    slot_id = agent.ctx.draft.slot_id
    check("a slot was held", slot_id is not None)

    with db.session() as conn:
        held = conn.execute("SELECT COUNT(*) c FROM slot_holds WHERE slot_id = ?", (slot_id,)).fetchone()["c"]
    check("hold exists before hang-up", held == 1, str(held))

    agent.hang_up()

    with db.session() as conn:
        held_after = conn.execute("SELECT COUNT(*) c FROM slot_holds WHERE slot_id = ?", (slot_id,)).fetchone()["c"]
        outcome = conn.execute("SELECT outcome FROM calls WHERE call_sid = 'esc-8'").fetchone()["outcome"]
    check("hold released on hang-up", held_after == 0, str(held_after))
    check("outcome recorded as abandoned", outcome == "abandoned", str(outcome))


def test_business_hours_helper():
    print("\n[hours] business-hours detection")
    check("in-hours datetime detected as open", is_within_business_hours(in_hours()))
    check("2am detected as closed", not is_within_business_hours(out_of_hours()))


def main():
    print("=" * 78)
    print("Safety paths: disclosure, escalation ladder, escape hatch, cleanup")
    print("=" * 78)

    test_greeting_discloses_ai()
    test_recording_disclosure_follows_config()
    test_human_request_transfers_during_hours()
    test_after_hours_does_not_transfer_into_the_void()
    test_emergency_routes_to_oncall_immediately()
    test_after_hours_emergency_gives_safety_advice()
    test_repeated_confusion_triggers_escape_hatch()
    test_confusion_counter_resets_on_success()
    test_callback_capture_failure_ends_honestly()
    test_hangup_releases_held_slot()
    test_business_hours_helper()

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("No dead ends: every escalation path terminates in a real outcome.")


if __name__ == "__main__":
    main()
