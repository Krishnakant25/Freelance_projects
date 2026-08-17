"""Console transport — type instead of speak.

The agent core is transport-agnostic on purpose: this drives exactly the same
`Receptionist` that a phone call would, so a conversation you can reproduce here
is a conversation the phone path will reproduce too (modulo speech recognition,
which is the part that genuinely differs — see README "Honest scope").

Usage:
    python -m app.cli setup                 # generate slots + load FAQ
    python -m app.cli call                  # interactive console call
    python -m app.cli call --at 02:00       # simulate an after-hours call
    python -m app.cli calls                 # recent call log
    python -m app.cli bookings              # upcoming appointments
    python -m app.cli metrics               # containment / abandon rates
"""
import argparse
import sys
from datetime import datetime, timedelta

from . import calendar_tool, config, db, faq
from .agent import Receptionist

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def cmd_setup(args):
    db.init_db()
    created = calendar_tool.generate_slots()
    faq.invalidate_cache()
    with db.session() as conn:
        existing = conn.execute("SELECT COUNT(*) c FROM faq_entries").fetchone()["c"]
    loaded = 0
    if existing == 0:
        loaded = faq.load_from_directory(config.ROOT_DIR / "data" / "faq")
    print(f"Created {created} appointment slots.")
    print(f"FAQ entries: {faq.entry_count()} ({loaded} newly loaded)")


def cmd_reset(args):
    """Wipes calls/bookings/callbacks and regenerates slots.

    Exists for demo recording: without it, each take consumes slots and leaves
    bookings behind, so the second take offers different times than the first
    and the metrics view shows leftovers from rehearsals.
    """
    db.init_db()
    with db.session() as conn:
        # audit_log is append-only by trigger and deliberately NOT cleared —
        # if a reset could erase the audit trail it wouldn't be an audit trail.
        for table in ("turns", "calls", "bookings", "slot_holds", "callbacks", "slots"):
            conn.execute(f"DELETE FROM {table}")
    created = calendar_tool.generate_slots()
    print(f"Reset complete. {created} fresh slots. FAQ entries kept: {faq.entry_count()}")
    print("(audit_log intentionally preserved — it's append-only by design)")


def cmd_call(args):
    db.init_db()

    # Warm the FAQ model BEFORE the call starts. Without this the first question
    # a caller asks pays the full model load (~11s) mid-conversation — which on a
    # phone line is dead air, and in a demo recording looks like a hang.
    if config.WARMUP_ON_STARTUP:
        print("Warming up (loading FAQ model)...", end=" ", flush=True)
        elapsed = faq.warmup()
        print(f"ready in {elapsed:.1f}s")

    now = None
    if args.at:
        hh, mm = args.at.split(":")
        now = datetime.now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        while now.weekday() not in config.BUSINESS_DAYS:
            now += timedelta(days=1)

    agent = Receptionist(caller_number=args.from_number, now=now)
    print("=" * 70)
    label = now.strftime("%A %H:%M") if now else "now"
    print(f"Simulated call — {label}   (Ctrl-C or 'hangup' to end)")
    print("=" * 70)

    greeting = agent.greeting()
    print(f"\nAGENT: {greeting.text}")

    try:
        while True:
            try:
                caller = input("\nYOU:   ").strip()
            except EOFError:
                break
            if not caller:
                continue
            if caller.lower() in {"hangup", "quit", "exit"}:
                agent.hang_up()
                print("\n[call ended by caller]")
                break

            reply = agent.handle(caller)
            print(f"\nAGENT: {reply.text}")
            if args.debug:
                print(f"       [state={reply.state.value} {reply.latency_ms:.0f}ms {reply.debug}]")
            if reply.transfer_to:
                print(f"       [TRANSFERRING to {reply.transfer_to}]")
            if reply.booking_confirmed:
                print(f"       [BOOKING CONFIRMED: {reply.confirmation_code}]")
            if reply.end_call:
                print("\n[call ended]")
                break
    except KeyboardInterrupt:
        agent.hang_up()
        print("\n[call ended]")

    print(f"\nOutcome: {agent.ctx.outcome or 'abandoned'}   Turns: {agent.ctx.turn_count}")


def cmd_calls(args):
    db.init_db()
    with db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY started_at DESC LIMIT ?", (args.limit,)
        ).fetchall()
    if not rows:
        print("No calls recorded.")
        return
    print(f"\n{'ID':4s} {'STARTED':20s} {'OUTCOME':20s} {'TURNS':>5s} {'CONF':>5s} {'AI?':>4s}")
    print("-" * 66)
    for r in rows:
        print(
            f"{r['id']:<4d} {(r['started_at'] or ''):20s} {(r['outcome'] or '-'):20s} "
            f"{r['turns']:>5d} {r['confusion_count']:>5d} {'yes' if r['disclosed_ai'] else 'NO':>4s}"
        )


def cmd_bookings(args):
    db.init_db()
    with db.session() as conn:
        rows = conn.execute(
            """SELECT b.confirmation_code, b.customer_name, b.customer_phone, s.starts_at
               FROM bookings b JOIN slots s ON s.id = b.slot_id
               WHERE b.status = 'confirmed'
               ORDER BY s.starts_at LIMIT ?""",
            (args.limit,),
        ).fetchall()
    if not rows:
        print("No confirmed bookings.")
        return
    print(f"\n{'CODE':8s} {'WHEN':22s} {'NAME':22s} PHONE")
    print("-" * 70)
    for r in rows:
        when = datetime.fromisoformat(r["starts_at"]).strftime("%a %d %b %H:%M")
        print(f"{r['confirmation_code']:8s} {when:22s} {r['customer_name'][:22]:22s} {r['customer_phone']}")


def cmd_metrics(args):
    """The numbers doc §6.8 says to track. Abandon rate is the one that matters
    most — it's callers hanging up on the agent."""
    db.init_db()
    with db.session() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM calls WHERE outcome IS NOT NULL").fetchone()["c"]
        rows = conn.execute(
            "SELECT outcome, COUNT(*) c FROM calls WHERE outcome IS NOT NULL GROUP BY outcome"
        ).fetchall()
        undisclosed = conn.execute(
            "SELECT COUNT(*) c FROM calls WHERE disclosed_ai = 0 AND outcome IS NOT NULL"
        ).fetchone()["c"]
        callbacks = conn.execute("SELECT COUNT(*) c FROM callbacks WHERE status='open'").fetchone()["c"]

    if total == 0:
        print("No completed calls yet.")
        return

    counts = {r["outcome"]: r["c"] for r in rows}
    contained = sum(counts.get(k, 0) for k in ("booked", "cancelled", "rescheduled", "faq_answered"))
    abandoned = sum(counts.get(k, 0) for k in ("abandoned", "failed"))
    transferred = counts.get("transferred", 0)

    print(f"\nCompleted calls: {total}\n")
    for outcome, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {outcome:22s} {count:>4d}  ({count / total:.0%})")

    print(f"\n  Containment rate   {contained / total:.0%}   (resolved without a human)")
    print(f"  Transfer rate      {transferred / total:.0%}")
    print(f"  Abandon rate       {abandoned / total:.0%}   <- the number that matters most")
    print(f"\n  Open callbacks     {callbacks}")
    if undisclosed:
        print(f"\n  WARNING: {undisclosed} call(s) had no AI disclosure recorded — check config.")


def main():
    parser = argparse.ArgumentParser(description="Voice receptionist — console transport")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Generate slots and load the FAQ").set_defaults(func=cmd_setup)

    p_call = sub.add_parser("call", help="Interactive simulated call")
    p_call.add_argument("--from-number", default="+15551230000")
    p_call.add_argument("--at", default="", help="Simulate a call at HH:MM (e.g. 02:00 for after hours)")
    p_call.add_argument("--debug", action="store_true", help="Show state and latency per turn")
    p_call.set_defaults(func=cmd_call)

    p_calls = sub.add_parser("calls", help="Recent call log")
    p_calls.add_argument("--limit", type=int, default=20)
    p_calls.set_defaults(func=cmd_calls)

    p_bookings = sub.add_parser("bookings", help="Upcoming confirmed appointments")
    p_bookings.add_argument("--limit", type=int, default=20)
    p_bookings.set_defaults(func=cmd_bookings)

    sub.add_parser("metrics", help="Containment / abandon rates").set_defaults(func=cmd_metrics)
    sub.add_parser("reset", help="Clear calls/bookings and regenerate slots (for clean demo takes)").set_defaults(func=cmd_reset)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
