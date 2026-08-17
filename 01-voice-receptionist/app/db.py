"""
SQLite storage: calls, transcripts, appointment slots, bookings, audit log.

The slot/booking schema is where the double-booking fix lives. Read the
comments on `slots` and `bookings` before changing either — the UNIQUE
constraints are load-bearing, not decorative.
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid TEXT UNIQUE,              -- transport-provided id (Twilio SID, or a local uuid)
    caller_number TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    outcome TEXT,                       -- booked | rescheduled | cancelled | faq_answered
                                        -- | callback_requested | transferred | abandoned | failed
    turns INTEGER NOT NULL DEFAULT 0,
    confusion_count INTEGER NOT NULL DEFAULT 0,
    disclosed_ai INTEGER NOT NULL DEFAULT 0,
    disclosed_recording INTEGER NOT NULL DEFAULT 0,
    transferred INTEGER NOT NULL DEFAULT 0,
    summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_outcome ON calls(outcome);
CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_index INTEGER NOT NULL,
    speaker TEXT NOT NULL,              -- caller | agent
    text TEXT NOT NULL,
    state TEXT,                         -- dialogue state when this turn was produced
    latency_ms REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_turns_call ON turns(call_id);

-- Appointment inventory. One row per bookable slot.
--
-- `held_until` + `held_by_call` implement a two-phase reserve->confirm. A
-- reservation is a TENTATIVE claim that expires, so an abandoned call cannot
-- block a slot permanently, while a caller still gets exclusive access long
-- enough to finish giving their details.
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    starts_at TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    -- The load-bearing constraint. Two callers racing for the same time cannot
    -- both end up with a slot, because there is only ever ONE row per start
    -- time and claiming it is a conditional UPDATE (see calendar_tool.py).
    UNIQUE (starts_at)
);

CREATE INDEX IF NOT EXISTS idx_slots_starts ON slots(starts_at);

CREATE TABLE IF NOT EXISTS slot_holds (
    slot_id INTEGER PRIMARY KEY REFERENCES slots(id) ON DELETE CASCADE,
    held_by_call TEXT NOT NULL,
    held_until TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT UNIQUE NOT NULL,
    -- A slot can have at most ONE active booking. Enforced by a partial unique
    -- index below rather than in application code, so a logic bug upstream
    -- still cannot produce a double-booked slot.
    slot_id INTEGER NOT NULL REFERENCES slots(id),
    call_sid TEXT,
    -- Idempotency: retrying the same logical booking (network retry, caller
    -- repeating themselves, agent re-invoking the tool) returns the EXISTING
    -- booking instead of creating a second one.
    idempotency_key TEXT UNIQUE,
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    service TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed',   -- confirmed | cancelled
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    cancelled_at TEXT
);

-- The real double-booking guard: at most one CONFIRMED booking per slot.
-- SQLite supports partial indexes, so cancelled bookings don't block rebooking.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_booking_per_slot
    ON bookings(slot_id) WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS idx_bookings_phone ON bookings(customer_phone);

CREATE TABLE IF NOT EXISTS callbacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid TEXT,
    customer_name TEXT,
    customer_phone TEXT NOT NULL,
    reason TEXT,
    urgency TEXT NOT NULL DEFAULT 'normal',   -- normal | urgent
    status TEXT NOT NULL DEFAULT 'open',      -- open | handled
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_callbacks_status ON callbacks(status);

CREATE TABLE IF NOT EXISTS faq_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    embedding BLOB,
    embedding_dim INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only, enforced by triggers. Same reasoning as the helpdesk project:
-- an audit log a bug can quietly rewrite provides no assurance while looking
-- like it does.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    call_sid TEXT,
    event_type TEXT NOT NULL,
    details TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_call ON audit_log(call_sid);

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


import struct


def pack_embedding(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def unpack_embedding(blob: bytes, dim: int):
    return struct.unpack(f"{dim}f", blob)


def get_connection() -> sqlite3.Connection:
    """WAL + busy_timeout are required here, not optional: the concurrency test
    races multiple callers at one slot, which is exactly the contention that
    default SQLite settings turn into 'database is locked' errors."""
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


@contextmanager
def immediate_session():
    """Transaction that takes the write lock UP FRONT (BEGIN IMMEDIATE).

    Needed for the slot-claim path: with a deferred transaction, two callers
    can both read "slot is free" before either writes, and SQLite only detects
    the conflict at commit — producing a confusing failure late instead of a
    clean serialization early. BEGIN IMMEDIATE makes the claim genuinely
    atomic read-then-write.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def log_event(conn: sqlite3.Connection, event_type: str, details: dict, call_sid: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (call_sid, event_type, details) VALUES (?, ?, ?)",
        (call_sid, event_type, json.dumps(details, default=str)),
    )
