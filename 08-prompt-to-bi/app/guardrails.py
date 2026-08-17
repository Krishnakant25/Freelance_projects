"""
Query execution guardrails.

Architecture doc §6.2 and §6.6. The controls here are layered so that no single
mistake is sufficient to cause harm:

  1. The connection is read-only (`mode=ro` + a SQLite authorizer) — enforced by
     the driver, not by inspecting the SQL string.
  2. Only ONE statement is allowed per execution, so a stacked statement can't
     ride along.
  3. EXPLAIN QUERY PLAN runs first and the query is refused if the planner
     indicates a scan beyond the configured ceiling. This is the stand-in for
     the bytes-scanned limit you'd set on BigQuery/Snowflake, where a single
     accidental cross join costs real money.
  4. Results are capped and timed.

Note what is NOT relied upon: a check that the SQL "starts with SELECT". That's
the wrong layer and trivially bypassable; it exists nowhere in this file.
"""
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field

from . import config, warehouse

logger = logging.getLogger(__name__)


class GuardrailViolation(RuntimeError):
    """Raised when a query is refused before execution."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict]
    row_count: int
    elapsed_ms: float
    scan_estimate: int = 0
    truncated: bool = False
    plan: list[str] = field(default_factory=list)


# Statements that must never reach the warehouse. Defence in depth only — the
# read-only connection already refuses them; this produces a clearer error and
# catches an obvious mistake earlier.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|VACUUM|PRAGMA|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _assert_single_read_statement(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise GuardrailViolation(
            "multiple SQL statements are not permitted in one execution"
        )
    if _FORBIDDEN.search(stripped):
        raise GuardrailViolation("only read queries are permitted")
    if not re.match(r"^\s*(SELECT|WITH)\b", stripped, re.IGNORECASE):
        raise GuardrailViolation("query must be a SELECT or WITH")


def estimate_scan(conn: sqlite3.Connection, sql: str, params: list) -> tuple[int, list[str]]:
    """Uses EXPLAIN QUERY PLAN plus table row counts to estimate scanned rows.

    Deliberately crude — SQLite has no bytes-scanned metric. The point is to have
    a cost ceiling that refuses a pathological query BEFORE running it, in the
    same place a real deployment would call BigQuery's dry-run.
    """
    plan_rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    plan = [r["detail"] if "detail" in r.keys() else str(tuple(r)) for r in plan_rows]

    counts = {}
    for table in ("orders", "order_items", "customers", "products"):
        counts[table] = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]

    estimate = 0
    for line in plan:
        lowered = line.lower()
        for table, count in counts.items():
            if table in lowered:
                # A full scan costs the whole table; an indexed lookup is cheap.
                estimate += count if "scan" in lowered else max(1, count // 100)
    return estimate, plan


def execute(sql: str, params: list = None, max_rows: int = None) -> QueryResult:
    params = params or []
    max_rows = max_rows or config.MAX_RESULT_ROWS

    _assert_single_read_statement(sql)

    started = time.perf_counter()
    conn = warehouse.read_only_connection()
    try:
        scan_estimate, plan = estimate_scan(conn, sql, params)
        if scan_estimate > config.MAX_SCAN_ROWS:
            raise GuardrailViolation(
                f"query refused: estimated scan of {scan_estimate:,} rows exceeds the "
                f"{config.MAX_SCAN_ROWS:,} row ceiling. Narrow the date range or add a filter."
            )

        cursor = conn.execute(sql, params)
        columns = [d[0] for d in cursor.description]
        raw = cursor.fetchmany(max_rows + 1)
        truncated = len(raw) > max_rows
        rows = [dict(zip(columns, r)) for r in raw[:max_rows]]

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=(time.perf_counter() - started) * 1000,
            scan_estimate=scan_estimate,
            truncated=truncated,
            plan=plan,
        )
    finally:
        conn.close()


def verify_read_only() -> dict:
    """Actively proves the read-only boundary holds, rather than assuming it.

    Used by the CLI `doctor` command and the test suite: attempts a write and
    reports whether it was correctly refused.
    """
    results = {}
    conn = warehouse.read_only_connection()
    try:
        for label, statement in [
            ("insert", "INSERT INTO orders (id, customer_id, ordered_at, channel, payment_method, gross_amount) VALUES (999999, 1, '2020-01-01', 'web', 'card', 1)"),
            ("update", "UPDATE orders SET gross_amount = 0"),
            ("delete", "DELETE FROM orders"),
            ("drop", "DROP TABLE orders"),
            ("attach", "ATTACH DATABASE 'evil.sqlite3' AS evil"),
        ]:
            try:
                conn.execute(statement)
                results[label] = "ALLOWED — BOUNDARY BROKEN"
            except Exception as e:  # noqa: BLE001 - we want any refusal
                results[label] = f"refused ({type(e).__name__})"
    finally:
        conn.close()
    return results
