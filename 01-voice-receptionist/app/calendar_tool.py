"""
Appointment booking with slot locking and idempotency.

This module exists to make two failure modes impossible, both called out in
the architecture doc §6.5:

  1. TWO CALLERS BOOKING THE SAME SLOT. A voice agent talks to several people
     at once. "Check availability, then write the booking" is a read-then-write
     race: both callers are told 3pm is free, both get told they're booked, one
     of them shows up to nothing. Fixed with a two-phase reserve->confirm where
     the reserve is an atomic conditional claim, plus a partial unique index so
     even a logic bug upstream can't produce two confirmed bookings for a slot.

  2. RETRIES CREATING DUPLICATE BOOKINGS. Network retry, caller repeating
     themselves, or the agent re-invoking the tool must not produce two
     appointments. Every booking carries an idempotency key; replaying it
     returns the SAME booking rather than making another.

The public functions return explicit result objects with a `success` flag and a
`confirmation_code`. The dialogue layer is forbidden from announcing a booking
without that code — see agent.py.
"""
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from . import config, db

logger = logging.getLogger(__name__)

_CODE_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"  # no easily-confused chars for phone readback


def _generate_confirmation_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


@dataclass
class Slot:
    id: int
    starts_at: str
    duration_minutes: int

    def spoken(self) -> str:
        dt = datetime.fromisoformat(self.starts_at)
        return dt.strftime("%A %B %d at %I:%M %p").replace(" 0", " ")


@dataclass
class ReserveResult:
    success: bool
    slot: Optional[Slot] = None
    reason: str = ""          # machine-readable failure reason
    message: str = ""         # human/spoken explanation


@dataclass
class BookingResult:
    """The ONLY thing that authorises the agent to say "you're booked".

    `success=True` and a non-empty `confirmation_code` together mean a row
    exists in the database. Anything else means it does not, regardless of what
    the conversation seemed to conclude.
    """
    success: bool
    confirmation_code: str = ""
    slot: Optional[Slot] = None
    reason: str = ""
    message: str = ""
    was_idempotent_replay: bool = False


# --- Slot inventory --------------------------------------------------------


def generate_slots(days_ahead: Optional[int] = None, now: Optional[datetime] = None) -> int:
    """Creates bookable slots across business hours/days. Idempotent — the
    UNIQUE(starts_at) constraint means re-running adds only new slots."""
    days_ahead = days_ahead or config.BOOKING_LOOKAHEAD_DAYS
    now = now or datetime.now()
    created = 0
    with db.session() as conn:
        for day_offset in range(days_ahead):
            day = (now + timedelta(days=day_offset)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if day.weekday() not in config.BUSINESS_DAYS:
                continue
            slot_time = day.replace(hour=config.BUSINESS_HOURS_START)
            end_time = day.replace(hour=config.BUSINESS_HOURS_END)
            while slot_time < end_time:
                if slot_time > now:
                    try:
                        conn.execute(
                            "INSERT INTO slots (starts_at, duration_minutes) VALUES (?, ?)",
                            (slot_time.isoformat(timespec="minutes"), config.APPOINTMENT_MINUTES),
                        )
                        created += 1
                    except Exception:  # noqa: BLE001 - UNIQUE violation = already exists
                        pass
                slot_time += timedelta(minutes=config.APPOINTMENT_MINUTES)
    return created


def _row_to_slot(row) -> Slot:
    return Slot(id=row["id"], starts_at=row["starts_at"], duration_minutes=row["duration_minutes"])


def available_slots(limit: int = 5, after: Optional[datetime] = None) -> list[Slot]:
    """Slots with no confirmed booking and no live hold.

    NOTE: this is advisory only. By the time the caller answers, another caller
    may have taken one — which is precisely why booking re-checks atomically
    rather than trusting this list.
    """
    after = after or datetime.now()
    with db.session() as conn:
        rows = conn.execute(
            """SELECT s.* FROM slots s
               LEFT JOIN bookings b ON b.slot_id = s.id AND b.status = 'confirmed'
               LEFT JOIN slot_holds h ON h.slot_id = s.id AND h.held_until > datetime('now')
               WHERE s.starts_at > ? AND b.id IS NULL AND h.slot_id IS NULL
               ORDER BY s.starts_at
               LIMIT ?""",
            (after.isoformat(timespec="minutes"), limit),
        ).fetchall()
    return [_row_to_slot(r) for r in rows]


_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def slots_matching_hint(
    hint: str, limit: int = 3, after: Optional[datetime] = None, offset: int = 0
) -> tuple[list[Slot], bool]:
    """Filters available slots by a coarse spoken hint ("thursday afternoon",
    "tomorrow", "next week").

    Returns (slots, hint_was_satisfied). If the hint matched nothing, falls back
    to the next available slots and reports False, so the agent can say "I don't
    have anything Thursday, but here's what I do have" instead of silently
    ignoring what the caller asked for.

    This exists because the hint was previously extracted and then thrown away:
    a caller asking "can I come in Thursday afternoon?" got offered Monday 9am
    with no acknowledgement, which reads as the agent not listening.
    """
    after = after or datetime.now()
    pool = available_slots(limit=200, after=after)
    if not hint:
        return pool[offset : offset + limit], True

    hint = hint.lower()
    candidates = pool

    # Day-of-week / relative-day filters.
    target_weekday = next((wd for name, wd in _WEEKDAYS.items() if name in hint), None)
    if target_weekday is not None:
        candidates = [s for s in candidates if datetime.fromisoformat(s.starts_at).weekday() == target_weekday]
    elif "tomorrow" in hint:
        target_date = (after + timedelta(days=1)).date()
        candidates = [s for s in candidates if datetime.fromisoformat(s.starts_at).date() == target_date]
    elif "today" in hint:
        candidates = [s for s in candidates if datetime.fromisoformat(s.starts_at).date() == after.date()]
    elif "next week" in hint:
        week_start = after + timedelta(days=7 - after.weekday())
        candidates = [s for s in candidates if datetime.fromisoformat(s.starts_at).date() >= week_start.date()]

    # Time-of-day filters.
    if "morning" in hint:
        candidates = [s for s in candidates if datetime.fromisoformat(s.starts_at).hour < 12]
    elif "afternoon" in hint:
        candidates = [s for s in candidates if 12 <= datetime.fromisoformat(s.starts_at).hour < 17]
    elif "evening" in hint:
        candidates = [s for s in candidates if datetime.fromisoformat(s.starts_at).hour >= 17]

    # Explicit hour, e.g. "3pm".
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", hint)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(2) == "pm" else 0)
        exact = [s for s in candidates if datetime.fromisoformat(s.starts_at).hour == hour]
        if exact:
            candidates = exact

    if candidates:
        return candidates[offset : offset + limit], True
    # Hint matched nothing — be explicit about it rather than pretending.
    return pool[offset : offset + limit], False


def find_slot_at(starts_at: str) -> Optional[Slot]:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM slots WHERE starts_at = ?", (starts_at,)).fetchone()
    return _row_to_slot(row) if row else None


# --- Phase 1: reserve (atomic claim) --------------------------------------


def reserve_slot(slot_id: int, call_sid: str) -> ReserveResult:
    """Takes an exclusive, expiring hold on a slot.

    This is the race-critical operation. It runs in a BEGIN IMMEDIATE
    transaction so the "is it free?" check and the "claim it" write cannot be
    interleaved with another caller doing the same thing. A deferred
    transaction would let both callers read "free" and only detect the
    conflict at commit time.
    """
    with db.immediate_session() as conn:
        slot = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
        if slot is None:
            return ReserveResult(False, reason="no_such_slot", message="I couldn't find that time.")

        booked = conn.execute(
            "SELECT 1 FROM bookings WHERE slot_id = ? AND status = 'confirmed'", (slot_id,)
        ).fetchone()
        if booked:
            return ReserveResult(
                False, reason="already_booked",
                message="That time has just been taken. Let me find you another.",
            )

        hold = conn.execute(
            "SELECT * FROM slot_holds WHERE slot_id = ? AND held_until > datetime('now')",
            (slot_id,),
        ).fetchone()
        if hold and hold["held_by_call"] != call_sid:
            return ReserveResult(
                False, reason="held_by_other",
                message="Someone else is booking that time right now. Let me find you another.",
            )

        # Claim it (or extend our own hold). Expired holds are overwritten,
        # which is what makes an abandoned call self-healing.
        conn.execute(
            """INSERT INTO slot_holds (slot_id, held_by_call, held_until)
               VALUES (?, ?, datetime('now', ? || ' seconds'))
               ON CONFLICT(slot_id) DO UPDATE
                 SET held_by_call = excluded.held_by_call,
                     held_until = excluded.held_until""",
            (slot_id, call_sid, f"+{int(config.SLOT_HOLD_SECONDS)}"),
        )
        db.log_event(conn, "slot_reserved", {"slot_id": slot_id}, call_sid=call_sid)
        return ReserveResult(True, slot=_row_to_slot(slot))


def release_slot(slot_id: int, call_sid: str) -> None:
    """Drops our hold — called when a caller changes their mind or hangs up, so
    the slot frees immediately instead of waiting for the hold to expire."""
    with db.session() as conn:
        conn.execute(
            "DELETE FROM slot_holds WHERE slot_id = ? AND held_by_call = ?", (slot_id, call_sid)
        )


# --- Phase 2: confirm (durable booking) ----------------------------------


def confirm_booking(
    slot_id: int,
    call_sid: str,
    customer_name: str,
    customer_phone: str,
    service: str = "",
    idempotency_key: Optional[str] = None,
) -> BookingResult:
    """Turns a hold into a durable booking.

    Returns a confirmation code ONLY on genuine success. The agent must treat
    the absence of a code as "not booked" no matter how the conversation went.
    """
    if not customer_name.strip() or not customer_phone.strip():
        # Refusing here rather than storing a nameless booking is deliberate:
        # a booking nobody can be contacted about is worse than no booking.
        return BookingResult(
            False, reason="missing_details",
            message="I still need your name and a phone number before I can book that.",
        )

    key = idempotency_key or f"{call_sid}:{slot_id}"

    with db.immediate_session() as conn:
        # Idempotent replay: same logical request returns the same booking.
        existing = conn.execute(
            "SELECT * FROM bookings WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing:
            if existing["status"] == "confirmed":
                slot = conn.execute(
                    "SELECT * FROM slots WHERE id = ?", (existing["slot_id"],)
                ).fetchone()
                return BookingResult(
                    True,
                    confirmation_code=existing["confirmation_code"],
                    slot=_row_to_slot(slot) if slot else None,
                    was_idempotent_replay=True,
                    message="That's already booked for you.",
                )
            return BookingResult(
                False, reason="previously_cancelled",
                message="That booking was cancelled. Shall I make a new one?",
            )

        slot = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
        if slot is None:
            return BookingResult(False, reason="no_such_slot", message="I couldn't find that time.")

        # Re-check the hold. If it expired or someone else holds it, we do NOT
        # book — the caller has to be re-offered a time. Silently booking over
        # an expired hold is how you get the double-booking this module exists
        # to prevent.
        hold = conn.execute(
            "SELECT * FROM slot_holds WHERE slot_id = ? AND held_until > datetime('now')",
            (slot_id,),
        ).fetchone()
        if hold is None:
            return BookingResult(
                False, reason="hold_expired",
                message="That time hold expired while we were talking. Let me check what's still free.",
            )
        if hold["held_by_call"] != call_sid:
            return BookingResult(
                False, reason="held_by_other",
                message="That time was just taken by someone else. Let me find you another.",
            )

        # Retry on the (very unlikely) code collision rather than surfacing it
        # as "that time was taken", which would be a misleading message for an
        # unrelated cause.
        code = _generate_confirmation_code()
        for _ in range(5):
            clash = conn.execute(
                "SELECT 1 FROM bookings WHERE confirmation_code = ?", (code,)
            ).fetchone()
            if not clash:
                break
            code = _generate_confirmation_code()

        try:
            conn.execute(
                """INSERT INTO bookings
                   (confirmation_code, slot_id, call_sid, idempotency_key,
                    customer_name, customer_phone, service)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (code, slot_id, call_sid, key, customer_name.strip(), customer_phone.strip(), service),
            )
        except Exception as e:  # noqa: BLE001
            # The partial unique index fired: someone confirmed this slot
            # between our hold check and this insert. The database is the final
            # arbiter, and it said no — so we report failure rather than lie.
            logger.warning("Booking insert rejected for slot %s: %s", slot_id, e)
            return BookingResult(
                False, reason="race_lost",
                message="That time was taken a moment ago. Let me find you another.",
            )

        conn.execute("DELETE FROM slot_holds WHERE slot_id = ?", (slot_id,))
        db.log_event(
            conn, "booking_confirmed",
            {"slot_id": slot_id, "code": code, "phone": customer_phone},
            call_sid=call_sid,
        )
        return BookingResult(
            True, confirmation_code=code, slot=_row_to_slot(slot),
        )


# --- Lookup / cancel / reschedule ----------------------------------------


def find_booking(confirmation_code: str = "", phone: str = "") -> Optional[dict]:
    with db.session() as conn:
        if confirmation_code:
            row = conn.execute(
                """SELECT b.*, s.starts_at, s.duration_minutes FROM bookings b
                   JOIN slots s ON s.id = b.slot_id
                   WHERE b.confirmation_code = ? AND b.status = 'confirmed'""",
                (confirmation_code.upper(),),
            ).fetchone()
        elif phone:
            row = conn.execute(
                """SELECT b.*, s.starts_at, s.duration_minutes FROM bookings b
                   JOIN slots s ON s.id = b.slot_id
                   WHERE b.customer_phone = ? AND b.status = 'confirmed'
                   ORDER BY s.starts_at LIMIT 1""",
                (phone,),
            ).fetchone()
        else:
            return None
    return dict(row) if row else None


def cancel_booking(confirmation_code: str, call_sid: str = "") -> BookingResult:
    with db.immediate_session() as conn:
        row = conn.execute(
            "SELECT * FROM bookings WHERE confirmation_code = ?", (confirmation_code.upper(),)
        ).fetchone()
        if row is None:
            return BookingResult(False, reason="not_found", message="I couldn't find a booking with that code.")
        if row["status"] == "cancelled":
            return BookingResult(
                True, confirmation_code=row["confirmation_code"],
                was_idempotent_replay=True, message="That booking was already cancelled.",
            )
        conn.execute(
            "UPDATE bookings SET status = 'cancelled', cancelled_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        db.log_event(
            conn, "booking_cancelled",
            {"code": row["confirmation_code"], "slot_id": row["slot_id"]}, call_sid=call_sid,
        )
        return BookingResult(True, confirmation_code=row["confirmation_code"], message="That's cancelled.")


def reschedule_booking(
    confirmation_code: str, new_slot_id: int, call_sid: str
) -> BookingResult:
    """Cancel + rebook. Deliberately NOT a slot_id mutation on the existing row:
    the new slot has to survive the same atomic claim as a fresh booking, and
    the old slot must genuinely free up. Mutating in place would bypass both.
    """
    existing = find_booking(confirmation_code=confirmation_code)
    if existing is None:
        return BookingResult(False, reason="not_found", message="I couldn't find a booking with that code.")

    reserve = reserve_slot(new_slot_id, call_sid)
    if not reserve.success:
        return BookingResult(False, reason=reserve.reason, message=reserve.message)

    cancel_booking(confirmation_code, call_sid=call_sid)
    return confirm_booking(
        slot_id=new_slot_id,
        call_sid=call_sid,
        customer_name=existing["customer_name"],
        customer_phone=existing["customer_phone"],
        service=existing.get("service") or "",
        idempotency_key=f"reschedule:{confirmation_code}:{new_slot_id}",
    )


def expire_stale_holds() -> int:
    with db.session() as conn:
        cur = conn.execute("DELETE FROM slot_holds WHERE held_until <= datetime('now')")
        return cur.rowcount
