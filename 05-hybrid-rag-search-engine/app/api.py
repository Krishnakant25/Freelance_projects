"""
FastAPI app.

SECURITY NOTE: `user_groups` is NOT a request field. ACL groups are resolved
server-side from the caller's API key (app/auth.py). An earlier version
accepted groups in the request body, which meant any caller could claim
["management"] and read restricted documents — correct SQL filtering on an
unverified identity is not access control. Do not reintroduce a client-
supplied groups parameter.
"""
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config, db
from .auth import Principal, require_ingest_permission, require_principal, reload_keystore
from .ingest import ingest_directory, ingest_file, remove_file
from .logging_config import setup_logging
from .query import answer_question
from .rate_limit import RateLimiter
from .vector_index import get_index

setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Hybrid RAG Search Engine",
    version="0.2.0",
    description="Domain-specific hybrid search with ACL-filtered retrieval and verified citations.",
)

_rate_limiter = RateLimiter(
    max_requests=config.RATE_LIMIT_REQUESTS,
    window_seconds=config.RATE_LIMIT_WINDOW_SECONDS,
)


@app.on_event("startup")
def _startup():
    db.init_db()
    from .auth import get_keystore

    keystore = get_keystore()
    if config.AUTH_ENABLED and len(keystore) == 0:
        logger.error(
            "AUTH_ENABLED=true but no API keys are defined at %s. Every request "
            "will be rejected. Create one with: python scripts/manage_keys.py create "
            "--name <name> --groups <groups>",
            config.API_KEYS_PATH,
        )
    if not config.AUTH_ENABLED:
        logger.warning("=" * 70)
        logger.warning("AUTH_ENABLED=false — API IS UNAUTHENTICATED. Local dev only.")
        logger.warning("=" * 70)
    logger.info("Startup complete. Vector backend=%s", config.VECTOR_INDEX_BACKEND)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request ID and log timing for every request."""
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


def enforce_rate_limit(request: Request, principal: Principal) -> None:
    if not config.RATE_LIMIT_ENABLED:
        return
    allowed, retry_after = _rate_limiter.check(principal.name)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )


# --- Request models -------------------------------------------------------
# Note the absence of any `user_groups` / `acl` field on QueryRequest.


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


class IngestFileRequest(BaseModel):
    path: str
    acl_groups: list[str] = []
    title: Optional[str] = None


class IngestDirectoryRequest(BaseModel):
    directory: str
    acl_groups: list[str] = []


# --- Endpoints ------------------------------------------------------------


@app.get("/health")
def health():
    """Liveness only — no auth, no dependencies touched."""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Readiness — verifies the DB is reachable and reports index state."""
    try:
        with db.session() as conn:
            chunk_count = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
            doc_count = conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"]
    except Exception as e:  # noqa: BLE001
        logger.exception("Readiness check failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "error": str(e)},
        )
    return {
        "status": "ready",
        "documents": doc_count,
        "chunks": chunk_count,
        "vector_backend": config.VECTOR_INDEX_BACKEND,
        "auth_enabled": config.AUTH_ENABLED,
    }


@app.post("/query")
def query_endpoint(
    req: QueryRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    enforce_rate_limit(request, principal)
    logger.info(
        "query",
        extra={
            "request_id": getattr(request.state, "request_id", "-"),
            "principal": principal.name,
            "groups": principal.groups,
        },
    )
    # principal.groups — server-derived, never from the request body.
    return answer_question(req.question, user_groups=principal.groups)


@app.post("/ingest/file")
def ingest_file_endpoint(
    req: IngestFileRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    require_ingest_permission(principal)
    enforce_rate_limit(request, principal)
    path = Path(req.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    result = ingest_file(path, acl_groups=req.acl_groups, title=req.title)
    get_index().invalidate()
    return result


@app.post("/ingest/directory")
def ingest_directory_endpoint(
    req: IngestDirectoryRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    require_ingest_permission(principal)
    enforce_rate_limit(request, principal)
    directory = Path(req.directory)
    if not directory.exists():
        raise HTTPException(status_code=404, detail=f"Directory not found: {directory}")
    results = ingest_directory(directory, acl_groups=req.acl_groups)
    get_index().invalidate()
    return {"results": results}


@app.delete("/ingest/file")
def delete_file_endpoint(
    path: str,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    require_ingest_permission(principal)
    enforce_rate_limit(request, principal)
    removed = remove_file(Path(path))
    if not removed:
        raise HTTPException(status_code=404, detail=f"No indexed document for path: {path}")
    get_index().invalidate()
    return {"path": path, "status": "deleted"}


@app.post("/admin/reload-keys")
def reload_keys_endpoint(
    request: Request,
    principal: Principal = Depends(require_principal),
):
    """Reload keys.json without restarting. Requires an ingest-capable key."""
    require_ingest_permission(principal)
    reload_keystore()
    return {"status": "reloaded"}
