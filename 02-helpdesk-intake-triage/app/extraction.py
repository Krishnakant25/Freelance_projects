"""
Structured extraction: free-text incident description -> validated fields
consumed by the rules engine.

Pluggable provider, same pattern as the RAG project's app/generate.py:
  none  = rule-based keyword extraction. No key, no cost, always available,
          and — this matters — its failure mode is legible: you can read the
          keyword lists and know exactly why it classified something a
          certain way. Weaker than an LLM at genuinely ambiguous text, but
          never down.
  mock  = deterministic fake, test suite only.
  groq | gemini = real LLM extraction (see MANUAL_STEPS.md).

Whatever the provider, the OUTPUT CONTRACT is the same: an ExtractedIncident
with impact/urgency as Impact/Urgency enums (UNKNOWN is a legitimate value,
not an error) — the rules engine's safe-default handling takes it from there.
Extraction never guesses past what the text actually supports.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Optional

import requests

from . import config
from .rules_engine import Impact, Urgency

CATEGORIES = ["network", "hardware", "software", "access", "email", "other"]


@dataclass
class ExtractedIncident:
    category: str
    affected_system: str
    impact: Impact
    urgency: Urgency
    description: str
    provider: str
    confidence_notes: list[str] = field(default_factory=list)


# --- Rule-based fallback (LLM_PROVIDER=none) --------------------------------

_CATEGORY_KEYWORDS = {
    "network": ["wifi", "wi-fi", "vpn", "network", "internet", "connection", "ethernet", "router"],
    "hardware": ["laptop", "monitor", "mouse", "keyboard", "printer", "device", "computer",
                 "screen", "docking station", "webcam", "headset"],
    "software": ["application", "app ", "crashed", "crashing", "software", "install", "license",
                 "update failed", "won't open", "freezes", "freezing", "bug"],
    "access": ["password", "login", "log in", "access", "permission", "account", "locked out",
                "mfa", "two-factor", "reset my"],
    "email": ["email", "outlook", "inbox", "spam", "mail", "calendar invite"],
}

_ORG_IMPACT_PHRASES = [
    "everyone", "entire company", "whole office", "all employees", "all staff",
    "company-wide", "companywide", "whole organization", "org-wide",
]
_DEPT_IMPACT_PHRASES = [
    "my team", "our team", "our department", "whole team", "several people",
    "a few of us", "my department", "multiple people", "some of my coworkers",
]
_SINGLE_IMPACT_PHRASES = [
    "just me", "only me", "just my", "only my", "i can't", "i cannot", "my laptop",
    "my computer", "my account",
]

_HIGH_URGENCY_PHRASES = [
    "urgent", "critical", "asap", "immediately", "right away", "blocking",
    "can't work", "cannot work", "emergency", "production is down",
]
_LOW_URGENCY_PHRASES = [
    "whenever", "no rush", "not urgent", "low priority", "when you get a chance",
    "can wait", "not blocking",
]


def _match_any(text_lower: str, phrases: list[str]) -> Optional[str]:
    for p in phrases:
        if p in text_lower:
            return p
    return None


def _rule_based_extract(text: str) -> ExtractedIncident:
    text_lower = text.lower()
    notes = []

    category = "other"
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            category = cat
            break
    if category == "other":
        notes.append("no category keyword matched; defaulted to 'other'")

    impact = Impact.UNKNOWN
    if _match_any(text_lower, _ORG_IMPACT_PHRASES):
        impact = Impact.ORGANIZATION
    elif _match_any(text_lower, _DEPT_IMPACT_PHRASES):
        impact = Impact.DEPARTMENT
    elif _match_any(text_lower, _SINGLE_IMPACT_PHRASES):
        impact = Impact.SINGLE_USER
    else:
        notes.append("no impact-scope phrase matched; impact left UNKNOWN (rules engine will apply a safe default)")

    urgency = Urgency.UNKNOWN
    # LOW is checked FIRST, deliberately: several LOW phrases ("not urgent",
    # "no rush") contain a HIGH trigger word ("urgent") as a substring. Plain
    # substring matching has no notion of negation, so checking HIGH first
    # would classify "not urgent" as HIGH — the opposite of what was said.
    # Found by testing: "just my monitor has a weird flicker, not urgent"
    # came out HIGH before this fix. LOW's phrases are the more specific,
    # qualified ones, so resolving them first is the safer order in general,
    # not just a patch for this one case.
    if _match_any(text_lower, _LOW_URGENCY_PHRASES):
        urgency = Urgency.LOW
    elif _match_any(text_lower, _HIGH_URGENCY_PHRASES):
        urgency = Urgency.HIGH
    else:
        notes.append("no urgency phrase matched; urgency left UNKNOWN (rules engine will apply a safe default)")

    # Affected system: best-effort — pull the matched category keyword's
    # surrounding words, or fall back to a generic label. This is genuinely
    # weak; an LLM does much better here. Being honest about that in the
    # confidence notes rather than pretending a keyword grab is a real answer.
    affected_system = category if category != "other" else "unspecified"
    notes.append("affected_system is a coarse category guess in rule-based mode, not a specific system name")

    return ExtractedIncident(
        category=category,
        affected_system=affected_system,
        impact=impact,
        urgency=urgency,
        description=text.strip(),
        provider="none",
        confidence_notes=notes,
    )


# --- LLM-based extraction ----------------------------------------------------

_MOCK_BEHAVIOUR = "normal"


def set_mock_behaviour(behaviour: str) -> None:
    global _MOCK_BEHAVIOUR
    _MOCK_BEHAVIOUR = behaviour


_SYSTEM_PROMPT = """You are an IT helpdesk intake assistant. Extract structured fields from the \
user's incident description. Respond with ONLY a JSON object, no markdown fences:
{
  "category": "<one of: network, hardware, software, access, email, other>",
  "affected_system": "<specific system/app name mentioned, or 'unspecified'>",
  "impact": "<one of: single_user, department, organization, unknown>",
  "urgency": "<one of: low, medium, high, unknown>",
  "description": "<a one-sentence clean summary of the issue>"
}
Rules:
- impact/urgency must be "unknown" if the text genuinely doesn't say — do not guess.
- Base impact/urgency ONLY on what the user actually wrote, never on assumptions.
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object in model output: {text[:200]!r}")
    return json.loads(match.group(0))


def _call_mock(text: str) -> str:
    if _MOCK_BEHAVIOUR == "malformed":
        return "Sorry, I can't format that as JSON right now."
    if _MOCK_BEHAVIOUR == "invalid_enum":
        return json.dumps(
            {
                "category": "not_a_real_category",
                "affected_system": "VPN",
                "impact": "extremely_bad",
                "urgency": "super_high",
                "description": text[:80],
            }
        )
    if _MOCK_BEHAVIOUR == "org_critical":
        return json.dumps(
            {
                "category": "network",
                "affected_system": "VPN",
                "impact": "organization",
                "urgency": "high",
                "description": text[:80],
            }
        )
    # normal: a plausible single-user, medium ticket
    return json.dumps(
        {
            "category": "hardware",
            "affected_system": "laptop",
            "impact": "single_user",
            "urgency": "medium",
            "description": text[:80],
        }
    )


def _call_groq(text: str) -> str:
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        json={
            "model": config.GROQ_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0.0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_gemini(text: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
    )
    resp = requests.post(
        url,
        json={
            "contents": [{"parts": [{"text": f"{_SYSTEM_PROMPT}\n\nUser text: {text}"}]}],
            "generationConfig": {"temperature": 0.0},
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def _safe_enum(value: str, enum_cls, default):
    """Never let a malformed/unexpected LLM enum value silently become a
    valid-but-wrong classification — fall back to the safe default (usually
    UNKNOWN, which the rules engine escalates) instead."""
    try:
        return enum_cls(value)
    except ValueError:
        return default


def extract_incident(text: str) -> ExtractedIncident:
    provider = config.LLM_PROVIDER

    if provider == "none":
        return _rule_based_extract(text)

    try:
        if provider == "mock":
            raw_text = _call_mock(text)
        elif provider == "groq":
            raw_text = _call_groq(text)
        elif provider == "gemini":
            raw_text = _call_gemini(text)
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {provider}")

        raw = _extract_json(raw_text)
        category = raw.get("category", "other")
        if category not in CATEGORIES:
            category = "other"

        impact = _safe_enum(raw.get("impact", "unknown"), Impact, Impact.UNKNOWN)
        urgency = _safe_enum(raw.get("urgency", "unknown"), Urgency, Urgency.UNKNOWN)

        notes = []
        if raw.get("impact") not in [i.value for i in Impact]:
            notes.append(f"model returned invalid impact {raw.get('impact')!r}, defaulted to unknown")
        if raw.get("urgency") not in [u.value for u in Urgency]:
            notes.append(f"model returned invalid urgency {raw.get('urgency')!r}, defaulted to unknown")

        return ExtractedIncident(
            category=category,
            affected_system=raw.get("affected_system", "unspecified") or "unspecified",
            impact=impact,
            urgency=urgency,
            description=raw.get("description", text[:200]) or text[:200],
            provider=provider,
            confidence_notes=notes,
        )
    except Exception as e:  # noqa: BLE001 - any provider/parse failure falls back safely
        result = _rule_based_extract(text)
        result.confidence_notes.insert(0, f"LLM extraction failed ({e}); used rule-based fallback")
        result.provider = f"{provider}(failed)->none"
        return result
