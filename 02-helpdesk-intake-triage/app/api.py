"""
FastAPI app.

TRUST MODEL — two tiers, deliberately:

  INTAKE (/report, /report/file-anyway, /deflection/feedback) is ANONYMOUS BY
  DESIGN. It's the public submission surface, embedded in a chat widget or
  kiosk anyone in the org can use, and it exposes nothing: you submit your own
  text and get back a KB article or your own ticket id. Requiring a per-user
  credential here would defeat the purpose. Protected instead by input caps
  and rate limiting.

  STAFF (/tickets, /tickets/{id}, acknowledge, resolve, /stats) REQUIRES an
  X-API-Key with the 'staff' role. These expose every ticket's contents —
  which in a helpdesk includes whatever users typed, up to and including
  credentials they shouldn't have pasted and security-incident detail — and
  mutate ticket state.

  ADMIN (/admin/*) requires the 'admin' role. Separate so a read-mostly staff
  key can't trigger pages or reload credentials.

See app/auth.py for key management (`scripts/manage_keys.py`).
"""
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import alerting, config, db, embeddings, kb, scheduler
from .auth import Principal, reload_keystore, require_admin, require_staff
from .intake import confirm_deflection_resolved, file_ticket, start_intake
from .logging_config import setup_logging
from .rate_limit import RateLimiter

setup_logging()
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Helpdesk Intake & Triage",
    version="0.2.0",
    description="Structured intake with KB deflection, deterministic priority, and red-flag override.",
)

# CORS is open because the intake page is designed to be embedded from other
# origins (intranet page, kiosk, chat widget) and the intake endpoints carry
# no credentials. Tighten allow_origins to specific front-end domains as soon
# as authentication is added — an authenticated API with wildcard CORS is a
# meaningfully worse combination than an anonymous one.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_rate_limiter = RateLimiter(
    max_requests=config.RATE_LIMIT_REQUESTS,
    window_seconds=config.RATE_LIMIT_WINDOW_SECONDS,
)

_startup_state = {"warmed_up": False, "warmup_seconds": None}


@app.on_event("startup")
async def _startup():
    db.init_db()

    from .auth import get_keystore

    keystore = get_keystore()
    if config.AUTH_ENABLED and len(keystore) == 0:
        logger.error(
            "AUTH_ENABLED=true but no API keys exist at %s — staff endpoints will "
            "reject every request. Create one: python scripts/manage_keys.py create "
            "--name <name> --roles staff",
            config.API_KEYS_PATH,
        )
    if not config.AUTH_ENABLED:
        logger.warning("=" * 70)
        logger.warning("AUTH_ENABLED=false — STAFF ENDPOINTS ARE UNAUTHENTICATED. Local dev only.")
        logger.warning("=" * 70)

    if config.WARMUP_ON_STARTUP:
        try:
            elapsed = embeddings.warmup()
            _startup_state["warmed_up"] = True
            _startup_state["warmup_seconds"] = round(elapsed, 1)
        except Exception:  # noqa: BLE001 - a warmup failure shouldn't prevent boot
            logger.exception("Embedding warmup failed; first request will pay the load cost")

    # Deliver anything a previous process left pending (crash, deploy, OOM).
    # This is the recovery half of the durable outbox — without it, a pending
    # alert written just before a restart would sit there until the first
    # scheduler tick.
    try:
        flushed = alerting.flush_outbox()
        if flushed["processed"]:
            logger.warning(
                "Recovered %d pending alert(s) from a previous run (%d delivered)",
                flushed["processed"], flushed["sent"],
            )
    except Exception:  # noqa: BLE001
        logger.exception("Startup outbox flush failed")

    scheduler.start()
    logger.info("Startup complete (provider=%s)", config.LLM_PROVIDER)


@app.on_event("shutdown")
async def _shutdown():
    await scheduler.stop()


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "elapsed_ms": round(elapsed_ms, 1),
        },
    )
    return response


def get_keystore_len():
    """Small indirection so /ready can report key-store state without importing
    the keystore at module scope (which would read keys.json at import time and
    make the test harness unable to point it elsewhere)."""
    from .auth import get_keystore

    return get_keystore()


def _client_key(request: Request) -> str:
    """Rate-limit key. Uses X-Forwarded-For's first hop when present, since
    behind a proxy every request otherwise appears to come from the proxy and
    a single client could exhaust the shared limit for everyone.

    NOTE: X-Forwarded-For is client-controllable unless a trusted proxy
    overwrites it. Only trust this if a proxy you control sets the header.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(request: Request) -> None:
    if not config.RATE_LIMIT_ENABLED:
        return
    key = _client_key(request)
    allowed, retry_after = _rate_limiter.check(key)
    if not allowed:
        logger.warning(
            "rate_limited",
            extra={"request_id": getattr(request.state, "request_id", "-"), "client": key},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
    # Opportunistic cleanup so the key-tracking dict can't grow unbounded on a
    # public endpoint. Cheap, and only when the map is already large.
    if _rate_limiter.tracked_keys() > 1000:
        _rate_limiter.prune()


# --- Request models -------------------------------------------------------
# Length caps matter here specifically because /report runs an embedding model
# over the description: unbounded text is cheap to send and expensive to
# process, which is the shape of an amplification vector.


class ReportRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=config.MAX_DESCRIPTION_CHARS)
    requester: str = Field("", max_length=config.MAX_REQUESTER_CHARS)
    allow_deflection: bool = True

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be blank")
        return v


class DeflectionFeedback(BaseModel):
    query_text: str = Field(..., min_length=1, max_length=config.MAX_DESCRIPTION_CHARS)
    kb_article_id: int = Field(..., ge=1)
    resolved: bool


# --- Endpoints ------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def root():
    return FileResponse(str(STATIC_DIR / "voice.html"))


@app.get("/health")
def health():
    """Liveness only — no dependencies touched, always cheap."""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Readiness — verifies the DB is reachable and reports whether the
    embedding model is actually loaded. A process that is 'up' but hasn't
    loaded its model will serve a ~30s first request, so liveness alone is a
    misleading signal for a load balancer to route on."""
    try:
        with db.session() as conn:
            tickets = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
            open_p1 = conn.execute(
                "SELECT COUNT(*) c FROM tickets WHERE priority='P1' AND status='open'"
            ).fetchone()["c"]
            outbox = db.outbox_counts(conn)
        kb_count = kb.article_count()
    except Exception as e:  # noqa: BLE001
        logger.exception("Readiness check failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "error": str(e)},
        )

    warnings = []
    if kb_count == 0:
        warnings.append("KB is empty — no self-service deflection is possible")
    if not _startup_state["warmed_up"] and config.WARMUP_ON_STARTUP:
        warnings.append("embedding model not warmed up — first request will be slow")
    if config.AUTH_ENABLED and len(get_keystore_len()) == 0:
        warnings.append("no API keys defined — staff endpoints will reject all requests")
    if not config.AUTH_ENABLED:
        warnings.append("AUTH_ENABLED=false — staff endpoints are UNAUTHENTICATED")
    if not scheduler.is_running():
        warnings.append(
            "escalation scheduler not running — unacknowledged P1s will not escalate "
            "unless external cron calls POST /admin/check-escalations"
        )
    # A failed outbox entry means a page was owed and never delivered. That is
    # operationally important enough to surface on the readiness probe rather
    # than leave buried in logs.
    if outbox["failed"]:
        warnings.append(f"{outbox['failed']} alert(s) permanently FAILED to deliver — investigate")
    if outbox["pending"] > 5:
        warnings.append(f"{outbox['pending']} alert(s) pending delivery — check alerting config")

    return {
        "status": "ready",
        "tickets": tickets,
        "open_p1": open_p1,
        "kb_articles": kb_count,
        "extraction_provider": config.LLM_PROVIDER,
        "warmed_up": _startup_state["warmed_up"],
        "warmup_seconds": _startup_state["warmup_seconds"],
        "alerting": "slack" if config.SLACK_WEBHOOK_URL else "log-only",
        "auth_enabled": config.AUTH_ENABLED,
        "outbox": outbox,
        "scheduler": {"running": scheduler.is_running(), **scheduler.stats()},
        "warnings": warnings,
    }


@app.post("/report")
def report_endpoint(req: ReportRequest, request: Request):
    enforce_rate_limit(request)
    result = start_intake(req.description, requester=req.requester, allow_deflection=req.allow_deflection)
    logger.info(
        "intake",
        extra={
            "request_id": getattr(request.state, "request_id", "-"),
            "outcome": result.outcome,
            "priority": result.priority,
            "red_flag": result.red_flag,
        },
    )
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
def file_anyway_endpoint(req: ReportRequest, request: Request):
    """Called when a deflection offer didn't resolve the issue."""
    enforce_rate_limit(request)
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
def deflection_feedback_endpoint(req: DeflectionFeedback, request: Request):
    enforce_rate_limit(request)
    confirm_deflection_resolved(req.query_text, req.kb_article_id, req.resolved)
    return {"status": "recorded"}


# --- Staff endpoints (AUTHENTICATED — require X-API-Key with 'staff') ------
# These expose every ticket's contents, which in a helpdesk can include
# whatever users typed, and mutate ticket state. See app/auth.py for why
# intake is anonymous and these are not.


@app.get("/tickets")
def list_tickets_endpoint(
    status_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    principal: Principal = Depends(require_staff),
):
    # Pagination is enforced, not optional: an unbounded SELECT on a table
    # that grows forever is a latency and memory problem that only appears
    # once the deployment has been running a while.
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with db.session() as conn:
        rows = db.list_tickets(conn, status=status_filter, limit=limit, offset=offset)
        total = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
    return {"tickets": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@app.get("/tickets/{ticket_id}")
def get_ticket_endpoint(ticket_id: int, principal: Principal = Depends(require_staff)):
    with db.session() as conn:
        row = db.get_ticket(conn, ticket_id)
        if row is None:
            raise HTTPException(status_code=404, detail="ticket not found")
        events = conn.execute(
            "SELECT * FROM audit_log WHERE ticket_id = ? ORDER BY created_at, id", (ticket_id,)
        ).fetchall()
    return {"ticket": dict(row), "audit_log": [dict(e) for e in events]}


@app.post("/tickets/{ticket_id}/acknowledge")
def acknowledge_endpoint(ticket_id: int, principal: Principal = Depends(require_staff)):
    with db.session() as conn:
        row = db.get_ticket(conn, ticket_id)
        if row is None:
            raise HTTPException(status_code=404, detail="ticket not found")
        db.acknowledge_ticket(conn, ticket_id)
        db.log_event(conn, "acknowledged", {"via": "api", "by": principal.name}, ticket_id=ticket_id)
    return {"status": "acknowledged"}


@app.post("/tickets/{ticket_id}/resolve")
def resolve_endpoint(ticket_id: int, principal: Principal = Depends(require_staff)):
    with db.session() as conn:
        row = db.get_ticket(conn, ticket_id)
        if row is None:
            raise HTTPException(status_code=404, detail="ticket not found")
        db.resolve_ticket(conn, ticket_id)
        db.log_event(conn, "resolved", {"via": "api", "by": principal.name}, ticket_id=ticket_id)
    return {"status": "resolved"}


@app.post("/admin/check-escalations")
def check_escalations_endpoint(principal: Principal = Depends(require_admin)):
    """Manual escalation sweep. The in-process scheduler normally does this;
    this endpoint exists for external cron when SCHEDULER_ENABLED=false (the
    correct setup for multi-worker deployments)."""
    escalated = alerting.check_escalations()
    return {"escalated": escalated, "scheduler_running": scheduler.is_running()}


@app.post("/admin/flush-alerts")
def flush_alerts_endpoint(principal: Principal = Depends(require_admin)):
    """Retries pending outbox alerts. Useful after fixing a broken webhook —
    previously-undelivered pages get sent rather than staying lost."""
    return alerting.flush_outbox()


@app.post("/admin/reload-keys")
def reload_keys_endpoint(principal: Principal = Depends(require_admin)):
    """Reload keys.json without a restart, so revoking a key doesn't require
    downtime."""
    reload_keystore()
    return {"status": "reloaded"}


@app.get("/stats")
def stats_endpoint(principal: Principal = Depends(require_staff)):
    with db.session() as conn:
        return db.deflection_stats(conn)
