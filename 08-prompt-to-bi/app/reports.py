"""
Frozen scheduled reports.

Architecture doc §6.5, and this is subtle enough to be worth stating plainly:

  If a recurring report REGENERATES its query from the question on every run,
  the query can change between runs — a different join, a different date
  boundary — and the week-over-week trend moves for reasons that have nothing
  to do with the business. That is the worst possible failure in a recurring
  executive report, because the number looks authoritative and the change looks
  like a business event.

So: once a question is approved for recurring delivery, the SELECTION is pinned
as a versioned artifact. Re-running executes the pinned selection. The question
text is kept only for display. Changing a pinned report is an explicit,
audited version bump — never a side effect of asking again.

Regeneration remains available for ad-hoc exploration; it just isn't what
scheduled reports do.
"""
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from . import answer as answer_mod
from . import config, semantic

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    question TEXT NOT NULL,          -- kept for display only, NOT re-interpreted
    selection_json TEXT NOT NULL,    -- the PINNED artifact that actually runs
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS report_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    selection_json TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    note TEXT
);

CREATE TABLE IF NOT EXISTS report_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    ran_at TEXT NOT NULL DEFAULT (datetime('now')),
    sql TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    primary_value REAL,
    ok INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_runs_report ON report_runs(report_id, ran_at);

-- Version history is append-only: the record of what a report USED to compute
-- is exactly what you need when a number changes and nobody remembers why.
CREATE TRIGGER IF NOT EXISTS report_versions_no_update
BEFORE UPDATE ON report_versions
BEGIN
    SELECT RAISE(ABORT, 'report_versions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS report_versions_no_delete
BEFORE DELETE ON report_versions
BEGIN
    SELECT RAISE(ABORT, 'report_versions is append-only');
END;
"""


def _connect() -> sqlite3.Connection:
    config.APP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.APP_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _selection_to_json(selection: semantic.Selection) -> str:
    return json.dumps(asdict(selection), sort_keys=True)


def _selection_from_json(raw: str) -> semantic.Selection:
    data = json.loads(raw)
    filters = [semantic.Filter(**f) for f in data.get("filters", [])]
    data["filters"] = filters
    return semantic.Selection(**data)


class ReportError(RuntimeError):
    pass


def pin(name: str, question: str, model: Optional[semantic.SemanticModel] = None) -> dict:
    """Interprets a question ONCE and pins the resulting selection.

    Refuses to pin a question that can't be answered — a scheduled report that
    silently returns nothing is worse than one that was never created.
    """
    model = model or answer_mod.get_model()
    from .selector import Refusal, select

    result = select(question, model)
    if isinstance(result, Refusal):
        raise ReportError(f"cannot pin: {result.message}")
    if not isinstance(result, semantic.Selection):
        raise ReportError("cannot pin: question needs clarification first")

    payload = _selection_to_json(result)
    with session() as conn:
        existing = conn.execute("SELECT * FROM reports WHERE name = ?", (name,)).fetchone()
        if existing:
            raise ReportError(f"report {name!r} already exists — use repin() to change it")
        cur = conn.execute(
            "INSERT INTO reports (name, question, selection_json) VALUES (?, ?, ?)",
            (name, question, payload),
        )
        report_id = cur.lastrowid
        conn.execute(
            "INSERT INTO report_versions (report_id, version, selection_json, note) VALUES (?, 1, ?, ?)",
            (report_id, payload, "initial pin"),
        )
    return {"name": name, "version": 1, "selection": json.loads(payload)}


def repin(name: str, question: str, note: str = "", model: Optional[semantic.SemanticModel] = None) -> dict:
    """Explicitly changes what a report computes, bumping the version.

    This is the ONLY way a pinned report changes. Deliberately noisy: a version
    bump with a note is what lets someone answer "why did this number move?"
    six months later.
    """
    model = model or answer_mod.get_model()
    from .selector import Refusal, select

    result = select(question, model)
    if isinstance(result, Refusal):
        raise ReportError(f"cannot repin: {result.message}")

    payload = _selection_to_json(result)
    with session() as conn:
        row = conn.execute("SELECT * FROM reports WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ReportError(f"no report named {name!r}")
        new_version = row["version"] + 1
        conn.execute(
            """UPDATE reports SET question = ?, selection_json = ?, version = ?,
                                  updated_at = datetime('now')
               WHERE id = ?""",
            (question, payload, new_version, row["id"]),
        )
        conn.execute(
            "INSERT INTO report_versions (report_id, version, selection_json, note) VALUES (?, ?, ?, ?)",
            (row["id"], new_version, payload, note or "repinned"),
        )
    return {"name": name, "version": new_version, "selection": json.loads(payload)}


def run(name: str, model: Optional[semantic.SemanticModel] = None) -> answer_mod.Answer:
    """Executes the PINNED selection. Does not re-interpret the question."""
    model = model or answer_mod.get_model()
    with session() as conn:
        row = conn.execute("SELECT * FROM reports WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ReportError(f"no report named {name!r}")
        report_id, version = row["id"], row["version"]
        selection = _selection_from_json(row["selection_json"])
        question = row["question"]

    result = answer_mod.run_selection(selection, question=question, model=model)

    primary = None
    if result.ok and result.rows:
        metric_cols = [c for c in result.columns if c in model.metrics]
        if metric_cols and len(result.rows) == 1:
            primary = result.rows[0].get(metric_cols[0])

    with session() as conn:
        conn.execute(
            """INSERT INTO report_runs (report_id, version, sql, row_count, primary_value, ok)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (report_id, version, result.sql, result.row_count, primary, int(result.ok)),
        )
    return result


def list_reports() -> list[dict]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM reports ORDER BY name").fetchall()
        out = []
        for r in rows:
            runs = conn.execute(
                "SELECT COUNT(*) c, MAX(ran_at) last FROM report_runs WHERE report_id = ?", (r["id"],)
            ).fetchone()
            out.append({
                "name": r["name"],
                "question": r["question"],
                "version": r["version"],
                "runs": runs["c"],
                "last_run": runs["last"],
            })
    return out


def history(name: str) -> dict:
    with session() as conn:
        row = conn.execute("SELECT * FROM reports WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ReportError(f"no report named {name!r}")
        versions = conn.execute(
            "SELECT version, changed_at, note FROM report_versions WHERE report_id = ? ORDER BY version",
            (row["id"],),
        ).fetchall()
        runs = conn.execute(
            """SELECT version, ran_at, row_count, primary_value, ok
               FROM report_runs WHERE report_id = ? ORDER BY ran_at DESC LIMIT 20""",
            (row["id"],),
        ).fetchall()
    return {
        "name": name,
        "question": row["question"],
        "current_version": row["version"],
        "versions": [dict(v) for v in versions],
        "recent_runs": [dict(r) for r in runs],
    }


def delete(name: str) -> bool:
    with session() as conn:
        cur = conn.execute("DELETE FROM reports WHERE name = ?", (name,))
        return cur.rowcount > 0
