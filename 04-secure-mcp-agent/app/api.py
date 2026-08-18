"""
HTTP API for the secure agent.

The shape of this API is itself a security decision. Two things are deliberate:

1. There is no endpoint that runs an arbitrary agent-chosen tool against
   arbitrary arguments with no policy in front of it. The operator submits a
   TASK; the split-agent architecture decides what runs. An endpoint that took
   a tool name and executed it directly would make the capability model
   advisory.

2. Submitting a task and approving its risky actions require DIFFERENT keys
   (see app/auth.py Role). Enforced in the policy engine too, so the CLI
   cannot route around it.

Run:  uvicorn app.api:app --reload
"""
from __future__ import annotations

import logging
import threading
import time

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import audit, config
from .agent import (
    ExecutorAgent, ReaderAgent, TrifectaViolation,
    executor_capabilities, reader_capabilities,
)
from .auth import Principal, require_admin, require_approver, require_operator
from .capabilities import CapabilityError
from .logging_config import setup_logging
from .policy import Decision, PolicyEngine
from .rate_limit import RateLimiter
from .registry import default_registry

setup_logging()
log = logging.getLogger("secure_agent.api")

app = FastAPI(
    title="Secure MCP Agent",
    description="An agent that executes tools under capability, policy and audit constraints.",
    version="1.0.0",
)

_limiter = RateLimiter(
    max_requests=config.RATE_LIMIT_REQUESTS,
    window_seconds=config.RATE_LIMIT_WINDOW_SECONDS,
)

# Per-actor policy engines. A pending approval belongs to the session that
# raised it, so approvals cannot leak between concurrent operators.
_SESSIONS: dict[str, PolicyEngine] = {}
_registry = None
_started_at = time.time()


# Sessions are retained so a pending approval survives between the request
# that raised it and the one that resolves it. They must NOT be retained
# forever: without eviction, one engine per distinct actor accumulates for the
# life of the process.
_SESSION_IDLE_TTL_SECONDS = max(config.SESSION_GRANT_TTL_SECONDS * 4, 3600)
_MAX_SESSIONS = 1000
_sessions_lock = threading.Lock()


def _evict_idle_sessions() -> None:
    """Drops sessions idle past the TTL, keeping any with pending approvals.

    Evicting a session with a pending approval would silently discard a
    decision a human still owes an answer to, so those are always kept.
    """
    now = time.time()
    stale = [actor for actor, engine in _SESSIONS.items()
             if now - engine.last_used > _SESSION_IDLE_TTL_SECONDS
             and not engine.pending_approvals()]
    for actor in stale:
        _SESSIONS.pop(actor, None)

    # Backstop: if a flood of distinct actors outruns the TTL, shed the
    # oldest idle ones rather than growing without bound.
    if len(_SESSIONS) > _MAX_SESSIONS:
        by_age = sorted(_SESSIONS.items(), key=lambda kv: kv[1].last_used)
        for actor, engine in by_age:
            if len(_SESSIONS) <= _MAX_SESSIONS:
                break
            if not engine.pending_approvals():
                _SESSIONS.pop(actor, None)


def _session_for(actor: str, attended: bool = True) -> PolicyEngine:
    with _sessions_lock:
        _evict_idle_sessions()
        engine = _SESSIONS.get(actor)
        if engine is None:
            engine = PolicyEngine(actor=actor, attended=attended)
            _SESSIONS[actor] = engine
    # Each HTTP request is one task. The tool-call budget bounds a runaway
    # loop WITHIN a task; capping an actor's lifetime request volume is the
    # rate limiter's job. Without this reset, reusing an engine across
    # requests turns the budget into a permanent lockout after 50 calls.
    engine.begin_task()
    return engine


@app.on_event("startup")
def _startup() -> None:
    global _registry
    _registry = default_registry(approver="system")
    if not config.AUTH_ENABLED:
        log.warning("AUTH_ENABLED=false - the API is unauthenticated. Do not run this way.")
    log.info("secure agent api started (workspace=%s)", config.WORKSPACE_DIR)


@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    if config.RATE_LIMIT_ENABLED and request.url.path not in ("/health", "/ready"):
        key = request.headers.get("x-api-key") or (request.client.host if request.client else "anon")
        allowed, retry_after = _limiter.check(key)
        if not allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded.", "retry_after_seconds": retry_after},
                headers={"Retry-After": str(retry_after)},
            )
    return await call_next(request)


# --- models ------------------------------------------------------------


class TaskRequest(BaseModel):
    path: str = Field(..., max_length=1000, description="File or directory inside the workspace.")
    mode: str = Field("summarize", pattern="^(summarize|scan)$")


class ActionRequest(BaseModel):
    tool: str = Field(..., max_length=100)
    args: dict = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    approval_id: str = Field(..., max_length=200)
    approved: bool
    grant_session: bool = False


# --- probes ------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Liveness only - deliberately does no work, so it cannot fail for a
    reason that should take the process out of rotation."""
    return {"status": "ok", "uptime_seconds": round(time.time() - _started_at, 1)}


@app.get("/ready")
def ready() -> dict:
    """Readiness. Fails if the audit chain is broken: an agent that cannot
    prove what it did should not be accepting new work."""
    problems = []
    chain = audit.verify()
    if not chain.valid:
        problems.append("audit chain invalid: " + chain.describe())
    if not config.WORKSPACE_DIR.exists():
        problems.append("workspace missing: " + str(config.WORKSPACE_DIR))
    if config.AUTH_ENABLED and not config.API_KEYS_PATH.exists():
        problems.append("AUTH_ENABLED but no keys file - every request would 401")

    if problems:
        raise HTTPException(status_code=503, detail={"ready": False, "problems": problems})
    return {"ready": True, "records_audited": chain.records_checked}


# --- the agent ---------------------------------------------------------


@app.post("/task")
def run_task(req: TaskRequest, principal: Principal = Depends(require_operator)) -> dict:
    """Runs a read-only analysis task through the ReaderAgent.

    The reader holds no write, exec, secret or egress capability, so this
    endpoint cannot be turned into an exfiltration primitive by anything the
    file it reads happens to say.
    """
    reader = ReaderAgent(reader_capabilities())
    try:
        findings = (reader.scan_directory(req.path) if req.mode == "scan"
                    else reader.read_and_summarize(req.path))
    except CapabilityError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No such file or directory in the workspace.")

    # The reader returns refusals as error FINDINGS rather than exceptions, so
    # that an attacker-influenced message can never re-enter as instruction.
    # Correct for the agent, wrong for HTTP: answering 200 to a traversal probe
    # makes it indistinguishable from a successful read in every dashboard and
    # access log. Translate it back into a status code at the boundary.
    if findings and all(f.kind == "error" for f in findings):
        message = findings[0].value
        code = 404 if "no such" in message.lower() or "not found" in message.lower() else 403
        raise HTTPException(status_code=code, detail=message)

    return {
        "mode": req.mode,
        "path": req.path,
        "findings": [f.as_dict() for f in findings],
        "note": ("Findings are sanitized, structurally typed data - never instructions. "
                 "The reader agent holds no egress or write capability."),
    }


@app.post("/action")
def request_action(req: ActionRequest, principal: Principal = Depends(require_operator)) -> dict:
    """Requests a state-changing action. Returns either the result, or a
    pending approval that a DIFFERENT principal must resolve."""
    engine = _session_for(principal.name, attended=True)
    executor = ExecutorAgent(executor_capabilities(), policy=engine)
    try:
        result = executor.act(req.tool, req.args)
    except TrifectaViolation as e:
        raise HTTPException(status_code=403, detail="refused: " + str(e))
    except CapabilityError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if result.get("decision") == Decision.PENDING_APPROVAL.value:
        result = dict(result)
        result["next_step"] = (
            "POST /approvals with this approval_id, using a key that holds the "
            "'approver' role and is NOT the key that submitted this action."
        )
    return result


@app.get("/approvals")
def list_approvals(principal: Principal = Depends(require_approver)) -> dict:
    pending = []
    for actor, engine in _SESSIONS.items():
        for approval_id, item in engine.pending_approvals().items():
            pending.append({
                "approval_id": approval_id,
                "requested_by": actor,
                "tool": item["tool"],
                "scope": item["scope"],
                "you_may_approve": actor != principal.name,
            })
    return {"pending": pending, "count": len(pending)}


@app.post("/approvals")
def resolve_approval(req: ApprovalRequest,
                     principal: Principal = Depends(require_approver)) -> dict:
    for engine in _SESSIONS.values():
        if req.approval_id in engine.pending_approvals():
            result = engine.resolve_approval(
                req.approval_id, approved=req.approved,
                approver=principal.name, grant_session=req.grant_session,
            )
            if result.decision is Decision.DENIED and "separation of duties" in result.reason:
                raise HTTPException(status_code=403, detail=result.reason)
            return {"decision": result.decision.value, "reason": result.reason}
    raise HTTPException(status_code=404, detail="Unknown or already-resolved approval.")


# --- transparency ------------------------------------------------------


@app.get("/audit/verify")
def audit_verify(principal: Principal = Depends(require_operator)) -> dict:
    result = audit.verify()
    return {
        "valid": result.valid,
        "records_checked": result.records_checked,
        "detail": result.describe(),
        "first_bad_seq": result.first_bad_seq,
    }


@app.get("/audit/stats")
def audit_stats(principal: Principal = Depends(require_operator)) -> dict:
    return audit.stats()


@app.get("/tools")
def list_tools(principal: Principal = Depends(require_operator)) -> dict:
    return {"tools": _registry.list_tools()}


@app.post("/tools/{name}/revoke")
def revoke_tool(name: str, principal: Principal = Depends(require_admin)) -> dict:
    if not _registry.revoke(name, approver=principal.name):
        raise HTTPException(status_code=404, detail="Unknown tool: " + name)
    return {"revoked": name, "by": principal.name}
