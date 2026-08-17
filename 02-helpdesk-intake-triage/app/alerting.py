"""
P1 alerting: durable outbox + delivery, and escalation for unacknowledged
tickets.

DESIGN — two properties this module exists to guarantee:

1. An alert that was owed is never silently lost. The intent is written to
   `alert_outbox` in the SAME transaction as the ticket, so it survives a
   crash between "ticket created" and "page sent". Delivery is a separate,
   retryable step. A permanently undeliverable alert becomes a `failed` row
   that an operator can see via /ready — not a gap in the logs.

2. Alerting does not become noise. Escalation is rate-limited per ticket by a
   cooldown, because the previous version re-alerted on every invocation:
   with a periodic scheduler an unacknowledged P1 would page every tick
   forever, which trains people to ignore the channel — the exact opposite of
   what a P1 alert is for.
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

from . import config, db

logger = logging.getLogger(__name__)


@dataclass
class DeliveryResult:
    sent: bool
    channel: str  # "slack" | "log"
    attempts: int = 1
    error: Optional[str] = None


def build_message(ticket_id: int, description: str, reasoning: str, escalation: bool = False) -> str:
    prefix = "ESCALATION — unacknowledged P1" if escalation else "NEW P1"
    return f"[{prefix}] Ticket #{ticket_id}: {description[:200]}\nClassification: {reasoning}"


def _deliver(message: str, ticket_id: int) -> DeliveryResult:
    """Attempts actual delivery. Never raises."""
    if not config.SLACK_WEBHOOK_URL:
        # No Slack configured — log at ERROR so it's impossible to miss in a
        # log aggregator, rather than silently doing nothing. This counts as
        # delivered: the signal reached somewhere a human can see it.
        logger.error("P1 ALERT (no Slack configured): %s", message)
        return DeliveryResult(sent=True, channel="log")

    last_error = None
    for attempt in range(1, config.ALERT_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                config.SLACK_WEBHOOK_URL,
                json={"text": message},
                timeout=config.ALERT_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            return DeliveryResult(sent=True, channel="slack", attempts=attempt)
        except Exception as e:  # noqa: BLE001 - alerting must never crash the caller
            last_error = str(e)
            logger.warning(
                "Slack alert attempt %d/%d failed for ticket #%s: %s",
                attempt, config.ALERT_MAX_ATTEMPTS, ticket_id, e,
            )
            if attempt < config.ALERT_MAX_ATTEMPTS:
                time.sleep(min(2 ** (attempt - 1), 4))

    logger.error(
        "P1 ALERT DELIVERY FAILED after %d attempts for ticket #%s (%s). Message: %s",
        config.ALERT_MAX_ATTEMPTS, ticket_id, last_error, message,
    )
    return DeliveryResult(
        sent=False, channel="slack", attempts=config.ALERT_MAX_ATTEMPTS, error=last_error
    )


def deliver_outbox_entry(row) -> DeliveryResult:
    """Delivers one outbox row and records the outcome."""
    result = _deliver(row["message"], row["ticket_id"])
    total_attempts = row["attempts"] + result.attempts
    with db.session() as conn:
        if result.sent:
            db.mark_alert_sent(conn, row["id"], total_attempts)
        else:
            give_up = total_attempts >= config.ALERT_MAX_TOTAL_ATTEMPTS
            db.mark_alert_attempt_failed(
                conn, row["id"], total_attempts, result.error or "unknown", give_up=give_up
            )
            if give_up:
                logger.error(
                    "Alert for ticket #%s permanently FAILED after %d total attempts — "
                    "visible in /ready as outbox.failed",
                    row["ticket_id"], total_attempts,
                )
        db.log_event(
            conn,
            event_type="alert_delivery",
            ticket_id=row["ticket_id"],
            details={
                "outbox_id": row["id"],
                "kind": row["kind"],
                "sent": result.sent,
                "channel": result.channel,
                "attempts": total_attempts,
                "error": result.error,
            },
        )
    return result


def flush_outbox(limit: int = 50) -> dict:
    """Delivers pending alerts. Safe to call repeatedly; this is what makes a
    crash mid-alert recoverable rather than a lost page."""
    with db.session() as conn:
        rows = [dict(r) for r in db.pending_alerts(conn, limit=limit)]
    sent = failed = 0
    for row in rows:
        result = deliver_outbox_entry(row)
        if result.sent:
            sent += 1
        else:
            failed += 1
    if rows:
        logger.info("Outbox flush: %d sent, %d failed", sent, failed)
    return {"processed": len(rows), "sent": sent, "failed": failed}


def check_escalations() -> list[dict]:
    """Enqueues escalation alerts for P1 tickets left unacknowledged past the
    window, then flushes the outbox.

    Two behaviours worth noting:

    - Read / enqueue / deliver are separate phases. An earlier version looped
      Slack calls while holding a DB session open; with SQLite's single writer
      that blocked all concurrent ticket submissions for the duration.

    - A per-ticket COOLDOWN prevents re-alerting the same ticket on every
      invocation. Without it, a scheduler running every minute pages every
      minute until someone acknowledges.
    """
    with db.session() as conn:
        candidates = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM tickets
                   WHERE priority = 'P1' AND status = 'open'
                     AND created_at <= datetime('now', ? || ' minutes')""",
                (f"-{int(config.P1_ESCALATION_MINUTES)}",),
            ).fetchall()
        ]

        enqueued = []
        for row in candidates:
            since = db.minutes_since_last_escalation(conn, row["id"])
            if since is not None and since < config.ESCALATION_COOLDOWN_MINUTES:
                # Already paged recently — stay quiet rather than repeat.
                continue
            message = build_message(row["id"], row["description"], row["reasoning"], escalation=True)
            outbox_id = db.enqueue_alert(conn, row["id"], "escalation", message)
            db.log_event(
                conn,
                event_type="escalation_enqueued",
                ticket_id=row["id"],
                details={"outbox_id": outbox_id, "minutes_since_last": since},
            )
            enqueued.append({"ticket_id": row["id"], "outbox_id": outbox_id})

    if enqueued:
        flush_outbox()
    return enqueued


# --- Backwards-compatible direct-send helper -------------------------------


def send_p1_alert(ticket_id: int, description: str, reasoning: str, escalation: bool = False) -> DeliveryResult:
    """Direct send without the outbox.

    Retained for callers that want an immediate best-effort page and are not
    inside a transaction that can enqueue durably. The intake path does NOT
    use this — it enqueues to the outbox instead, which is the crash-safe
    route. Prefer enqueue + flush_outbox() for anything that matters.
    """
    return _deliver(build_message(ticket_id, description, reasoning, escalation), ticket_id)
