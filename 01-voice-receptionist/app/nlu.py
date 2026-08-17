"""
Intent and entity extraction for the receptionist.

Deliberately rule-based, not an LLM call. Two reasons specific to voice:

  1. LATENCY. Doc §6.1 budgets the whole turn at under ~800ms. An LLM round
     trip spends most of that before the caller hears anything. Intent
     classification for a receptionist is a small closed set — booking,
     reschedule, cancel, FAQ, human — and keyword rules resolve it in
     microseconds with no network dependency.
  2. LEGIBILITY. When the agent mishears "cancel" as "book", you want to read
     the rule that fired, not re-prompt a model.

The tradeoff is real and stated in the README: rule-based NLU is weaker on
unusual phrasing. The mitigation is that ambiguity routes to a clarifying
question or the escape hatch (doc §6.6) rather than to a guess.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class Intent(str, Enum):
    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    FAQ = "faq"
    HUMAN = "human"
    EMERGENCY = "emergency"
    AFFIRM = "affirm"
    DENY = "deny"
    PROVIDE_INFO = "provide_info"
    GOODBYE = "goodbye"
    UNKNOWN = "unknown"


# Ordered by priority — the first matching group wins. EMERGENCY is checked
# before everything because a caller in trouble must never be routed into a
# booking flow (asymmetric error cost, same principle as the helpdesk
# red-flag scanner).
_EMERGENCY_PATTERNS = [
    r"\bemergency\b", r"\burgent(ly)?\b", r"\bsevere pain\b", r"\bbleeding\b",
    r"\bcan'?t breathe\b", r"\bright now\b.{0,20}\bpain\b", r"\bexcruciating\b",
    r"\bswelling\b", r"\baccident\b",
]
_HUMAN_PATTERNS = [
    r"\b(speak|talk) to (a |an )?(human|person|someone|real person|receptionist|staff)\b",
    r"\bhuman being\b", r"\btransfer me\b", r"\bput me through\b",
    r"\bmanager\b", r"\breal person\b", r"\boperator\b",
]
_CANCEL_PATTERNS = [
    r"\bcancel\b", r"\bcall(ing)? off\b", r"\bdon'?t need (my|the) appointment\b",
]
_RESCHEDULE_PATTERNS = [
    r"\breschedul", r"\bmove (my|the) appointment\b", r"\bchange (my|the) appointment\b",
    r"\bdifferent (time|day)\b", r"\bpush (it|my appointment) back\b",
]
_BOOK_PATTERNS = [
    r"\bbook\b", r"\bappointment\b", r"\bschedul", r"\bcome in\b",
    r"\bavailab",
    # "opening" must NOT be bare: "opening hours" is a question about hours, not
    # a request for an available slot. Caught by the replay eval, where
    # "what are your opening hours?" was classified as a booking request and the
    # caller got offered appointment times instead of an answer — despite the
    # FAQ having a 0.84-scoring match for it.
    r"\ban opening\b", r"\bany opening", r"\bopenings\b",
    r"\bslot\b", r"\bmake an appointment\b",
    r"\bsee (the|a) (dentist|doctor)\b", r"\bget in\b",
]
_FAQ_PATTERNS = [
    r"\bwhat (are|is|time)\b", r"\bwhen (are|do) you\b", r"\bhow much\b", r"\bhow long\b",
    r"\bdo you (take|accept|have|offer|treat|see)\b", r"\bdo i need\b",
    r"\bwhere are you\b", r"\bwhere is\b", r"\bopen(ing)? (hours|times?)\b",
    r"\bparking\b", r"\bpark\b", r"\bcost\b", r"\bprice\b", r"\bhow do i\b",
    r"\binsurance\b", r"\baddress\b", r"\bphone number\b", r"\bis there\b",
    r"\bcan i pay\b", r"\bwhat happens if\b",
]
_AFFIRM_PATTERNS = [
    r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|correct|that'?s right|right|please|sounds good|perfect|great|confirm|go ahead)\b",
    r"\bthat works\b", r"\bthat'?s (it|correct|right)\b",
]
_DENY_PATTERNS = [
    r"^\s*(no|nope|nah|negative|incorrect|wrong)\b",
    r"\bthat'?s (not right|wrong|incorrect)\b", r"\bnot (right|correct)\b",
    r"\bdifferent\b",
    # "none of those work" is the most natural way to decline offered times, and
    # it does NOT match `^(no)\b` because "none" continues the word. Missing
    # these meant declining slots was read as confusion and pushed the caller
    # toward the escape hatch instead of showing later availability.
    # This slipped past the replay eval because that script asserted only the
    # final outcome (booked) — which stayed true via the NEXT turn, masking the
    # broken decline. Asserting outcomes alone can hide a broken middle.
    r"\bnone of (those|them|these)\b", r"^\s*none\b", r"\bneither\b",
    r"\bdoesn'?t work\b", r"\bdon'?t work\b", r"\bwon'?t work\b",
    r"\bno good\b", r"\bnot great\b", r"\banything else\b", r"\banything later\b",
    r"\bsomething (else|later|earlier)\b", r"\bnot (those|that one|any of)\b",
    r"\bcan'?t (do|make) (those|that|any)\b",
]
_GOODBYE_PATTERNS = [
    r"\b(good)?bye\b", r"\bthat'?s all\b", r"\bnothing else\b", r"\bthank you,? bye\b",
    r"\bthat'?s everything\b", r"\bwe'?re done\b",
]


def _matches(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


@dataclass
class Extracted:
    intent: Intent
    name: Optional[str] = None
    phone: Optional[str] = None
    digits: Optional[str] = None
    confirmation_code: Optional[str] = None
    datetime_hint: Optional[str] = None
    slot_choice: Optional[int] = None      # 1-based index into offered options
    raw: str = ""
    notes: list[str] = field(default_factory=list)


# --- Entity extraction ----------------------------------------------------

_PHONE_RE = re.compile(r"(\+?\d[\d\s\-().]{6,}\d)")
_CODE_RE = re.compile(r"\b([ACDEFGHJKLMNPQRTUVWXY34679]{6})\b", re.IGNORECASE)
_ORDINALS = {
    "first": 1, "1st": 1, "one": 1, "second": 2, "2nd": 2, "two": 2,
    "third": 3, "3rd": 3, "three": 3, "fourth": 4, "4th": 4, "four": 4,
    "fifth": 5, "5th": 5, "five": 5, "last": -1,
}

_SPOKEN_DIGITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def normalize_phone(text: str) -> Optional[str]:
    """Extracts a phone number, tolerating spoken digits.

    Phone audio is 8kHz narrowband (doc §6.4), so digit strings are exactly
    where STT degrades. This accepts both "555 123 4567" and "five five five
    one two three...". It does NOT try to be clever about partial numbers —
    the dialogue layer reads back whatever this returns for confirmation,
    which is the actual safety mechanism.
    """
    words = re.findall(r"[a-z]+|\d", text.lower())
    spoken = "".join(_SPOKEN_DIGITS.get(w, w if w.isdigit() else "") for w in words)
    if len(spoken) >= 10:
        return spoken

    m = _PHONE_RE.search(text)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        if len(digits) >= 10:
            return digits
    return None


def extract_name(text: str) -> Optional[str]:
    """Pulls a name out of common phrasings. Conservative on purpose: a wrong
    name that gets read back is recoverable, a wrong name silently stored is not."""
    patterns = [
        r"\b(?:my name'?s|my name is|it'?s|this is|i'?m|name:)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})",
        r"\b(?:my name'?s|my name is|it'?s|this is|i'?m)\s+([a-z]+(?:\s+[a-z]+)?)\b",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            candidate = m.group(1).strip()
            # Filter out phrasings that aren't names ("I'm looking for...").
            if candidate.lower().split()[0] in {
                "looking", "calling", "trying", "just", "not", "here", "wondering",
                "hoping", "afraid", "sorry", "good", "fine", "ok", "okay",
            }:
                continue
            return " ".join(w.capitalize() for w in candidate.split())
    return None


# Ordinary English words that happen to be spellable from the confirmation-code
# alphabet. Without this filter, "cancel my appointment" parses "cancel" as
# confirmation code CANCEL, the lookup fails, and the caller gets an unnecessary
# "I couldn't find that" — found by auditing the code regex against real phrasings.
_CODE_FALSE_POSITIVES = {
    "CANCEL", "CHANGE", "PLEASE", "THANKS", "APPOINT", "NUMBER", "MARTHA",
    "DOCTOR", "FRIDAY", "MONDAY", "AFTERN", "MORNIN", "URGENT", "HELLO",
    "LATER", "EARLY", "CHECK", "PHONE", "NAMEIS", "CALLED", "WANTED",
}


def extract_confirmation_code(text: str) -> Optional[str]:
    """Pulls a 6-character confirmation code out of an utterance.

    Deliberately conservative. A bare 6-letter match is only accepted when it's
    plausibly a code rather than an ordinary word: either it's introduced by
    "code", it's the whole utterance, or it isn't a known English word from the
    same alphabet.
    """
    # Strongest signal: explicitly introduced.
    m = re.search(
        r"\b(?:code|reference|ref)\b[^A-Za-z0-9]{0,10}([ACDEFGHJKLMNPQRTUVWXY34679]{6})\b",
        text, re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()

    stripped = re.sub(r"[^A-Za-z0-9]", "", text)
    # The whole utterance is the code (how callers usually answer "what's your code?").
    if len(stripped) == 6 and _CODE_RE.fullmatch(stripped):
        return stripped.upper()

    for candidate in _CODE_RE.findall(text):
        upper = candidate.upper()
        if upper in _CODE_FALSE_POSITIVES:
            continue
        # A code contains at least one digit in practice far more often than an
        # English word does; require either a digit or an unusual letter run.
        if any(ch.isdigit() for ch in upper):
            return upper
        # All-letters candidate: only trust it if the original was uppercase
        # (typed/DTMF) or it isn't a pronounceable word shape.
        if candidate.isupper():
            return upper
    return None


def extract_slot_choice(text: str) -> Optional[int]:
    """Resolves which offered option the caller picked.

    A BARE DIGIT must work ("1", "2"). This isn't a nicety: the agent explicitly
    invites "press it on your keypad" as the DTMF fallback for noisy lines
    (doc §6.4), and DTMF delivers exactly a bare digit. Recognising only
    "first" / "option 1" meant the fallback the agent offered didn't work —
    caught by test_booking_gate, where a caller answering "1" fell through to
    the confusion escape hatch instead of booking.
    """
    lowered = text.lower().strip()

    # Bare digit (spoken or keypad), possibly with light punctuation.
    m = re.fullmatch(r"[^\w]*([1-9])[^\w]*", lowered)
    if m:
        return int(m.group(1))

    for word, idx in _ORDINALS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return idx

    m = re.search(r"\b(?:option|number|choice)\s*(\d)\b", lowered)
    if m:
        return int(m.group(1))

    # "I'll take 2" / "let's do 3" — a digit in an otherwise short utterance.
    if len(lowered.split()) <= 5:
        m = re.search(r"\b([1-9])\b", lowered)
        if m:
            return int(m.group(1))
    return None


def extract_datetime_hint(text: str) -> Optional[str]:
    """Returns a coarse hint, not a parsed datetime.

    Deliberate: resolving "next Tuesday afternoon" to an exact slot is the
    calendar's job (it knows what's actually free). The agent offers real
    options rather than guessing a time and then discovering it's taken.
    """
    lowered = text.lower()
    hints = []
    for kw in ["today", "tomorrow", "this week", "next week", "morning", "afternoon", "evening", "asap", "as soon as possible"]:
        if kw in lowered:
            hints.append(kw)
    for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
        if day in lowered:
            hints.append(day)
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", lowered)
    if m:
        hints.append(m.group(0))
    return " ".join(hints) if hints else None


def understand(text: str, expecting: Optional[str] = None) -> Extracted:
    """Classifies a caller utterance.

    `expecting` biases interpretation toward whatever the agent just asked for,
    which is how a bare "Sarah Chen" or "yes" gets read correctly instead of
    falling through to UNKNOWN.
    """
    raw = (text or "").strip()
    if not raw:
        return Extracted(intent=Intent.UNKNOWN, raw=raw, notes=["empty utterance"])

    digits_only = re.sub(r"\D", "", raw)
    result = Extracted(intent=Intent.UNKNOWN, raw=raw)
    result.phone = normalize_phone(raw)
    result.name = extract_name(raw)
    result.slot_choice = extract_slot_choice(raw)
    result.datetime_hint = extract_datetime_hint(raw)
    result.confirmation_code = extract_confirmation_code(raw)
    if digits_only and len(digits_only) <= 4 and len(digits_only) == len(raw.strip()):
        result.digits = digits_only  # DTMF keypad entry

    # Emergency first — never route distress into a booking flow.
    if _matches(raw, _EMERGENCY_PATTERNS):
        result.intent = Intent.EMERGENCY
        return result
    if _matches(raw, _HUMAN_PATTERNS):
        result.intent = Intent.HUMAN
        return result

    # CLOSED yes/no questions ("is that right?", "shall I cancel it?"): a bare
    # affirm/deny is the answer, ahead of any keyword in the same sentence.
    # "yes please cancel it" is confirming, not requesting.
    if expecting in {"confirm_slot", "confirm_details", "confirm_phone", "consent"}:
        if _matches(raw, _AFFIRM_PATTERNS):
            result.intent = Intent.AFFIRM
            return result
        if _matches(raw, _DENY_PATTERNS):
            result.intent = Intent.DENY
            return result

    # OPEN questions ("anything else?"): a NEW intent is expected, so a
    # substantive request must win over a leading pleasantry.
    #
    # Caught by the replay eval: "great, can I book an appointment then" was
    # classified as a bare AFFIRM because "great" matches an affirm pattern at
    # the start of the string — discarding the actual booking request and
    # eventually dumping the caller to a callback. Treating open and closed
    # questions the same way is the underlying mistake; they need different
    # precedence.
    if expecting == "anything_else":
        if _matches(raw, _CANCEL_PATTERNS):
            result.intent = Intent.CANCEL
            return result
        if _matches(raw, _RESCHEDULE_PATTERNS):
            result.intent = Intent.RESCHEDULE
            return result
        if _matches(raw, _BOOK_PATTERNS):
            result.intent = Intent.BOOK
            return result
        if _matches(raw, _GOODBYE_PATTERNS):
            result.intent = Intent.GOODBYE
            return result
        if _matches(raw, _FAQ_PATTERNS):
            result.intent = Intent.FAQ
            return result
        if _matches(raw, _DENY_PATTERNS):
            result.intent = Intent.DENY
            return result
        if _matches(raw, _AFFIRM_PATTERNS):
            result.intent = Intent.AFFIRM
            return result

    if _matches(raw, _CANCEL_PATTERNS):
        result.intent = Intent.CANCEL
        return result
    if _matches(raw, _RESCHEDULE_PATTERNS):
        result.intent = Intent.RESCHEDULE
        return result
    if _matches(raw, _GOODBYE_PATTERNS):
        result.intent = Intent.GOODBYE
        return result
    if _matches(raw, _BOOK_PATTERNS):
        result.intent = Intent.BOOK
        return result
    if _matches(raw, _FAQ_PATTERNS):
        result.intent = Intent.FAQ
        return result
    if _matches(raw, _AFFIRM_PATTERNS):
        result.intent = Intent.AFFIRM
        return result
    if _matches(raw, _DENY_PATTERNS):
        result.intent = Intent.DENY
        return result

    # If we were expecting a specific piece of information and got something
    # that looks like it, treat that as the answer rather than "unknown".
    if expecting == "name" and (result.name or len(raw.split()) <= 4):
        result.intent = Intent.PROVIDE_INFO
        if not result.name:
            result.name = " ".join(w.capitalize() for w in raw.split()[:3])
        return result
    if expecting == "phone" and (result.phone or result.digits):
        result.intent = Intent.PROVIDE_INFO
        return result
    if expecting == "slot_choice" and (result.slot_choice or result.datetime_hint):
        result.intent = Intent.PROVIDE_INFO
        return result
    if expecting == "confirmation_code" and result.confirmation_code:
        result.intent = Intent.PROVIDE_INFO
        return result

    if result.phone or result.name or result.confirmation_code or result.slot_choice:
        result.intent = Intent.PROVIDE_INFO
        return result

    result.notes.append("no intent pattern matched")
    return result
