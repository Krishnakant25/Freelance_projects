"""
FastAPI app.

Every successful response includes the generated SQL, the metric definitions
used, the assumptions applied, and the data freshness — architecture doc §6.4:
never return a number without its derivation. A client UI that hides these is
free to collapse them, but the API always supplies them.
"""
import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import answer as answer_mod
from . import config, guardrails, reports, warehouse

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO))
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Prompt-to-BI",
    version="0.1.0",
    description="Semantic-layer analytics: questions map to defined metrics, SQL is generated deterministically.",
)


@app.on_event("startup")
def _startup():
    reports.init_db()
    try:
        answer_mod.get_model()
    except Exception:  # noqa: BLE001
        logger.exception("Semantic model failed to load — /ready will report unavailable")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)

    @field_validator("question")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be blank")
        return v


class PinRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    question: str = Field(..., min_length=1, max_length=500)


class RepinRequest(PinRequest):
    note: str = Field("", max_length=300)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Reports whether the model loaded, the warehouse is reachable, and — the
    part worth surfacing — whether the read-only boundary actually holds."""
    problems = []
    try:
        model = answer_mod.get_model()
        metrics = len(model.metrics)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "error": f"semantic model: {e}"},
        )
    try:
        counts = warehouse.table_row_counts()
        fresh = warehouse.freshness()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "error": f"warehouse: {e}"},
        )

    readonly = guardrails.verify_read_only()
    breaches = [k for k, v in readonly.items() if "refused" not in v]
    if breaches:
        problems.append(f"READ-ONLY BOUNDARY BROKEN for: {breaches}")

    return {
        "status": "ready" if not problems else "degraded",
        "metrics": metrics,
        "dimensions": len(model.dimensions),
        "warehouse_rows": counts,
        "data_freshness": fresh,
        "read_only_enforced": not breaches,
        "selector": config.LLM_PROVIDER,
        "problems": problems,
    }


@app.get("/model")
def get_model_endpoint():
    """The vocabulary. A client can render this as the 'what can I ask?' panel —
    which matters, because the honest answer to an undefined question is a
    refusal, and users need to see the boundary."""
    model = answer_mod.get_model()
    return {
        "metrics": [
            {"name": m.name, "label": m.label, "description": m.description,
             "format": m.format, "sql": m.sql}
            for m in model.metrics.values()
        ],
        "dimensions": [
            {"name": d.name, "label": d.label, "description": d.description,
             "time_dimension": d.time_dimension}
            for d in model.dimensions.values()
        ],
        "date_ranges": {k: v.label for k, v in model.date_ranges.items()},
        "time_grains": list(model.time_grains.keys()),
        "glossary": model.glossary,
    }


@app.post("/ask")
def ask_endpoint(req: AskRequest):
    result = answer_mod.ask(req.question)
    payload = {
        "ok": result.ok,
        "question": result.question,
        "refused": result.refused,
        "needs_clarification": result.needs_clarification,
        "message": result.message,
        "elapsed_ms": round(result.elapsed_ms, 1),
    }
    if result.refused:
        payload.update({
            "refusal_reason": result.refusal_reason,
            "available_metrics": result.available_metrics,
            "available_dimensions": result.available_dimensions,
        })
        return payload
    if result.needs_clarification:
        payload["clarification_options"] = result.clarification_options
        return payload

    payload.update({
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "chart": result.chart,
        # Derivation — always present, never optional.
        "sql": result.sql,
        "params": result.params,
        "metric_definitions": result.metric_definitions,
        "assumptions": result.assumptions,
        "filters_applied": result.filters_applied,
        "date_range": result.date_range,
        "data_freshness": result.data_freshness,
        "scan_estimate": result.scan_estimate,
        "truncated": result.truncated,
        "cached": result.cached,
    })
    return payload


@app.get("/reports")
def list_reports_endpoint():
    return {"reports": reports.list_reports()}


@app.post("/reports")
def pin_report_endpoint(req: PinRequest):
    try:
        return reports.pin(req.name, req.question)
    except reports.ReportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/reports/{name}")
def repin_report_endpoint(name: str, req: RepinRequest):
    try:
        return reports.repin(name, req.question, note=req.note)
    except reports.ReportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/reports/{name}/run")
def run_report_endpoint(name: str):
    try:
        result = reports.run(name)
    except reports.ReportError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "ok": result.ok,
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "chart": result.chart,
        "sql": result.sql,
        "assumptions": result.assumptions,
        "date_range": result.date_range,
        "data_freshness": result.data_freshness,
    }


@app.get("/reports/{name}/history")
def report_history_endpoint(name: str):
    try:
        return reports.history(name)
    except reports.ReportError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/reports/{name}")
def delete_report_endpoint(name: str):
    if not reports.delete(name):
        raise HTTPException(status_code=404, detail=f"no report named {name!r}")
    return {"status": "deleted"}
