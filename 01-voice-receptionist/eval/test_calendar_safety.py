"""
Booking-safety tests — the reason calendar_tool.py exists.

These cover the two failure modes from architecture doc §6.5, both of which are
invisible in a single-user demo and appear immediately in production:

  1. Two callers booking the same slot. A voice agent talks to several people
     simultaneously; "check then write" is a race.
  2. Retries creating duplicate appointments.

Run:  python eval/test_calendar_safety.py
"""
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

_harness.quiet_logs()

from app import calendar_tool, config, db  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


def seed_one_slot() -> int:
    """Creates exactly ONE bookable slot, so any race has a single winner."""
    _harness.reset_db()
    when = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO slots (starts_at, duration_minutes) VALUES (?, ?)",
            (when.isoformat(timespec="minutes"), 30),
        )
        return cur.lastrowid


# --- 1. Concurrency -------------------------------------------------------


def test_two_callers_cannot_book_same_slot():
    print("\n[race] 8 concurrent callers, ONE slot — exactly one must succeed")
    slot_id = seed_one_slot()

    results = []
    lock = threading.Lock()

    def caller(n: int):
        call_sid = f"call-{n}"
        reserve = calendar_tool.reserve_slot(slot_id, call_sid)
        if not reserve.success:
            with lock:
                results.append((n, False, reserve.reason, ""))
            return
        booking = calendar_tool.confirm_booking(
            slot_id=slot_id, call_sid=call_sid,
            customer_name=f"Caller {n}", customer_phone=f"+1555000{n:04d}",
        )
        with lock:
            results.append((n, booking.success, booking.reason, booking.confirmation_code))

    threads = [threading.Thread(target=caller, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    winners = [r for r in results if r[1]]
    losers = [r for r in results if not r[1]]

    check("all 8 callers got a definite answer", len(results) == 8, f"got {len(results)}")
    check(
        "EXACTLY ONE caller was booked",
        len(winners) == 1,
        f"{len(winners)} winners: {[(w[0], w[3]) for w in winners]}",
    )
    check("the winner received a confirmation code", winners and bool(winners[0][3]), str(winners))
    check(
        "every loser was told a specific reason (not silently failed)",
        all(r[2] for r in losers),
        str([(r[0], r[2]) for r in losers]),
    )

    with db.session() as conn:
        confirmed = conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE slot_id = ? AND status = 'confirmed'", (slot_id,)
        ).fetchone()["c"]
    check("database holds exactly one confirmed booking", confirmed == 1, f"found {confirmed}")


def test_db_index_blocks_double_booking_even_if_logic_fails():
    """Defence in depth: if a future change bypasses the hold logic, the partial
    unique index must still refuse a second confirmed booking. Application
    guards get refactored; a database constraint doesn't quietly stop applying."""
    print("\n[constraint] the DB itself must reject a second confirmed booking")
    slot_id = seed_one_slot()

    calendar_tool.reserve_slot(slot_id, "call-a")
    first = calendar_tool.confirm_booking(
        slot_id=slot_id, call_sid="call-a", customer_name="First", customer_phone="+15550001111"
    )
    check("first booking succeeded", first.success, first.reason)

    # Bypass the tool entirely and insert directly, as a buggy code path would.
    raised = False
    try:
        with db.session() as conn:
            conn.execute(
                """INSERT INTO bookings
                   (confirmation_code, slot_id, idempotency_key, customer_name, customer_phone)
                   VALUES ('XXXXXX', ?, 'bypass-key', 'Sneaky', '+15550002222')""",
                (slot_id,),
            )
    except Exception:  # noqa: BLE001 - we are asserting it raises
        raised = True
    check("direct INSERT of a second confirmed booking is rejected", raised, "DB allowed a double-booking")


# --- 2. Idempotency ------------------------------------------------------


def test_retry_does_not_create_duplicate_booking():
    print("\n[idempotency] replaying the same booking returns the SAME appointment")
    slot_id = seed_one_slot()
    calendar_tool.reserve_slot(slot_id, "call-x")

    first = calendar_tool.confirm_booking(
        slot_id=slot_id, call_sid="call-x", customer_name="Rita", customer_phone="+15551234567",
        idempotency_key="retry-test",
    )
    second = calendar_tool.confirm_booking(
        slot_id=slot_id, call_sid="call-x", customer_name="Rita", customer_phone="+15551234567",
        idempotency_key="retry-test",
    )
    third = calendar_tool.confirm_booking(
        slot_id=slot_id, call_sid="call-x", customer_name="Rita", customer_phone="+15551234567",
        idempotency_key="retry-test",
    )

    check("first attempt succeeded", first.success, first.reason)
    check("retry also reports success", second.success, second.reason)
    check("retry returns the SAME confirmation code",
          second.confirmation_code == first.confirmation_code,
          f"{first.confirmation_code} vs {second.confirmation_code}")
    check("retry is flagged as a replay, not a new booking", second.was_idempotent_replay)
    check("third attempt is consistent too", third.confirmation_code == first.confirmation_code)

    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM bookings").fetchone()["c"]
    check("only ONE booking row exists", count == 1, f"found {count}")


# --- 3. Hold semantics ---------------------------------------------------


def test_expired_hold_blocks_confirmation():
    """A hold that lapsed must NOT silently book. The caller has to be
    re-offered a time — otherwise a long call books over someone else."""
    print("\n[hold] an expired hold must refuse to confirm, not book anyway")
    slot_id = seed_one_slot()
    calendar_tool.reserve_slot(slot_id, "call-slow")

    # Force expiry.
    with db.session() as conn:
        conn.execute("UPDATE slot_holds SET held_until = datetime('now', '-1 minute') WHERE slot_id = ?", (slot_id,))

    result = calendar_tool.confirm_booking(
        slot_id=slot_id, call_sid="call-slow", customer_name="Slow", customer_phone="+15559998888"
    )
    check("confirmation refused", not result.success, "booked despite an expired hold")
    check("reason is hold_expired", result.reason == "hold_expired", result.reason)
    check("no confirmation code was issued", result.confirmation_code == "", result.confirmation_code)
    check("caller is told what happens next", "free" in result.message.lower() or "check" in result.message.lower(), result.message)


def test_released_hold_frees_slot_immediately():
    print("\n[hold] releasing a hold frees the slot for another caller at once")
    slot_id = seed_one_slot()
    calendar_tool.reserve_slot(slot_id, "call-1")

    blocked = calendar_tool.reserve_slot(slot_id, "call-2")
    check("second caller is blocked while the hold is live", not blocked.success, blocked.reason)

    calendar_tool.release_slot(slot_id, "call-1")
    now_free = calendar_tool.reserve_slot(slot_id, "call-2")
    check("second caller can reserve after release", now_free.success, now_free.reason)


def test_same_call_can_re_reserve_own_hold():
    print("\n[hold] a caller re-reserving their OWN slot is not blocked by themselves")
    slot_id = seed_one_slot()
    first = calendar_tool.reserve_slot(slot_id, "call-same")
    again = calendar_tool.reserve_slot(slot_id, "call-same")
    check("first reserve succeeded", first.success)
    check("same call can re-reserve (extends the hold)", again.success, again.reason)


# --- 4. Refusing incomplete bookings ------------------------------------


def test_booking_without_details_is_refused():
    """A booking nobody can be contacted about is worse than no booking."""
    print("\n[validation] missing name/phone must refuse rather than store a blank")
    slot_id = seed_one_slot()
    calendar_tool.reserve_slot(slot_id, "call-anon")

    no_name = calendar_tool.confirm_booking(slot_id=slot_id, call_sid="call-anon", customer_name="", customer_phone="+15551112222")
    no_phone = calendar_tool.confirm_booking(slot_id=slot_id, call_sid="call-anon", customer_name="Sam", customer_phone="")
    whitespace = calendar_tool.confirm_booking(slot_id=slot_id, call_sid="call-anon", customer_name="   ", customer_phone="   ")

    check("missing name refused", not no_name.success, no_name.reason)
    check("missing phone refused", not no_phone.success, no_phone.reason)
    check("whitespace-only refused", not whitespace.success, whitespace.reason)
    check("no code issued for incomplete booking", no_name.confirmation_code == "")

    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM bookings").fetchone()["c"]
    check("no booking rows created", count == 0, f"found {count}")


# --- 5. Cancel / reschedule ---------------------------------------------


def test_cancel_frees_the_slot_for_rebooking():
    print("\n[cancel] cancelling frees the slot (partial index allows rebooking)")
    slot_id = seed_one_slot()
    calendar_tool.reserve_slot(slot_id, "call-c1")
    booked = calendar_tool.confirm_booking(
        slot_id=slot_id, call_sid="call-c1", customer_name="Cancel Me", customer_phone="+15553334444"
    )
    check("initial booking succeeded", booked.success, booked.reason)

    cancelled = calendar_tool.cancel_booking(booked.confirmation_code, call_sid="call-c1")
    check("cancellation succeeded", cancelled.success, cancelled.reason)

    calendar_tool.reserve_slot(slot_id, "call-c2")
    rebooked = calendar_tool.confirm_booking(
        slot_id=slot_id, call_sid="call-c2", customer_name="New Person", customer_phone="+15555556666",
        idempotency_key="rebook-after-cancel",
    )
    check("the freed slot can be rebooked", rebooked.success, rebooked.reason)
    check("rebooking got a different code", rebooked.confirmation_code != booked.confirmation_code)


def test_cancel_is_idempotent():
    print("\n[cancel] cancelling twice is safe")
    slot_id = seed_one_slot()
    calendar_tool.reserve_slot(slot_id, "call-d")
    booked = calendar_tool.confirm_booking(
        slot_id=slot_id, call_sid="call-d", customer_name="Dup", customer_phone="+15557778888"
    )
    first = calendar_tool.cancel_booking(booked.confirmation_code)
    second = calendar_tool.cancel_booking(booked.confirmation_code)
    check("first cancel succeeded", first.success)
    check("second cancel also reports success", second.success, second.reason)
    check("second is flagged as a replay", second.was_idempotent_replay)


def test_confirmation_code_is_phone_friendly():
    """Codes get read aloud over an 8kHz phone line (doc §6.4). Characters that
    sound alike — 0/O, 1/I, 5/S, 8/B — must not appear."""
    print("\n[readback] confirmation codes avoid easily-confused characters")
    slot_id = seed_one_slot()
    calendar_tool.reserve_slot(slot_id, "call-code")
    booked = calendar_tool.confirm_booking(
        slot_id=slot_id, call_sid="call-code", customer_name="Code Test", customer_phone="+15550009999"
    )
    code = booked.confirmation_code
    check("a code was issued", bool(code))
    forbidden = set("01ISB5O8Z2")
    check(
        "code contains no easily-confused characters",
        not (set(code) & forbidden),
        f"code {code!r} contains {set(code) & forbidden}",
    )
    check("code is a reasonable length to say aloud", 4 <= len(code) <= 8, str(len(code)))


def main():
    print("=" * 78)
    print("Booking safety: concurrency, idempotency, hold semantics")
    print("=" * 78)

    test_two_callers_cannot_book_same_slot()
    test_db_index_blocks_double_booking_even_if_logic_fails()
    test_retry_does_not_create_duplicate_booking()
    test_expired_hold_blocks_confirmation()
    test_released_hold_frees_slot_immediately()
    test_same_call_can_re_reserve_own_hold()
    test_booking_without_details_is_refused()
    test_cancel_frees_the_slot_for_rebooking()
    test_cancel_is_idempotent()
    test_confirmation_code_is_phone_friendly()

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("No double-booking, no duplicate bookings, no phantom confirmations.")


if __name__ == "__main__":
    main()
