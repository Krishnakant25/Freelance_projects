"""
Red-flag keyword scanner — runs BEFORE the LLM, independent of it.

Per the architecture doc §6 (carried over from the medical version's §6.4):
a missed security incident or org-wide outage is expensive, so this bias is
deliberately asymmetric — it costs a few minutes of human triage time to
double-check a false positive, and that's a fine trade against the cost of
a real incident sitting in the LLM-extraction pipeline instead of paging
someone immediately.

This is a SEPARATE module from rules_engine.py on purpose: "does the text
contain a dangerous phrase" and "given known impact/urgency, what's the
priority" are different concerns, each independently testable. Combining
them into one function is how you end up with a rule that's hard to verify
by reading it.

Matching is intentionally simple (substring/regex on a fixed list), not a
model call — a regex you can read and test beats an LLM classification you
have to trust, for exactly the cases where being wrong is most expensive.
"""
import re
from dataclasses import dataclass, field

# Each entry: (pattern, category). Patterns are case-insensitive.
# THIS LIST IS DOMAIN KNOWLEDGE THE CLIENT HAS, NOT SOMETHING TO INVENT IN A
# VACUUM — see the architecture doc's build sequence step 2. Ship with this
# starter list, then hand it to the client's IT/security lead to extend.
_RED_FLAG_PATTERNS: list[tuple[str, str]] = [
    (r"\bransomware\b", "security"),
    (r"\bdata\s*breach\b", "security"),
    (r"\bdata\s*leak(ed)?\b", "security"),
    (r"\bunauthorized\s+access\b", "security"),
    (r"\bcompromised\s+(account|credentials?|system)\b", "security"),
    (r"\bphishing\b", "security"),
    (r"\bmalware\b", "security"),
    (r"\bvirus\b.{0,15}\b(detected|infection|infected)\b", "security"),
    (r"\bhacked\b", "security"),
    (r"\bsuspicious\s+(login|activity|access)\b", "security"),
    (r"\bexfiltrat", "security"),
    (r"\bcredentials?\s+(stolen|leaked|exposed)\b", "security"),

    (r"\bsystem\s+(is\s+)?down\s+for\s+everyone\b", "outage"),
    (r"\bentire\s+(company|office|team|department)\s+(is\s+)?(down|offline|unable)\b", "outage"),
    (r"\ball\s+(users|employees|staff)\s+(are\s+)?(unable|can'?t|cannot)\b", "outage"),
    (r"\bcompany[- ]wide\s+outage\b", "outage"),
    (r"\bproduction\s+(is\s+)?down\b", "outage"),
    (r"\bcomplete\s+(system\s+)?outage\b", "outage"),
    (r"\bnobody\s+can\s+(access|log\s*in|work)\b", "outage"),

    (r"\bpayroll\s+(is\s+)?(down|broken|failed)\b", "business-critical"),
    (r"\bcan'?t\s+process\s+payments?\b", "business-critical"),
    (r"\border\s+system\s+(is\s+)?down\b", "business-critical"),
]

_COMPILED = [(re.compile(pat, re.IGNORECASE), cat) for pat, cat in _RED_FLAG_PATTERNS]


@dataclass
class RedFlagResult:
    matched: bool
    category: str = ""
    matched_phrase: str = ""
    all_matches: list[tuple[str, str]] = field(default_factory=list)


def scan(text: str) -> RedFlagResult:
    """Scans free text for red-flag phrases. Returns the FIRST match for the
    headline result, but `all_matches` carries everything found — useful for
    the audit log, since a ticket matching multiple categories is worth
    knowing about even though one override is enough to force P1."""
    if not text:
        return RedFlagResult(matched=False)

    matches = []
    for pattern, category in _COMPILED:
        m = pattern.search(text)
        if m:
            matches.append((m.group(0), category))

    if not matches:
        return RedFlagResult(matched=False)

    first_phrase, first_category = matches[0]
    return RedFlagResult(
        matched=True,
        category=first_category,
        matched_phrase=first_phrase,
        all_matches=matches,
    )


def add_pattern(pattern: str, category: str) -> None:
    """Runtime extension point — lets an operator add client-specific red
    flags (product names, internal system names) without touching source.
    Intentionally simple; see MANUAL_STEPS.md for how a client's IT lead
    extends this list."""
    global _COMPILED
    _RED_FLAG_PATTERNS.append((pattern, category))
    _COMPILED.append((re.compile(pattern, re.IGNORECASE), category))
