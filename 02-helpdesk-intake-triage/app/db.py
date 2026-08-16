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

-- Append-only. Never UPDATE or DELETE from this table — that's what makes
-- "why did this get classified this way" answerable after the fact.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    ticket_id INTEGER,
    event_type TEXT NOT NULL,
    details TEXT NOT NULL   -- JSON
);
"""


def pack_embedding(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def unpack_embedding(blob: bytes, dim: int):
    return struct.unpack(f"{dim}f", blob)


def get_connection() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
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


def list_tickets(conn: sqlite3.Connection, status: Optional[str] = None):
    priority_order = "CASE priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 WHEN 'P4' THEN 4 ELSE 5 END"
    if status:
        return conn.execute(
            f"SELECT * FROM tickets WHERE status = ? ORDER BY {priority_order}, created_at", (status,)
        ).fetchall()
    return conn.execute(f"SELECT * FROM tickets ORDER BY {priority_order}, created_at").fetchall()


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
