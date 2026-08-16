"""
Deterministic priority rules engine — ITIL-style Impact x Urgency matrix.

This is the module the whole project's credibility rests on. Per the
architecture doc: the LLM extracts, this engine decides. Nobody should ever
be able to ask "why is this a P1" and get "the AI thought so" as the answer —
they should get a row/column lookup they can check by hand.

Design rules, all deliberate:
- Pure functions, no I/O, no randomness, no model calls. Fully unit-testable,
  and tested exhaustively (every cell of the matrix) in eval/test_rules_engine.py.
- Unknown/missing input is NEVER silently treated as low priority. Missing
  data escalates, on the same "asymmetric error cost" logic as the medical
  version of this project: a wrongly-escalated ticket costs a few minutes of
  a human's time, a wrongly-buried one costs an outage nobody noticed.
- Red-flag override lives in a SEPARATE module (redflag.py) and runs before
  this engine, not inside it — keeping "deterministic classification" and
  "hard override on dangerous keywords" as two independently testable
  concerns rather than one entangled function.
"""
from dataclasses import dataclass
from enum import Enum


class Impact(str, Enum):
    SINGLE_USER = "single_user"
    DEPARTMENT = "department"
    ORGANIZATION = "organization"
    UNKNOWN = "unknown"


class Urgency(str, Enum):
    LOW = "low"          # can wait, not blocking work
    MEDIUM = "medium"     # blocking the requester's work
    HIGH = "high"         # blocking multiple people / critical function
    UNKNOWN = "unknown"


class Priority(str, Enum):
    P1 = "P1"  # critical — page immediately
    P2 = "P2"  # high — same-business-day
    P3 = "P3"  # medium — this week
    P4 = "P4"  # low — backlog


@dataclass
class PriorityResult:
    priority: Priority
    reasoning: str
    used_safe_default: bool  # True if impact/urgency were unknown and escalated


# The matrix. Rows = Impact, columns = Urgency. This is the auditable core:
# change a client's actual priority policy by editing this table, not by
# reasoning about it. UNKNOWN is not a row/column — it's resolved to a safe
# value before lookup (see resolve_priority()).
_MATRIX: dict[tuple[Impact, Urgency], Priority] = {
    (Impact.SINGLE_USER, Urgency.LOW): Priority.P4,
    (Impact.SINGLE_USER, Urgency.MEDIUM): Priority.P3,
    (Impact.SINGLE_USER, Urgency.HIGH): Priority.P2,
    (Impact.DEPARTMENT, Urgency.LOW): Priority.P3,
    (Impact.DEPARTMENT, Urgency.MEDIUM): Priority.P2,
    (Impact.DEPARTMENT, Urgency.HIGH): Priority.P1,
    (Impact.ORGANIZATION, Urgency.LOW): Priority.P2,
    (Impact.ORGANIZATION, Urgency.MEDIUM): Priority.P1,
    (Impact.ORGANIZATION, Urgency.HIGH): Priority.P1,
}

# What "unknown" resolves to before hitting the matrix. Chosen to be the
# WORSE (higher-priority) of the two known values along that axis, never the
# best-case assumption — this is the "escalate on ambiguity" rule made concrete.
_UNKNOWN_IMPACT_RESOLVES_TO = Impact.DEPARTMENT
_UNKNOWN_URGENCY_RESOLVES_TO = Urgency.MEDIUM


def resolve_priority(impact: Impact, urgency: Urgency) -> PriorityResult:
    used_safe_default = False
    resolved_impact = impact
    resolved_urgency = urgency

    if impact == Impact.UNKNOWN:
        resolved_impact = _UNKNOWN_IMPACT_RESOLVES_TO
        used_safe_default = True
    if urgency == Urgency.UNKNOWN:
        resolved_urgency = _UNKNOWN_URGENCY_RESOLVES_TO
        used_safe_default = True

    key = (resolved_impact, resolved_urgency)
    if key not in _MATRIX:
        # Defensive: should be unreachable if Impact/Urgency enums stay in
        # sync with the matrix, but a silently-missing cell defaulting to
        # "low priority" would be exactly the kind of bug this module exists
        # to prevent. Fail loud instead.
        raise ValueError(
            f"No matrix entry for resolved (impact={resolved_impact}, urgency={resolved_urgency}). "
            "This means the matrix is out of sync with the Impact/Urgency enums — fix _MATRIX."
        )

    priority = _MATRIX[key]

    reasoning_parts = [f"Impact={resolved_impact.value}", f"Urgency={resolved_urgency.value}"]
    if impact == Impact.UNKNOWN:
        reasoning_parts.append(f"(impact was unspecified, defaulted to {resolved_impact.value} for safety)")
    if urgency == Urgency.UNKNOWN:
        reasoning_parts.append(f"(urgency was unspecified, defaulted to {resolved_urgency.value} for safety)")
    reasoning_parts.append(f"-> {priority.value}")
    reasoning = " ".join(reasoning_parts)

    return PriorityResult(priority=priority, reasoning=reasoning, used_safe_default=used_safe_default)
