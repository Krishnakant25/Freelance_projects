"""
SQL safety tests — the boundary that makes this system safe to point at a real
warehouse.

Architecture doc §6.2: "read-only enforced in a prompt is not read-only." These
tests assert the boundary holds at the DATABASE layer, where it can't be talked
around, and that user-supplied values are parameters rather than string
concatenation.

Run:  python eval/test_sql_safety.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

_harness.quiet_logs()
_harness.ensure_warehouse()

from app import answer as answer_mod  # noqa: E402
from app import config, guardrails, selector, semantic, sql_builder, warehouse  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


MODEL = answer_mod.get_model()


# --- 1. Read-only is enforced by the driver ------------------------------


def test_writes_refused_at_connection_level():
    """Not "the SQL didn't start with SELECT" — the connection itself refuses."""
    print("\n[read-only] every write statement is refused by the connection")
    results = guardrails.verify_read_only()
    for label, outcome in results.items():
        check(f"{label} refused", "refused" in outcome, outcome)


def test_attach_is_blocked():
    """ATTACH is how a read-only connection could otherwise reach a WRITABLE
    file and escape the boundary entirely."""
    print("\n[read-only] ATTACH DATABASE is blocked (would bypass mode=ro)")
    conn = warehouse.read_only_connection()
    try:
        raised = False
        try:
            conn.execute("ATTACH DATABASE ':memory:' AS scratch")
        except Exception:  # noqa: BLE001
            raised = True
        check("ATTACH raises", raised, "a read-only connection could attach a writable db")
    finally:
        conn.close()


# --- 2. Statement restrictions -------------------------------------------


def test_stacked_statements_refused():
    print("\n[statements] stacked statements are refused")
    for sql in [
        "SELECT 1; DROP TABLE orders",
        "SELECT 1;DELETE FROM orders",
        "SELECT 1; SELECT 2",
    ]:
        raised = False
        try:
            guardrails.execute(sql)
        except guardrails.GuardrailViolation:
            raised = True
        except Exception:  # noqa: BLE001
            raised = True
        check(f"refused: {sql[:34]!r}", raised)


def test_non_select_refused():
    print("\n[statements] non-read statements are refused before execution")
    for sql in [
        "DELETE FROM orders",
        "UPDATE orders SET gross_amount = 0",
        "DROP TABLE orders",
        "PRAGMA writable_schema = 1",
        "CREATE TABLE evil (x INT)",
    ]:
        raised = False
        try:
            guardrails.execute(sql)
        except Exception:  # noqa: BLE001
            raised = True
        check(f"refused: {sql[:34]!r}", raised)


def test_plain_select_still_works():
    """The guardrails must not be so strict that legitimate queries fail —
    otherwise it's broken rather than safe."""
    print("\n[statements] a legitimate SELECT still runs")
    result = guardrails.execute("SELECT COUNT(*) AS c FROM orders")
    check("SELECT executes", result.row_count == 1, str(result.rows))
    check("returns real data", result.rows[0]["c"] > 0, str(result.rows))

    with_result = guardrails.execute("WITH x AS (SELECT 1 AS n) SELECT n FROM x")
    check("WITH (CTE) executes", with_result.rows[0]["n"] == 1, str(with_result.rows))


# --- 3. Injection through filter values ---------------------------------


def test_filter_values_are_parameterised():
    """A malicious dimension VALUE must be inert. Values come from the question,
    so they're the one part of the query influenced by untrusted input — they are
    bound as parameters, never concatenated."""
    print("\n[injection] filter values are bound parameters, not concatenated")
    nasty = "web'; DROP TABLE orders; --"
    selection = semantic.Selection(
        metrics=["net_revenue"],
        filters=[semantic.Filter(dimension="channel", operator="in", values=[nasty])],
        date_range="all_time",
    )
    built = sql_builder.build(selection, MODEL)

    check("the payload is NOT present in the SQL text", nasty not in built.sql, built.sql)
    check("a placeholder is used instead", "?" in built.sql, built.sql)
    check("the payload is passed as a parameter", nasty in built.params, str(built.params))

    # And it must execute harmlessly, returning no rows rather than dropping a table.
    result = guardrails.execute(built.sql, built.params)
    check("executes without error", result is not None)

    after = guardrails.execute("SELECT COUNT(*) c FROM orders")
    check("orders table still exists and is populated", after.rows[0]["c"] > 0, str(after.rows))


def test_llm_supplied_injection_is_inert():
    """Same attack, but arriving via the LLM selector rather than constructed by
    hand — the realistic path."""
    print("\n[injection] an injection attempt from the model is inert")
    original = config.LLM_PROVIDER
    try:
        config.LLM_PROVIDER = "mock"
        selector.set_mock_behaviour("sql_injection_attempt")
        result = answer_mod.ask("revenue by channel")
        check("query completed or refused cleanly", result is not None)
        after = guardrails.execute("SELECT COUNT(*) c FROM orders")
        check("orders table intact", after.rows[0]["c"] > 0, str(after.rows))
    finally:
        selector.set_mock_behaviour("normal")
        config.LLM_PROVIDER = original


# --- 4. Cost and row ceilings -------------------------------------------


def test_row_cap_applied():
    print("\n[limits] every generated query carries a LIMIT")
    selection = semantic.Selection(metrics=["net_revenue"], dimensions=["order_date"], date_range="all_time")
    built = sql_builder.build(selection, MODEL)
    check("LIMIT present in SQL", "LIMIT" in built.sql, built.sql)

    huge = semantic.Selection(
        metrics=["net_revenue"], dimensions=["order_date"], date_range="all_time", limit=999_999
    )
    built_huge = sql_builder.build(huge, MODEL)
    check(
        "requested limit is capped at MAX_RESULT_ROWS",
        f"LIMIT {config.MAX_RESULT_ROWS}" in built_huge.sql,
        built_huge.sql,
    )
    check(
        "the cap is disclosed as an assumption",
        any("capped" in a for a in built_huge.assumptions),
        str(built_huge.assumptions),
    )


def test_scan_ceiling_refuses_expensive_query():
    """Stands in for a bytes-scanned ceiling on a real warehouse, where one bad
    generated query can cost hundreds of dollars."""
    print("\n[limits] a query above the scan ceiling is refused BEFORE running")
    original = config.MAX_SCAN_ROWS
    try:
        config.MAX_SCAN_ROWS = 10  # force a refusal
        raised = False
        message = ""
        try:
            guardrails.execute("SELECT COUNT(*) c FROM orders")
        except guardrails.GuardrailViolation as e:
            raised = True
            message = str(e)
        check("refused", raised, "expensive query was allowed to run")
        check("message explains the ceiling and how to fix it",
              "ceiling" in message.lower() and "narrow" in message.lower(), message)
    finally:
        config.MAX_SCAN_ROWS = original


def test_scan_estimate_is_reported():
    print("\n[limits] scan estimate is reported with results (cost visibility)")
    result = guardrails.execute("SELECT COUNT(*) c FROM orders")
    check("scan estimate present", result.scan_estimate > 0, str(result.scan_estimate))
    check("query plan captured", len(result.plan) > 0, str(result.plan))


# --- 5. Joins can't be invented ----------------------------------------


def test_only_model_joins_are_emitted():
    """The classic free-form NL→SQL failure is a plausible but wrong join. Here
    joins come only from the model, and prerequisites are followed."""
    print("\n[joins] only joins declared in the model appear, with prerequisites")
    selection = semantic.Selection(
        metrics=["units_sold"], dimensions=["product_category"], date_range="last_30_days"
    )
    built = sql_builder.build(selection, MODEL)
    check("order_items join present", "order_items oi" in built.sql, built.sql)
    check("products join present", "products p" in built.sql, built.sql)
    check(
        "order_items appears BEFORE products (prerequisite order)",
        built.sql.index("order_items oi") < built.sql.index("products p"),
        built.sql,
    )
    check("no undeclared table appears", "sqlite_master" not in built.sql.lower())


def test_unknown_field_rejected_not_guessed():
    print("\n[validation] an unknown metric/dimension is rejected, never guessed")
    for selection, label in [
        (semantic.Selection(metrics=["profit_margin"]), "unknown metric"),
        (semantic.Selection(metrics=["net_revenue"], dimensions=["salesperson"]), "unknown dimension"),
        (semantic.Selection(metrics=["net_revenue"], date_range="last_fortnight"), "unknown date range"),
        (semantic.Selection(metrics=["net_revenue"], time_grain="fortnight"), "unknown grain"),
    ]:
        raised = False
        try:
            semantic.validate_selection(selection, MODEL)
        except semantic.SelectionError:
            raised = True
        check(f"{label} rejected", raised)


def main():
    print("=" * 78)
    print("SQL safety: read-only boundary, injection, cost ceilings, join integrity")
    print("=" * 78)

    test_writes_refused_at_connection_level()
    test_attach_is_blocked()
    test_stacked_statements_refused()
    test_non_select_refused()
    test_plain_select_still_works()
    test_filter_values_are_parameterised()
    test_llm_supplied_injection_is_inert()
    test_row_cap_applied()
    test_scan_ceiling_refuses_expensive_query()
    test_scan_estimate_is_reported()
    test_only_model_joins_are_emitted()
    test_unknown_field_rejected_not_guessed()

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("The warehouse cannot be written to, and untrusted values cannot reach SQL text.")


if __name__ == "__main__":
    main()
