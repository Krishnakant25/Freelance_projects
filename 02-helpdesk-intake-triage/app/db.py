"""
SQLite storage: tickets, KB articles, deflection log, and an immutable audit
log of every classification decision.

Single-file SQLite, same rationale as the RAG project: zero infra for a
demo/small-deployment scale, with a documented Postgres upgrade path once a
real client needs concurrent writers.
"""
import json
import sqlite3
import struct
from contextlib import contextmanager
from typing import Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    requester TEXT,
    category TEXT NOT NULL,
    affected_system TEXT,
    impact TEXT NOT NULL,
    urgency TEXT NOT NULL,
    priority TEXT NOT NULL,
    description TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    red_flag_matched INTEGER NOT NULL DEFAULT 0,
    red_flag_category TEXT,
    red_flag_phrase TEXT,
    extraction_provider TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open | acknowledged | resolved | escalated
    acknowledged_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_priority ON tickets(priority);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

CREATE TABLE IF NOT EXISTS kb_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT,
    embedding BLOB,
    embedding_dim INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    query_text TEXT NOT NULL,
    kb_article_id INTEGER REFERENCES kb_articles(id),
    similarity_score REAL,
    resolved INTEGER   -- NULL = unknown/not asked, 1 = user confirmed resolved, 0 = user said no
);

-- Append-only. This is ENFORCED by triggers below, not just documented —
-- an audit log that a bug (or a person) can quietly rewrite provides no
-- more assurance than no audit log at all, while looking like it does.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    ticket_id INTEGER,
    event_type TEXT NOT NULL,
    details TEXT NOT NULL   -- JSON
);

-- Durable outbox for P1 alerts.
--
-- WHY AN OUTBOX: previously an alert was a fire-and-forget HTTP call made
-- after the ticket was written. If the process died between those two steps —
-- deploy, OOM, crash — the ticket existed and the page was never sent, with
-- nothing anywhere recording that it should have been. For the highest
-- severity path in the system that's the wrong failure mode.
--
-- Now the alert INTENT is written in the same transaction as the ticket, so it
-- is durable before any network call is attempted. Delivery is a separate
-- step that can retry, and a crash leaves a 'pending' row that gets picked up
-- on the next sweep instead of vanishing.
CREATE TABLE IF NOT EXISTS alert_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,              -- 'new_p1' | 'escalation'
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_attempt_at TEXT,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_status ON alert_outbox(status);
CREATE INDEX IF NOT EXISTS idx_outbox_ticket ON alert_outbox(ticket_id);

CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_log(ticket_id);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_deflections_created ON deflections(created_at);
CREATE INDEX IF NOT EXISTS idx_deflections_article ON deflections(kb_article_id);
CREATE INDEX IF NOT EXISTS idx_deflections_resolved ON deflections(resolved);

-- Immutability enforcement. RAISE(ABORT) makes any UPDATE or DELETE against
-- audit_log fail at the database level, so the guarantee holds regardless of
-- which code path (or which future contributor) tries it.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is not permitted');
END;
"""


def pack_embedding(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def unpack_embedding(blob: bytes, dim: int):
    return struct.unpack(f"{dim}f", blob)


def get_connection() -> sqlite3.Connection:
    """
    Opens a tuned connection. The PRAGMAs below are the difference between
    "works on my laptop with one user" and "survives concurrent traffic":

    - journal_mode=WAL: readers don't block the writer and vice versa. In the
      default rollback-journal mode, any read blocks a concurrent write, so
      a queue-dashboard refresh could make a ticket submission fail.
    - busy_timeout: without it, a locked database raises
      "database is locked" IMMEDIATELY instead of waiting. Two simultaneous
      ticket submissions would surface as a 500 to one of the users.
    - synchronous=NORMAL: safe with WAL and substantially faster than FULL.
      (Trade-off: a hard OS crash can lose the last transaction(s). Acceptable
      for helpdesk tickets; revisit if writes ever become financial records.)
    - foreign_keys=ON: not on by default in SQLite, and the schema declares
      FK relationships that would otherwise be silently unenforced.
    """
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(config.DB_PATH),
        timeout=config.DB_BUSY_TIMEOUT_SECONDS,
        isolation_level="DEFERRED",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(config.DB_BUSY_TIMEOUT_SECONDS * 1000)}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def log_event(conn: sqlite3.Connection, event_type: str, details: dict, ticket_id: Optional[int] = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (ticket_id, event_type, details) VALUES (?, ?, ?)",
        (ticket_id, event_type, json.dumps(details, default=str)),
    )


def insert_ticket(
    conn: sqlite3.Connection,
    requester: str,
    category: str,
    affected_system: str,
    impact: str,
    urgency: str,
    priority: str,
    description: str,
    reasoning: str,
    red_flag_matched: bool,
    red_flag_category: str,
    red_flag_phrase: str,
    extraction_provider: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO tickets
           (requester, category, affected_system, impact, urgency, priority,
            description, reasoning, red_flag_matched, red_flag_category,
            red_flag_phrase, extraction_provider)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            requester, category, affected_system, impact, urgency, priority,
            description, reasoning, int(red_flag_matched), red_flag_category,
            red_flag_phrase, extraction_provider,
        ),
    )
    return cur.lastrowid


def get_ticket(conn: sqlite3.Connection, ticket_id: int):
    return conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()


def list_tickets(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
):
    """Priority-sorted ticket list.

    `limit` is optional here (the CLI reasonably wants everything on a small
    local DB) but the API always passes one — an unbounded SELECT over a
    forever-growing table is a latency problem that only shows up months into
    a deployment.
    """
    priority_order = (
        "CASE priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 "
        "WHEN 'P3' THEN 3 WHEN 'P4' THEN 4 ELSE 5 END"
    )
    clauses = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    sql = f"SELECT * FROM tickets {where} ORDER BY {priority_order}, created_at"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
    return conn.execute(sql, params).fetchall()


def acknowledge_ticket(conn: sqlite3.Connection, ticket_id: int) -> None:
    conn.execute(
        "UPDATE tickets SET status = 'acknowledged', acknowledged_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (ticket_id,),
    )


def resolve_ticket(conn: sqlite3.Connection, ticket_id: int) -> None:
    conn.execute(
        "UPDATE tickets SET status = 'resolved', updated_at = datetime('now') WHERE id = ?",
        (ticket_id,),
    )


def unacknowledged_p1_tickets(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM tickets WHERE priority = 'P1' AND status = 'open' ORDER BY created_at"
    ).fetchall()


# --- Alert outbox ---------------------------------------------------------


def enqueue_alert(conn: sqlite3.Connection, ticket_id: int, kind: str, message: str) -> int:
    """Records an alert intent. MUST be called inside the same transaction as
    the ticket insert so the intent is durable before any delivery attempt."""
    cur = conn.execute(
        "INSERT INTO alert_outbox (ticket_id, kind, message) VALUES (?, ?, ?)",
        (ticket_id, kind, message),
    )
    return cur.lastrowid


def pending_alerts(conn: sqlite3.Connection, limit: int = 50):
    return conn.execute(
        "SELECT * FROM alert_outbox WHERE status = 'pending' ORDER BY created_at LIMIT ?",
        (limit,),
    ).fetchall()


def mark_alert_sent(conn: sqlite3.Connection, outbox_id: int, attempts: int) -> None:
    conn.execute(
        """UPDATE alert_outbox
           SET status = 'sent', attempts = ?, sent_at = datetime('now'),
               last_attempt_at = datetime('now'), last_error = NULL
           WHERE id = ?""",
        (attempts, outbox_id),
    )


def mark_alert_attempt_failed(
    conn: sqlite3.Connection, outbox_id: int, attempts: int, error: str, give_up: bool
) -> None:
    """Records a failed delivery attempt.

    `give_up` moves the row to 'failed' so the sweeper stops retrying forever.
    A 'failed' row is deliberately NOT deleted — it's the record that a page
    was owed and never delivered, which is exactly what an operator needs to
    see. /ready surfaces the count.
    """
    conn.execute(
        """UPDATE alert_outbox
           SET status = ?, attempts = ?, last_error = ?, last_attempt_at = datetime('now')
           WHERE id = ?""",
        ("failed" if give_up else "pending", attempts, error[:500], outbox_id),
    )


def outbox_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM alert_outbox GROUP BY status"
    ).fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    return {
        "pending": counts.get("pending", 0),
        "sent": counts.get("sent", 0),
        "failed": counts.get("failed", 0),
    }


def minutes_since_last_escalation(conn: sqlite3.Connection, ticket_id: int) -> Optional[float]:
    """Minutes since the last escalation alert was ENQUEUED for this ticket, or
    None if there hasn't been one.

    Used to enforce a cooldown. Without it, check_escalations() re-alerts every
    invocation — on a one-minute scheduler an unacknowledged P1 pages Slack
    every minute indefinitely, which trains everyone to mute the channel.
    """
    row = conn.execute(
        """SELECT (julianday('now') - julianday(MAX(created_at))) * 24 * 60 AS mins
           FROM alert_outbox
           WHERE ticket_id = ? AND kind = 'escalation'""",
        (ticket_id,),
    ).fetchone()
    if row is None or row["mins"] is None:
        return None
    return float(row["mins"])


def insert_kb_article(conn: sqlite3.Connection, title: str, body: str, category: str, embedding) -> int:
    blob = pack_embedding(embedding) if embedding is not None else None
    dim = len(embedding) if embedding is not None else None
    cur = conn.execute(
        "INSERT INTO kb_articles (title, body, category, embedding, embedding_dim) VALUES (?, ?, ?, ?, ?)",
        (title, body, category, blob, dim),
    )
    return cur.lastrowid


def all_kb_articles(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM kb_articles").fetchall()


def log_deflection(
    conn: sqlite3.Connection, query_text: str, kb_article_id: Optional[int], similarity_score: Optional[float], resolved: Optional[bool]
) -> int:
    cur = conn.execute(
        "INSERT INTO deflections (query_text, kb_article_id, similarity_score, resolved) VALUES (?, ?, ?, ?)",
        (query_text, kb_article_id, similarity_score, None if resolved is None else int(resolved)),
    )
    return cur.lastrowid


def deflection_stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) c FROM deflections").fetchone()["c"]
    resolved = conn.execute("SELECT COUNT(*) c FROM deflections WHERE resolved = 1").fetchone()["c"]
    tickets_created = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
    return {
        "self_service_offers": total,
        "self_service_resolved": resolved,
        "tickets_created": tickets_created,
        "deflection_rate": round(resolved / total, 3) if total else 0.0,
    }
