"""
Top-level orchestration: red-flag scan -> (deflect OR extract+classify+file).

Two public entry points, modeling a real two-step conversation:
  start_intake()      — first turn. Either offers a KB article (if a good
                         match exists and nothing dangerous was said) or
                         goes straight to ticket creation.
  file_ticket()        — second turn, called when the user says the KB
                         article didn't help, or when start_intake() already
                         decided a ticket was needed.

Red-flag scanning runs FIRST and unconditionally — a security/outage report
never gets offered a KB article instead of immediate escalation, and it
never depends on extraction or the LLM to be recognized.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from . import alerting, db
from .extraction import extract_incident
from .kb import KBMatch, best_match
from .redflag import scan as redflag_scan
from .rules_engine import Impact, Urgency, resolve_priority

logger = logging.getLogger(__name__)


@dataclass
class IntakeResult:
    outcome: str  # "deflected" | "ticket_created"
    kb_match: Optional[KBMatch] = None
    ticket_id: Optional[int] = None
    priority: Optional[str] = None
    reasoning: Optional[str] = None
    red_flag: bool = False
    alert: Optional[dict] = None


def _create_ticket(text: str, requester: str) -> IntakeResult:
    """
    ORDERING IS DELIBERATE — network I/O never happens inside a DB
    transaction. An earlier version ran extract_incident() (an LLM call, up
    to a 30s timeout) and send_p1_alert() (a Slack call, 10s timeout) inside
    `with db.session()`. Because SQLite allows a single writer, that held the
    write path open across two separate network round-trips: every concurrent
    ticket submission queued behind whichever request was waiting on an
    external API, and a slow LLM provider would stall the entire helpdesk
    rather than just the one request that used it.

    The sequence below keeps each DB transaction to pure local writes:
      1. red-flag scan   (local, instant)
      2. extraction      (NETWORK — outside any transaction)
      3. classify        (local, pure function)
      4. write ticket + audit  (transaction 1, local only)
      5. alert           (NETWORK — outside any transaction)
      6. write alert log (transaction 2, local only)
    """
    # 1. Red-flag scan — local regex, no I/O.
    red_flag = redflag_scan(text)

    # 2. Extraction — may be a network call. Deliberately before any session.
    extracted = extract_incident(text)

    # 3. Classification — pure function, no I/O.
    if red_flag.matched:
        # Red flag overrides the rules engine entirely. Extraction still ran
        # (for category/description on the record), but its impact/urgency
        # output is not consulted for priority.
        priority = "P1"
        reasoning = (
            f"RED-FLAG OVERRIDE: matched {red_flag.category!r} phrase "
            f"{red_flag.matched_phrase!r} — forced P1 regardless of extracted impact/urgency."
        )
    else:
        result = resolve_priority(extracted.impact, extracted.urgency)
        priority = result.priority.value
        reasoning = result.reasoning
        if result.used_safe_default:
            reasoning += " [safe default applied due to unspecified field]"

    # 4. Transaction 1 — local writes only, held for microseconds. The P1
    #    alert INTENT is enqueued here, in the same transaction as the ticket,
    #    so a crash before delivery leaves a recoverable pending row rather
    #    than a ticket nobody was ever paged about.
    with db.session() as conn:
        ticket_id = db.insert_ticket(
            conn,
            requester=requester,
            category=extracted.category,
            affected_system=extracted.affected_system,
            impact=extracted.impact.value,
            urgency=extracted.urgency.value,
            priority=priority,
            description=extracted.description or text,
            reasoning=reasoning,
            red_flag_matched=red_flag.matched,
            red_flag_category=red_flag.category,
            red_flag_phrase=red_flag.matched_phrase,
            extraction_provider=extracted.provider,
        )
        db.log_event(
            conn,
            event_type="ticket_classified",
            ticket_id=ticket_id,
            details={
                "priority": priority,
                "reasoning": reasoning,
                "red_flag": red_flag.matched,
                "red_flag_category": red_flag.category,
                "extraction_provider": extracted.provider,
                "confidence_notes": extracted.confidence_notes,
            },
        )

        outbox_id = None
        if priority == "P1":
            outbox_id = db.enqueue_alert(
                conn,
                ticket_id,
                "new_p1",
                alerting.build_message(ticket_id, extracted.description or text, reasoning),
            )

    # The ticket AND its alert intent are now durably stored. Delivery below is
    # a best-effort fast path for low latency; if it fails or the process dies
    # here, the pending outbox row is picked up by flush_outbox() on the next
    # scheduler tick. An alerting failure can no longer lose the page.
    alert_info = None
    if priority == "P1" and outbox_id is not None:
        try:
            with db.session() as conn:
                row = conn.execute(
                    "SELECT * FROM alert_outbox WHERE id = ?", (outbox_id,)
                ).fetchone()
                row = dict(row) if row else None
            if row:
                # 5. Network call — outside any transaction.
                result = alerting.deliver_outbox_entry(row)
                alert_info = {
                    "channel": result.channel,
                    "sent": result.sent,
                    "attempts": result.attempts,
                }
        except Exception:  # noqa: BLE001 - never let delivery break ticket creation
            logger.exception(
                "Immediate alert delivery failed for ticket %s; left pending in outbox", ticket_id
            )
            alert_info = {"channel": "outbox", "sent": False, "queued": True}

    return IntakeResult(
        outcome="ticket_created",
        ticket_id=ticket_id,
        priority=priority,
        reasoning=reasoning,
        red_flag=red_flag.matched,
        alert=alert_info,
    )


def start_intake(text: str, requester: str = "", allow_deflection: bool = True) -> IntakeResult:
    red_flag = redflag_scan(text)

    if red_flag.matched:
        # Never offer self-service for something red-flagged, regardless of
        # what allow_deflection says — this is not a caller-tunable choice.
        return _create_ticket(text, requester)

    if allow_deflection:
        match = best_match(text)
        if match is not None:
            with db.session() as conn:
                db.log_deflection(conn, query_text=text, kb_article_id=match.article_id, similarity_score=match.score, resolved=None)
            return IntakeResult(outcome="deflected", kb_match=match)

    return _create_ticket(text, requester)


def file_ticket(text: str, requester: str = "") -> IntakeResult:
    """Called when a deflection offer didn't resolve the issue — files a
    ticket unconditionally, bypassing KB search entirely."""
    return _create_ticket(text, requester)


def confirm_deflection_resolved(query_text: str, kb_article_id: int, resolved: bool) -> None:
    """Updates the most recent matching deflection log entry with whether it
    actually resolved the issue — this is the number that makes the ROI case
    to a client (see db.deflection_stats)."""
    with db.session() as conn:
        row = conn.execute(
            """SELECT id FROM deflections
               WHERE query_text = ? AND kb_article_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (query_text, kb_article_id),
        ).fetchone()
        if row:
            conn.execute("UPDATE deflections SET resolved = ? WHERE id = ?", (int(resolved), row["id"]))
