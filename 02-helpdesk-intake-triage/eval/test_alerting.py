"""
Tests for P1 alerting and unacknowledged-ticket escalation.

Run:  python eval/test_alerting.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import alerting, config, db  # noqa: E402

config.LLM_PROVIDER = "none"
config.SLACK_WEBHOOK_URL = ""  # force log-fallback path, no real network call

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
    db.init_db()
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
    db.init_db()
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
    db.init_db()
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
        events = conn.execute(
            "SELECT * FROM audit_log WHERE ticket_id = ? AND event_type = 'escalation_alert'", (ticket_id,)
        ).fetchall()
    check("an escalation_alert audit event exists", len(events) >= 1)


def main():
    print("=" * 74)
    print("Alerting and escalation tests")
    print("=" * 74)

    test_alert_without_slack_falls_back_to_log()
    test_alert_with_broken_slack_url_does_not_crash()
    test_escalation_fires_for_old_unacknowledged_p1()
    test_acknowledged_p1_does_not_escalate()
    test_escalation_writes_audit_log()

    print("\n" + "=" * 74)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("P1 alerts always produce a signal, and unacknowledged ones escalate.")


if __name__ == "__main__":
    main()
