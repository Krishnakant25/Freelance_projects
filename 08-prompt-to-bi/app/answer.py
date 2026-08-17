"""
Orchestration: question -> Selection -> SQL -> result -> answer with derivation.

Architecture doc §6.4: never show a number without its derivation. Every answer
returned from here carries the generated SQL, the metric definitions used, the
filters applied, the resolved date range, the row count, and the data freshness
timestamp. That's what keeps the residual accuracy risk *checkable* rather than
invisible — an analyst can verify any figure in one click instead of trusting it.
"""
import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import charts, config, guardrails, semantic, sql_builder, warehouse
from .selector import Clarification, Refusal, select

logger = logging.getLogger(__name__)

_model_lock = threading.Lock()
_model: Optional[semantic.SemanticModel] = None


def get_model(reload: bool = False) -> semantic.SemanticModel:
    global _model
    with _model_lock:
        if _model is None or reload:
            _model = semantic.load_model()
        return _model


@dataclass
class Answer:
    ok: bool
    question: str
    # Populated on success
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    sql: str = ""
    params: list = field(default_factory=list)
    metric_definitions: dict = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    filters_applied: list[str] = field(default_factory=list)
    date_range: str = ""
    chart: dict = field(default_factory=dict)
    data_freshness: str = ""
    elapsed_ms: float = 0.0
    scan_estimate: int = 0
    cached: bool = False
    truncated: bool = False
    # Populated on refusal / clarification
    refused: bool = False
    refusal_reason: str = ""
    message: str = ""
    available_metrics: list[str] = field(default_factory=list)
    available_dimensions: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    clarification_options: list[str] = field(default_factory=list)


# --- Result cache ---------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _cache_key(sql: str, params: list) -> str:
    return hashlib.sha256(json.dumps([sql, params], default=str).encode()).hexdigest()


def _cache_get(key: str) -> Optional[dict]:
    if not config.CACHE_ENABLED:
        return None
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        stored_at, payload = entry
        if time.monotonic() - stored_at > config.CACHE_TTL_SECONDS:
            del _cache[key]
            return None
        return payload


def _cache_set(key: str, payload: dict) -> None:
    if not config.CACHE_ENABLED:
        return
    with _cache_lock:
        _cache[key] = (time.monotonic(), payload)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# --- Main entry point -----------------------------------------------------


def ask(question: str, model: Optional[semantic.SemanticModel] = None) -> Answer:
    model = model or get_model()
    started = time.perf_counter()

    result = select(question, model)

    if isinstance(result, Refusal):
        return Answer(
            ok=False,
            refused=True,
            question=question,
            refusal_reason=result.reason,
            message=result.message,
            available_metrics=result.available_metrics,
            available_dimensions=result.available_dimensions,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    if isinstance(result, Clarification):
        return Answer(
            ok=False,
            needs_clarification=True,
            question=question,
            message=result.question,
            clarification_options=result.options,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    return run_selection(result, question=question, model=model, started=started)


def run_selection(
    selection: semantic.Selection,
    question: str = "",
    model: Optional[semantic.SemanticModel] = None,
    started: Optional[float] = None,
) -> Answer:
    """Executes an already-validated Selection.

    Separate from ask() because frozen scheduled reports re-run a PINNED
    Selection rather than re-interpreting the question — see app/reports.py and
    doc §6.5 on why regenerating a scheduled query lets trends drift for
    non-business reasons.
    """
    model = model or get_model()
    started = started or time.perf_counter()

    built = sql_builder.build(selection, model)
    key = _cache_key(built.sql, built.params)

    cached_payload = _cache_get(key)
    if cached_payload is not None:
        answer = Answer(**cached_payload)
        answer.cached = True
        answer.elapsed_ms = (time.perf_counter() - started) * 1000
        return answer

    try:
        qr = guardrails.execute(built.sql, built.params)
    except guardrails.GuardrailViolation as e:
        return Answer(
            ok=False,
            refused=True,
            question=question,
            refusal_reason="guardrail",
            message=str(e),
            sql=built.sql,
            params=built.params,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    chart_spec = charts.choose(
        columns=built.columns,
        rows=qr.rows,
        model=model,
        selection=selection,
    )

    answer = Answer(
        ok=True,
        question=question,
        columns=qr.columns,
        rows=qr.rows,
        row_count=qr.row_count,
        sql=built.sql,
        params=built.params,
        metric_definitions=built.metric_definitions,
        assumptions=built.assumptions,
        filters_applied=[f.describe() for f in selection.filters],
        date_range=built.date_range_label,
        chart=chart_spec,
        data_freshness=warehouse.freshness(),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        scan_estimate=qr.scan_estimate,
        truncated=qr.truncated,
    )

    payload = asdict(answer)
    payload.pop("cached", None)
    payload.pop("elapsed_ms", None)
    _cache_set(key, {**payload, "cached": False, "elapsed_ms": 0.0})
    return answer
