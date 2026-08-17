"""
Tests for P1 alerting and unacknowledged-ticket escalation.

Run:  python eval/test_alerting.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

from app import alerting, config, db  # noqa: E402

_harness.quiet_logs()

# Keep retries at 1 for the failure-path test so it doesn't sleep through
# the full backoff schedule on every run.
config.ALERT_MAX_ATTEMPTS = 1

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


def test_alert_without_slack_falls_back_to_log():
    print("\n[alert] no Slack configured -> logs instead of silently doing nothing")
    result = alerting.send_p1_alert(999, "test description", "test reasoning")
    check("alert reports sent=True via log channel", result.sent is True and result.channel == "log")


def test_alert_with_broken_slack_url_does_not_crash():
    print("\n[alert] a broken webhook URL fails gracefully, doesn't raise")
    config.SLACK_WEBHOOK_URL = "https://hooks.slack.invalid/definitely-not-real"
    try:
        result = alerting.send_p1_alert(999, "test description", "test reasoning")
        check("did not raise", True)
        check("reports sent=False on failure", result.sent is False)
        check("channel recorded as slack", result.channel == "slack")
        check("error message captured", bool(result.error))
    except Exception as e:  # noqa: BLE001
        check("did not raise", False, f"raised {e}")
    finally:
        config.SLACK_WEBHOOK_URL = ""


def test_escalation_fires_for_old_unacknowledged_p1():
    print("\n[escalation] an old, unacknowledged P1 gets re-alerted")
    _harness.reset_db()
    config.P1_ESCALATION_MINUTES = 15

    with db.session() as conn:
        # Insert a P1 ticket with a created_at in the past, bypassing the
        # normal insert path so we control the timestamp directly.
        cur = conn.execute(
            """INSERT INTO tickets
               (created_at, requester, category, affected_system, impact, urgency,
                priority, description, reasoning, status)
               VALUES (datetime('now', '-30 minutes'), 'test-user', 'network', 'VPN',
                       'organization', 'high', 'P1', 'test outage', 'test reasoning', 'open')"""
        )
        old_ticket_id = cur.lastrowid

        cur2 = conn.execute(
            """INSERT INTO tickets
               (created_at, requester, category, affected_system, impact, urgency,
                priority, description, reasoning, status)
               VALUES (datetime('now'), 'test-user', 'network', 'VPN',
                       'organization', 'high', 'P1', 'fresh outage', 'test reasoning', 'open')"""
        )
        fresh_ticket_id = cur2.lastrowid

    escalated = alerting.check_escalations()
    escalated_ids = [e["ticket_id"] for e in escalated]

    check("the old unacknowledged P1 is escalated", old_ticket_id in escalated_ids, str(escalated_ids))
    check("the fresh P1 (within window) is NOT escalated yet", fresh_ticket_id not in escalated_ids, str(escalated_ids))


def test_acknowledged_p1_does_not_escalate():
    print("\n[escalation] an ACKNOWLEDGED old P1 must not re-escalate")
    _harness.reset_db()
    with db.session() as conn:
        cur = conn.execute(
            """INSERT INTO tickets
               (created_at, requester, category, affected_system, impact, urgency,
                priority, description, reasoning, status, acknowledged_at)
               VALUES (datetime('now', '-30 minutes'), 'test-user', 'network', 'VPN',
                       'organization', 'high', 'P1', 'already being handled', 'test', 'acknowledged', datetime('now'))"""
        )
        ack_ticket_id = cur.lastrowid

    escalated = alerting.check_escalations()
    escalated_ids = [e["ticket_id"] for e in escalated]
    check("acknowledged ticket is not re-escalated", ack_ticket_id not in escalated_ids, str(escalated_ids))


def test_escalation_writes_audit_log():
    print("\n[audit] escalation is recorded in the audit log")
    _harness.reset_db()
    with db.session() as conn:
        cur = conn.execute(
            """INSERT INTO tickets
               (created_at, requester, category, affected_system, impact, urgency,
                priority, description, reasoning, status)
               VALUES (datetime('now', '-60 minutes'), 'test-user', 'network', 'VPN',
                       'organization', 'high', 'P1', 'audit test', 'test reasoning', 'open')"""
        )
        ticket_id = cur.lastrowid

    alerting.check_escalations()

    with db.session() as conn:
        # Event names changed with the durable-outbox rework: enqueue and
        # delivery are now separate, auditable steps rather than one combined
        # 'escalation_alert' event.
        enqueued = conn.execute(
            "SELECT * FROM audit_log WHERE ticket_id = ? AND event_type = 'escalation_enqueued'",
            (ticket_id,),
        ).fetchall()
        delivered = conn.execute(
            "SELECT * FROM audit_log WHERE ticket_id = ? AND event_type = 'alert_delivery'",
            (ticket_id,),
        ).fetchall()
    check("an escalation_enqueued audit event exists", len(enqueued) >= 1)
    check("an alert_delivery audit event exists", len(delivered) >= 1)


def test_escalation_cooldown_prevents_repeat_alerts():
    """The bug this covers: check_escalations() had no cooldown, so it
    re-alerted the same ticket on EVERY invocation. On a one-minute scheduler
    an unacknowledged P1 would page Slack every minute indefinitely — alert
    fatigue that trains people to mute the channel."""
    print("\n[cooldown] the same ticket must not be re-escalated on every sweep")
    _harness.reset_db()
    config.ESCALATION_COOLDOWN_MINUTES = 30

    with db.session() as conn:
        conn.execute(
            """INSERT INTO tickets
               (created_at, requester, category, affected_system, impact, urgency,
                priority, description, reasoning, status)
               VALUES (datetime('now', '-60 minutes'), 'test-user', 'network', 'VPN',
                       'organization', 'high', 'P1', 'cooldown test', 'test reasoning', 'open')"""
        )

    first = alerting.check_escalations()
    check("first sweep escalates the ticket", len(first) == 1, str(first))

    second = alerting.check_escalations()
    check("second sweep does NOT re-escalate (cooldown)", len(second) == 0, str(second))

    third = alerting.check_escalations()
    check("third sweep also stays quiet", len(third) == 0, str(third))

    # With the cooldown removed, it should alert again — proving the cooldown is
    # what's suppressing it, not something else.
    config.ESCALATION_COOLDOWN_MINUTES = 0
    fourth = alerting.check_escalations()
    check("escalates again once the cooldown is zero", len(fourth) == 1, str(fourth))
    config.ESCALATION_COOLDOWN_MINUTES = 30


def test_outbox_makes_alerts_crash_safe():
    """The gap this covers: an alert was a fire-and-forget HTTP call made after
    the ticket write. A crash in between left a ticket nobody was paged about,
    with nothing recording that a page was owed."""
    print("\n[outbox] a pending alert survives and is recoverable")
    _harness.reset_db()

    from app.intake import file_ticket

    # Simulate the process dying before delivery: enqueue happens inside the
    # ticket transaction, then delivery raises.
    original_deliver = alerting.deliver_outbox_entry
    alerting.deliver_outbox_entry = lambda row: (_ for _ in ()).throw(RuntimeError("simulated crash"))
    try:
        result = file_ticket("we found ransomware on the file server", requester="crash-test")
        check("ticket was still created despite delivery failure", result.ticket_id is not None)
        check("priority is P1", result.priority == "P1", str(result.priority))
    finally:
        alerting.deliver_outbox_entry = original_deliver

    with db.session() as conn:
        pending = db.pending_alerts(conn)
    check("a pending outbox entry survived the 'crash'", len(pending) == 1, str(len(pending)))

    # Recovery: flushing the outbox delivers it.
    flushed = alerting.flush_outbox()
    check("flush delivered the pending alert", flushed["sent"] == 1, str(flushed))

    with db.session() as conn:
        counts = db.outbox_counts(conn)
    check("no alerts left pending", counts["pending"] == 0, str(counts))
    check("one alert recorded as sent", counts["sent"] == 1, str(counts))


def main():
    print("=" * 74)
    print("Alerting and escalation tests")
    print("=" * 74)

    test_alert_without_slack_falls_back_to_log()
    test_alert_with_broken_slack_url_does_not_crash()
    test_escalation_fires_for_old_unacknowledged_p1()
    test_acknowledged_p1_does_not_escalate()
    test_escalation_writes_audit_log()
    test_escalation_cooldown_prevents_repeat_alerts()
    test_outbox_makes_alerts_crash_safe()

    print("\n" + "=" * 74)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("P1 alerts always produce a signal, and unacknowledged ones escalate.")


if __name__ == "__main__":
    main()
