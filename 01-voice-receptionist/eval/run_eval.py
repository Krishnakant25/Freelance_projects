"""
Replay eval harness — architecture doc §6.8.

The doc's complaint about the original plan: "§5 says 'iterate the prompt,' but
there's no definition of working. Prompt changes then get judged on vibes and
silently regress."

So: scripted calls replayed against the real agent, scored on OUTCOME and
CAPTURED DATA rather than exact wording (which would make the suite brittle to
copy changes). Reports the production metrics the doc names — containment rate
and abandon rate — plus per-turn latency against the budget.

Run:  python eval/run_eval.py
      python eval/run_eval.py --verbose      (print full transcripts)
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

_harness.quiet_logs()

from app import calendar_tool, config, db, faq  # noqa: E402
from app.agent import Receptionist  # noqa: E402
from app.dialogue import State  # noqa: E402

ROOT = _harness.ROOT
SCRIPTS_PATH = Path(__file__).resolve().parent / "call_scripts.json"
FAQ_DIR = ROOT / "data" / "faq"

# Outcomes that mean the agent resolved the call itself. Containment rate is the
# headline number a client cares about: what fraction of calls didn't need a human.
CONTAINED_OUTCOMES = {"booked", "cancelled", "rescheduled", "faq_answered"}


def in_hours() -> datetime:
    d = datetime.now().replace(hour=config.BUSINESS_HOURS_START + 1, minute=0, second=0, microsecond=0)
    while d.weekday() not in config.BUSINESS_DAYS:
        d += timedelta(days=1)
    return d


def out_of_hours() -> datetime:
    d = datetime.now().replace(hour=2, minute=0, second=0, microsecond=0)
    while d.weekday() not in config.BUSINESS_DAYS:
        d += timedelta(days=1)
    return d


def setup_world() -> None:
    _harness.reset_db()
    calendar_tool.generate_slots(days_ahead=7, now=datetime.now())
    faq.invalidate_cache()
    faq.load_from_directory(FAQ_DIR)


def run_script(script: dict, verbose: bool = False) -> dict:
    setup_world()
    now = out_of_hours() if script.get("when") == "out_of_hours" else in_hours()

    # Some scripts need an existing booking to cancel.
    seeded_code = ""
    if "setup_booking" in script:
        sb = script["setup_booking"]
        slots = calendar_tool.available_slots(limit=1, after=datetime.now())
        calendar_tool.reserve_slot(slots[0].id, "seed-call")
        result = calendar_tool.confirm_booking(
            slot_id=slots[0].id, call_sid="seed-call",
            customer_name=sb["name"], customer_phone=sb["phone"],
            idempotency_key=f"seed-{script['id']}",
        )
        seeded_code = result.confirmation_code

    agent = Receptionist(call_sid=f"eval-{script['id']}", now=now)
    transcript = [("agent", agent.greeting().text)]
    latencies = []
    transferred = False
    booking_confirmed = False
    confirmation_code = ""

    for utterance in script["turns"]:
        utterance = utterance.replace("{{confirmation_code}}", seeded_code)
        transcript.append(("caller", utterance))
        reply = agent.handle(utterance)
        transcript.append(("agent", reply.text))
        latencies.append(reply.latency_ms)
        if reply.transfer_to:
            transferred = True
        if reply.booking_confirmed:
            booking_confirmed = True
            confirmation_code = reply.confirmation_code
        if reply.end_call:
            break

    # If the caller ran out of scripted turns without the call ending, that's an
    # abandon — the agent didn't reach a conclusion.
    if not agent.ctx.outcome:
        agent.hang_up("eval script exhausted")

    with db.session() as conn:
        call_row = conn.execute(
            "SELECT * FROM calls WHERE call_sid = ?", (f"eval-{script['id']}",)
        ).fetchone()

    outcome = call_row["outcome"] if call_row else ""
    failures = []

    expected_outcome = script.get("expect_outcome")
    if expected_outcome and outcome != expected_outcome:
        failures.append(f"outcome={outcome!r}, expected {expected_outcome!r}")

    if script.get("expect_booking") and not booking_confirmed:
        failures.append("expected a confirmed booking, got none")
    if script.get("expect_booking") is False and booking_confirmed:
        failures.append("a booking was confirmed but none was expected")

    if "expect_transfer" in script:
        if script["expect_transfer"] and not transferred:
            failures.append("expected a transfer, none happened")
        if not script["expect_transfer"] and transferred:
            failures.append("transferred when it should not have")

    # Verify captured data actually landed in the database, not just that the
    # conversation sounded right.
    for field, expected in script.get("expect_captured", {}).items():
        if not confirmation_code:
            failures.append(f"cannot verify {field}: no booking created")
            break
        with db.session() as conn:
            row = conn.execute(
                "SELECT * FROM bookings WHERE confirmation_code = ?", (confirmation_code,)
            ).fetchone()
        actual = row[field] if row else None
        if actual != expected:
            failures.append(f"{field}={actual!r}, expected {expected!r}")

    agent_text = " ".join(t for spk, t in transcript if spk == "agent").lower()
    for phrase in script.get("expect_reply_contains", []):
        if phrase.lower() not in agent_text:
            failures.append(f"agent never said {phrase!r}")

    # Assert the MIDDLE of the call, not just its ending. A script that only
    # checks the final outcome can pass while an intermediate step is broken —
    # exactly what happened with the decline path, where "none of those work"
    # was misread as confusion and the next turn still produced a booking.
    if script.get("expect_no_confusion") and agent.ctx.confusion_events > 0:
        failures.append(
            f"agent was confused {agent.ctx.confusion_events}x on a script that should be understood throughout"
        )

    expected_offers = script.get("expect_distinct_slot_offers")
    if expected_offers:
        offer_sets = [
            frozenset(s.id for s in offer) for offer in agent.ctx.offer_history
        ]
        distinct = len(set(offer_sets))
        if distinct < expected_offers:
            failures.append(
                f"only {distinct} distinct slot offer(s), expected {expected_offers} "
                f"— declining is probably re-offering the same times"
            )

    if verbose:
        print(f"\n--- transcript: {script['id']} ---")
        for spk, text in transcript:
            print(f"  {spk:>6}: {text}")

    return {
        "id": script["id"],
        "passed": not failures,
        "failures": failures,
        "outcome": outcome,
        "turns": len(script["turns"]),
        "booking": booking_confirmed,
        "transferred": transferred,
        "max_latency_ms": max(latencies) if latencies else 0.0,
        "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else 0.0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", help="print full transcripts")
    args = parser.parse_args()

    print("=" * 82)
    print("Replay eval: scripted calls against the real agent")
    print("=" * 82)

    scripts = json.loads(SCRIPTS_PATH.read_text(encoding="utf-8"))["scripts"]
    results = [run_script(s, verbose=args.verbose) for s in scripts]

    print(f"\n{'CALL':38s} {'PASS':6s} {'OUTCOME':22s} {'MAX ms':>8s}")
    print("-" * 82)
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{r['id']:38s} {status:6s} {r['outcome']:22s} {r['max_latency_ms']:8.1f}")
        for f in r["failures"]:
            print(f"    -> {f}")

    passed = sum(1 for r in results if r["passed"])
    contained = sum(1 for r in results if r["outcome"] in CONTAINED_OUTCOMES)
    abandoned = sum(1 for r in results if r["outcome"] in {"abandoned", "failed"})
    transfers = sum(1 for r in results if r["transferred"])
    worst_latency = max((r["max_latency_ms"] for r in results), default=0.0)
    avg_latency = sum(r["avg_latency_ms"] for r in results) / len(results) if results else 0.0

    print("\n" + "=" * 82)
    print("PRODUCTION METRICS (doc §6.8)")
    print(f"  Containment rate  {contained}/{len(results)}  ({contained / len(results):.0%})"
          "   — calls resolved without a human")
    print(f"  Transfer rate     {transfers}/{len(results)}  ({transfers / len(results):.0%})"
          "   — expected for emergency/human-request scripts")
    print(f"  Abandon rate      {abandoned}/{len(results)}  ({abandoned / len(results):.0%})"
          "   — the number that matters most: caller gave up")
    print()
    print("LATENCY (agent decision time only — see README: this EXCLUDES speech")
    print("recognition, text-to-speech, and network, which dominate a real call)")
    print(f"  worst turn        {worst_latency:.1f} ms")
    print(f"  average turn      {avg_latency:.1f} ms")
    print(f"  budget            {config.TARGET_TURN_LATENCY_MS} ms for the FULL round trip")

    print("\n" + "=" * 82)
    print(f"TOTAL: {passed}/{len(results)} scripts passed")

    if passed < len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
