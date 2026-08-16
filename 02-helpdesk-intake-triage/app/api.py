"""
FastAPI app: intake endpoints (public-facing, no auth — this is meant to be
embedded in a chat widget anyone can use) and staff endpoints (queue,
acknowledge, resolve — would need auth in a real deployment; see
MANUAL_STEPS.md, this demo build does not implement it, unlike the RAG
project's API which does. Adding it is a Phase 2 task if this gets deployed
publicly, tracked honestly rather than pretended-away).
"""
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import alerting, db
from .intake import confirm_deflection_resolved, file_ticket, start_intake

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Helpdesk Intake & Triage", version="0.1.0")

# The voice UI is a plain static page making fetch() calls to this same API.
# CORS is wide open here on purpose — /report is meant to be reachable from
# any front-end embedding it (a chat widget, an intranet page, a kiosk),
# same "public-facing, no auth" trust model already stated above. This is
# NOT appropriate once auth is added (see Known Limitations in README) —
# tighten allow_origins to specific deployed front-end domains at that point.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def root():
    return FileResponse(str(STATIC_DIR / "voice.html"))


@app.on_event("startup")
def _startup():
    db.init_db()


class ReportRequest(BaseModel):
    description: str
    requester: str = ""
    allow_deflection: bool = True


class DeflectionFeedback(BaseModel):
    query_text: str
    kb_article_id: int
    resolved: bool


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/report")
def report_endpoint(req: ReportRequest):
    if not req.description.strip():
        raise HTTPException(status_code=400, detail="description must not be empty")
    result = start_intake(req.description, requester=req.requester, allow_deflection=req.allow_deflection)
    if result.outcome == "deflected":
        return {
            "outcome": "deflected",
            "kb_article": {
                "id": result.kb_match.article_id,
                "title": result.kb_match.title,
                "body": result.kb_match.body,
                "score": round(result.kb_match.score, 3),
            },
        }
    return {
        "outcome": "ticket_created",
        "ticket_id": result.ticket_id,
        "priority": result.priority,
        "reasoning": result.reasoning,
        "red_flag": result.red_flag,
        "alert": result.alert,
    }


@app.post("/report/file-anyway")
def file_anyway_endpoint(req: ReportRequest):
    """Called when a deflection offer didn't resolve the issue."""
    result = file_ticket(req.description, requester=req.requester)
    return {
        "outcome": "ticket_created",
        "ticket_id": result.ticket_id,
        "priority": result.priority,
        "reasoning": result.reasoning,
        "red_flag": result.red_flag,
        "alert": result.alert,
    }


@app.post("/deflection/feedback")
def deflection_feedback_endpoint(req: DeflectionFeedback):
    confirm_deflection_resolved(req.query_text, req.kb_article_id, req.resolved)
    return {"status": "recorded"}


@app.get("/tickets")
def list_tickets_endpoint(status: Optional[str] = None):
    with db.session() as conn:
        rows = db.list_tickets(conn, status=status)
    return {"tickets": [dict(r) for r in rows]}


@app.get("/tickets/{ticket_id}")
def get_ticket_endpoint(ticket_id: int):
    with db.session() as conn:
        row = db.get_ticket(conn, ticket_id)
        if row is None:
            raise HTTPException(status_code=404, detail="ticket not found")
        events = conn.execute(
            "SELECT * FROM audit_log WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)
        ).fetchall()
    return {"ticket": dict(row), "audit_log": [dict(e) for e in events]}


@app.post("/tickets/{ticket_id}/acknowledge")
def acknowledge_endpoint(ticket_id: int):
    with db.session() as conn:
        row = db.get_ticket(conn, ticket_id)
        if row is None:
            raise HTTPException(status_code=404, detail="ticket not found")
        db.acknowledge_ticket(conn, ticket_id)
        db.log_event(conn, "acknowledged", {"via": "api"}, ticket_id=ticket_id)
    return {"status": "acknowledged"}


@app.post("/tickets/{ticket_id}/resolve")
def resolve_endpoint(ticket_id: int):
    with db.session() as conn:
        row = db.get_ticket(conn, ticket_id)
        if row is None:
            raise HTTPException(status_code=404, detail="ticket not found")
        db.resolve_ticket(conn, ticket_id)
        db.log_event(conn, "resolved", {"via": "api"}, ticket_id=ticket_id)
    return {"status": "resolved"}


@app.post("/admin/check-escalations")
def check_escalations_endpoint():
    escalated = alerting.check_escalations()
    return {"escalated": escalated}


@app.get("/stats")
def stats_endpoint():
    with db.session() as conn:
        return db.deflection_stats(conn)
