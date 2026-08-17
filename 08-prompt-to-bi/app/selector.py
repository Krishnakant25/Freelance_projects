"""
Question -> Selection. The only place natural language is interpreted.

The critical property: this function can FAIL CLEANLY. If the question asks for
something the semantic model doesn't define, it returns a Refusal naming what IS
available — it does not guess a join, invent a metric, or answer a different
question than the one asked.

That's the capability free-form NL→SQL structurally lacks: a model asked to
write SQL will always write *some* SQL. Architecture doc §6.1.

Providers:
  none  = rule-based synonym matching over the model vocabulary. No key, no
          cost, and legible — you can read exactly why a word mapped to a
          metric. Weaker on unusual phrasing than an LLM.
  mock  = deterministic fake for tests.
  groq | gemini = LLM selection, still constrained: the model returns metric and
          dimension NAMES from the provided vocabulary, and anything outside it
          is rejected by validate_selection() rather than trusted.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import requests

from . import config
from .semantic import (
    Filter,
    Selection,
    SelectionError,
    SemanticModel,
    validate_selection,
)

logger = logging.getLogger(__name__)


@dataclass
class Refusal:
    """A clean "I can't answer that" with the information needed to ask a better
    question. This is a first-class result, not an error path."""
    reason: str
    message: str
    available_metrics: list[str] = field(default_factory=list)
    available_dimensions: list[str] = field(default_factory=list)
    unmatched_terms: list[str] = field(default_factory=list)


@dataclass
class Clarification:
    """Returned when a term maps to more than one metric. Asking beats guessing:
    doc §6.3 — "revenue" meaning gross vs net silently produces a defensible
    number under one definition and a wrong one under the other."""
    question: str
    options: list[str]
    term: str = ""


SelectorResult = Selection  # or Refusal / Clarification


# --- Rule-based selection -------------------------------------------------

# Terms that indicate a specific date range in the question.
_DATE_RANGE_PATTERNS = [
    (r"\byesterday\b", "yesterday"),
    (r"\btoday\b", "today"),
    (r"\blast month\b|\bprevious month\b", "last_month"),
    (r"\bthis month\b|\bmonth to date\b|\bmtd\b", "this_month"),
    (r"\blast 7 days\b|\bpast week\b|\blast week\b|\bpast 7 days\b", "last_7_days"),
    (r"\blast 30 days\b|\bpast 30 days\b|\blast thirty days\b", "last_30_days"),
    (r"\blast quarter\b|\blast 90 days\b|\bpast quarter\b", "last_quarter"),
    (r"\bthis year\b|\byear to date\b|\bytd\b", "this_year"),
    (r"\ball time\b|\ball-time\b|\bever\b|\boverall\b|\btotal\b", "all_time"),
]

_GRAIN_PATTERNS = [
    (r"\bby day\b|\bdaily\b|\bper day\b|\beach day\b", "day"),
    (r"\bby week\b|\bweekly\b|\bper week\b", "week"),
    (r"\bby month\b|\bmonthly\b|\bper month\b", "month"),
    (r"\bby year\b|\byearly\b|\bannually\b|\bper year\b", "year"),
]

_TREND_HINTS = [r"\bover time\b", r"\btrend\b", r"\bby day\b", r"\bdaily\b",
                r"\bby week\b", r"\bweekly\b", r"\bby month\b", r"\bmonthly\b"]

# Known dimension VALUES, so "revenue from mobile app" becomes a filter rather
# than being ignored. Kept explicit rather than inferred from the warehouse so
# a question can't accidentally probe live data during interpretation.
_KNOWN_VALUES = {
    "channel": ["web", "mobile_app", "marketplace", "phone"],
    "region": ["North America", "EMEA", "APAC", "LATAM"],
    "customer_segment": ["consumer", "business", "enterprise"],
    "product_category": ["Electronics", "Home", "Apparel", "Outdoors", "Beauty"],
    "payment_method": ["card", "paypal", "bank_transfer", "gift_card"],
}

_VALUE_ALIASES = {
    "mobile": "mobile_app", "mobile app": "mobile_app", "app": "mobile_app",
    "website": "web", "online": "web",
    "us": "North America", "usa": "North America", "north america": "North America",
    "europe": "EMEA", "emea": "EMEA", "asia": "APAC", "apac": "APAC",
    "latam": "LATAM", "latin america": "LATAM",
    "b2b": "business", "b2c": "consumer",
    "credit card": "card", "cards": "card",
}


def _match_terms(text: str, model: SemanticModel) -> tuple[list[str], list[str], list[str]]:
    """Returns (metric_names, dimension_names, ambiguous_terms)."""
    lowered = f" {text.lower()} "
    metrics: list[str] = []
    dimensions: list[str] = []
    ambiguous: list[str] = []

    # Longest synonyms first so "average order value" wins over "order".
    metric_terms: list[tuple[str, str]] = []
    for m in model.metrics.values():
        for syn in [m.name.replace("_", " "), m.name] + m.synonyms:
            metric_terms.append((syn.lower(), m.name))
    metric_terms.sort(key=lambda t: -len(t[0]))

    consumed_spans: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in consumed_spans)

    for term, metric_name in metric_terms:
        for match in re.finditer(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered):
            if overlaps(match.start(), match.end()):
                continue
            if metric_name not in metrics:
                metrics.append(metric_name)
            consumed_spans.append((match.start(), match.end()))
            break

    dim_terms: list[tuple[str, str]] = []
    for d in model.dimensions.values():
        for syn in [d.name.replace("_", " "), d.name] + d.synonyms:
            dim_terms.append((syn.lower(), d.name))
    dim_terms.sort(key=lambda t: -len(t[0]))

    for term, dim_name in dim_terms:
        for match in re.finditer(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered):
            if overlaps(match.start(), match.end()):
                continue
            if dim_name not in dimensions:
                dimensions.append(dim_name)
            consumed_spans.append((match.start(), match.end()))
            break

    return metrics, dimensions, ambiguous


def _match_date_range(text: str) -> Optional[str]:
    lowered = text.lower()
    for pattern, name in _DATE_RANGE_PATTERNS:
        if re.search(pattern, lowered):
            return name
    return None


def _match_grain(text: str) -> Optional[str]:
    lowered = text.lower()
    for pattern, name in _GRAIN_PATTERNS:
        if re.search(pattern, lowered):
            return name
    return None


def _wants_trend(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(p, lowered) for p in _TREND_HINTS)


_BREAKDOWN_KEYWORDS = r"\b(?:by|per|broken\s+down\s+by|split\s+by|across)\b"


def _dimensions_after_breakdown_keyword(question: str, model: SemanticModel) -> set[str]:
    """Dimensions the user actually asked to GROUP BY.

    Only the text following a breakdown keyword ("by", "per", "split by", …) is
    considered, and only up to the next clause boundary — so in
    "revenue by region from the phone channel", `region` is a grouping and
    `channel` is not, even though both dimension words appear.
    """
    lowered = question.lower()
    requested: set[str] = set()

    for match in re.finditer(_BREAKDOWN_KEYWORDS, lowered):
        # Look at the span after the keyword, stopping at a clause boundary that
        # signals a filter or time phrase rather than another grouping.
        tail = lowered[match.end():]
        boundary = re.search(r"\b(?:from|for|in|with|where|during|last|this|yesterday|today)\b", tail)
        segment = tail[: boundary.start()] if boundary else tail

        for dim in model.dimensions.values():
            names = [dim.name.replace("_", " "), dim.name] + dim.synonyms
            for term in names:
                if re.search(rf"(?<![a-z]){re.escape(term.lower())}(?![a-z])", segment):
                    requested.add(dim.name)
                    break
    return requested


def _match_filters(text: str, model: SemanticModel) -> list[Filter]:
    lowered = text.lower()
    filters: list[Filter] = []
    for dim_name, values in _KNOWN_VALUES.items():
        if dim_name not in model.dimensions:
            continue
        matched: list[str] = []
        for value in values:
            if re.search(rf"(?<![a-z]){re.escape(value.lower())}(?![a-z])", lowered):
                matched.append(value)
        for alias, canonical in _VALUE_ALIASES.items():
            if canonical in values and re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
                if canonical not in matched:
                    matched.append(canonical)
        if matched:
            filters.append(Filter(dimension=dim_name, operator="in", values=matched))
    return filters


def _mask_time_phrases(text: str) -> str:
    """Blanks out date-range and grain phrases before dimension matching.

    WHY: date phrases contain words that are also dimension synonyms. "last
    month" contains "month", a synonym for the order_date dimension — so
    "what was our revenue last month?" was parsed as "group revenue by month"
    and returned 31 daily rows instead of the single number asked for. Same for
    "this year" and "last week".

    A time FILTER and a time GROUPING are different requests, and the phrase
    expressing one must not be readable as the other. Masking applies only to a
    copy used for term matching; grain and range are captured separately from
    the original text.
    """
    masked = text
    for pattern, _name in _DATE_RANGE_PATTERNS:
        masked = re.sub(pattern, " ", masked, flags=re.IGNORECASE)
    for pattern, _name in _GRAIN_PATTERNS:
        # "by month" genuinely DOES imply grouping, but the grain is captured by
        # _match_grain and the time dimension is added by the trend check below —
        # leaving the words here would double-count them as a dimension match.
        masked = re.sub(pattern, " ", masked, flags=re.IGNORECASE)
    return masked


def _rule_based_select(question: str, model: SemanticModel):
    # Match against text with time phrases removed, so a date filter cannot
    # masquerade as a grouping request.
    metrics, dimensions, _ = _match_terms(_mask_time_phrases(question), model)

    if not metrics:
        # No metric matched — refuse and say what's available. This is the
        # behaviour that free-form SQL generation cannot offer.
        words = [w for w in re.findall(r"[a-z]{4,}", question.lower())]
        return Refusal(
            reason="no_metric_matched",
            message=(
                "I don't have a metric that answers that. I can only report on "
                "metrics that have been defined in the semantic model."
            ),
            available_metrics=model.metric_names(),
            available_dimensions=model.dimension_names(),
            unmatched_terms=words[:6],
        )

    assumptions: list[str] = []
    date_range = _match_date_range(question)
    grain = _match_grain(question)

    # "revenue over time" implies the time dimension even without "by day".
    if _wants_trend(question) and "order_date" not in dimensions:
        dimensions.insert(0, "order_date")

    filters = _match_filters(question, model)

    # A dimension used purely as a FILTER must not also become a GROUP BY.
    #
    # The earlier version only checked whether the question contained "by"
    # anywhere, which broke on "revenue by region from the phone channel":
    # the literal word "channel" matched the channel dimension, a "by" was
    # present, so it grouped by channel AND region — answering a different
    # question than the one asked.
    #
    # A dimension is a grouping only if it is actually named in the
    # breakdown clause. Everything else that produced a filter is filter-only.
    filtered_dims = {f.dimension for f in filters}
    if filtered_dims:
        requested_groupings = _dimensions_after_breakdown_keyword(question, model)
        dimensions = [
            d for d in dimensions
            if d not in filtered_dims or d in requested_groupings
        ]

    order_by = metrics[0] if dimensions and not any(
        model.dimensions[d].time_dimension for d in dimensions
    ) else None

    limit = None
    m = re.search(r"\btop\s+(\d{1,3})\b", question.lower())
    if m:
        limit = int(m.group(1))

    selection = Selection(
        metrics=metrics,
        dimensions=dimensions,
        date_range=date_range or "",
        time_grain=grain or "",
        filters=filters,
        order_by_metric=order_by,
        limit=limit,
        assumptions=assumptions,
    )
    return validate_selection(selection, model)


# --- LLM selection --------------------------------------------------------

_SYSTEM_PROMPT = """You translate a business question into a SELECTION over a \
fixed semantic model. You do NOT write SQL.

Respond with ONLY a JSON object, no markdown fences:
{
  "metrics": ["<metric_name>", ...],
  "dimensions": ["<dimension_name>", ...],
  "date_range": "<date_range_name or empty>",
  "time_grain": "<day|week|month|year or empty>",
  "filters": [{"dimension": "<dimension_name>", "operator": "in", "values": ["..."]}],
  "limit": <int or null>,
  "refuse": false,
  "refuse_reason": ""
}

Rules:
- Use ONLY the metric, dimension, date_range and time_grain names listed below.
- If the question needs a metric that is NOT listed, set "refuse": true and
  explain what's missing in refuse_reason. Do NOT substitute a different metric.
- Never invent names. Never output SQL.
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in model output: {text[:200]!r}")
    return json.loads(match.group(0))


_MOCK_BEHAVIOUR = "normal"


def set_mock_behaviour(behaviour: str) -> None:
    global _MOCK_BEHAVIOUR
    _MOCK_BEHAVIOUR = behaviour


def _call_mock(question: str, model: SemanticModel) -> str:
    if _MOCK_BEHAVIOUR == "hallucinate_metric":
        # The important adversarial case: the model names a metric that doesn't
        # exist. validate_selection must reject it rather than let it through.
        return json.dumps({"metrics": ["profit_margin"], "dimensions": [], "date_range": "last_month"})
    if _MOCK_BEHAVIOUR == "hallucinate_dimension":
        return json.dumps({"metrics": ["net_revenue"], "dimensions": ["salesperson"], "date_range": ""})
    if _MOCK_BEHAVIOUR == "sql_injection_attempt":
        # A model trying to smuggle SQL through a value. Values are bound as
        # parameters, so this must be inert.
        return json.dumps({
            "metrics": ["net_revenue"],
            "dimensions": [],
            "filters": [{"dimension": "channel", "operator": "in",
                          "values": ["web'; DROP TABLE orders; --"]}],
        })
    if _MOCK_BEHAVIOUR == "refuse":
        return json.dumps({"metrics": [], "refuse": True, "refuse_reason": "no churn metric defined"})
    if _MOCK_BEHAVIOUR == "malformed":
        return "I'm afraid I can't do that."
    return json.dumps({
        "metrics": ["net_revenue"],
        "dimensions": ["channel"],
        "date_range": "last_month",
        "time_grain": "",
        "filters": [],
        "limit": None,
        "refuse": False,
    })


def _call_groq(prompt: str, question: str) -> str:
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        json={
            "model": config.GROQ_MODEL,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            "temperature": 0.0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_gemini(prompt: str, question: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
    )
    resp = requests.post(
        url,
        json={
            "contents": [{"parts": [{"text": f"{prompt}\n\nQuestion: {question}"}]}],
            "generationConfig": {"temperature": 0.0},
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def _llm_select(question: str, model: SemanticModel):
    prompt = _SYSTEM_PROMPT + "\n" + model.describe_for_prompt()
    provider = config.LLM_PROVIDER
    try:
        if provider == "mock":
            raw_text = _call_mock(question, model)
        elif provider == "groq":
            raw_text = _call_groq(prompt, question)
        elif provider == "gemini":
            raw_text = _call_gemini(prompt, question)
        else:
            raise ValueError(f"unknown LLM_PROVIDER {provider!r}")
        raw = _extract_json(raw_text)
    except Exception as e:  # noqa: BLE001 - any provider/parse failure falls back
        logger.warning("LLM selection failed (%s); falling back to rule-based", e)
        result = _rule_based_select(question, model)
        if isinstance(result, Selection):
            result.assumptions.append(f"LLM selection failed ({e}); used rule-based matching")
        return result

    if raw.get("refuse"):
        return Refusal(
            reason="model_refused",
            message=raw.get("refuse_reason") or "That can't be answered with the defined metrics.",
            available_metrics=model.metric_names(),
            available_dimensions=model.dimension_names(),
        )

    filters = [
        Filter(
            dimension=f.get("dimension", ""),
            operator=f.get("operator", "in"),
            values=f.get("values", []) or [],
        )
        for f in (raw.get("filters") or [])
    ]

    selection = Selection(
        metrics=raw.get("metrics") or [],
        dimensions=raw.get("dimensions") or [],
        date_range=raw.get("date_range") or "",
        time_grain=raw.get("time_grain") or "",
        filters=filters,
        limit=raw.get("limit"),
        assumptions=[f"interpreted by {provider}"],
    )

    # THE GATE. Anything the model invented is rejected here rather than
    # becoming SQL. A hallucinated metric name is a refusal, not a guess.
    try:
        return validate_selection(selection, model)
    except SelectionError as e:
        return Refusal(
            reason="model_named_undefined_field",
            message=(
                f"The interpretation referenced something not in the semantic model ({e}). "
                f"I won't guess at a definition."
            ),
            available_metrics=model.metric_names(),
            available_dimensions=model.dimension_names(),
        )


def select(question: str, model: SemanticModel):
    """Returns a Selection, a Refusal, or a Clarification."""
    if not question or not question.strip():
        return Refusal(
            reason="empty_question",
            message="Please ask a question.",
            available_metrics=model.metric_names(),
            available_dimensions=model.dimension_names(),
        )

    if config.LLM_PROVIDER == "none":
        return _rule_based_select(question, model)
    return _llm_select(question, model)
