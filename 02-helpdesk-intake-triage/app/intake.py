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
    red_flag = redflag_scan(text)

    with db.session() as conn:
        if red_flag.matched:
            # Red flag overrides the rules engine entirely — extraction still
            # runs (for category/description on the ticket record), but its
            # impact/urgency output is not consulted for priority.
            extracted = extract_incident(text)
            priority = "P1"
            reasoning = (
                f"RED-FLAG OVERRIDE: matched {red_flag.category!r} phrase "
                f"{red_flag.matched_phrase!r} — forced P1 regardless of extracted impact/urgency."
            )
        else:
            extracted = extract_incident(text)
            result = resolve_priority(extracted.impact, extracted.urgency)
            priority = result.priority.value
            reasoning = result.reasoning
            if result.used_safe_default:
                reasoning += " [safe default applied due to unspecified field]"

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

        alert_info = None
        if priority == "P1":
            alert_result = alerting.send_p1_alert(ticket_id, extracted.description or text, reasoning)
            db.log_event(
                conn,
                event_type="p1_alert",
                ticket_id=ticket_id,
                details={"channel": alert_result.channel, "sent": alert_result.sent, "error": alert_result.error},
            )
            alert_info = {"channel": alert_result.channel, "sent": alert_result.sent}

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
