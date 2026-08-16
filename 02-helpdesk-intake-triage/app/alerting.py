"""
Alerting for P1 tickets, plus escalation for ones nobody acknowledged.

Per the architecture doc's carried-over lesson (§6 / medical doc §6.5): "the
ticket is in a priority-sorted queue" is not the same as "a human knows
about it." A P1 must produce an actual page, and if nobody acknowledges it
within a time budget, that has to escalate rather than sit there.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from . import config, db

logger = logging.getLogger(__name__)


@dataclass
class AlertResult:
    sent: bool
    channel: str  # "slack" | "log"
    error: Optional[str] = None


def send_p1_alert(ticket_id: int, description: str, reasoning: str, escalation: bool = False) -> AlertResult:
    prefix = "ESCALATION — unacknowledged P1" if escalation else "NEW P1"
    message = f"[{prefix}] Ticket #{ticket_id}: {description[:200]}\nClassification: {reasoning}"

    if config.SLACK_WEBHOOK_URL:
        try:
            resp = requests.post(config.SLACK_WEBHOOK_URL, json={"text": message}, timeout=10)
            resp.raise_for_status()
            return AlertResult(sent=True, channel="slack")
        except Exception as e:  # noqa: BLE001 - alerting must never crash the caller
            logger.error("Slack alert failed for ticket #%s: %s. Message: %s", ticket_id, e, message)
            return AlertResult(sent=False, channel="slack", error=str(e))

    # No Slack configured — log at ERROR so it's impossible to miss in any
    # log aggregator, rather than silently doing nothing.
    logger.error("P1 ALERT (no Slack configured): %s", message)
    return AlertResult(sent=True, channel="log")


def check_escalations() -> list[dict]:
    """Finds P1 tickets still 'open' (never acknowledged) past the
    escalation window and re-alerts on them. Meant to be run on a schedule
    (cron, or a loop) in production — see MANUAL_STEPS.md."""
    escalated = []
    with db.session() as conn:
        rows = conn.execute(
            f"""SELECT * FROM tickets
                WHERE priority = 'P1' AND status = 'open'
                AND created_at <= datetime('now', '-{config.P1_ESCALATION_MINUTES} minutes')"""
        ).fetchall()
        for row in rows:
            result = send_p1_alert(row["id"], row["description"], row["reasoning"], escalation=True)
            db.log_event(
                conn,
                event_type="escalation_alert",
                ticket_id=row["id"],
                details={"channel": result.channel, "sent": result.sent, "error": result.error},
            )
            escalated.append({"ticket_id": row["id"], "channel": result.channel, "sent": result.sent})
    return escalated
