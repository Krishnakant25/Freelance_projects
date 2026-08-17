"""
Regression tests for the production audit.

Each test corresponds to a defect found by auditing the running system, not by
reading the code. All five would have passed silently before their fix — the
test suite was green while every one of them was present, which is the argument
for doing an adversarial pass separately from writing tests.

Run:  python eval/test_production_fixes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

_harness.quiet_logs()
_harness.ensure_warehouse()

from app import answer as answer_mod  # noqa: E402
from app import config, guardrails, reports, selector, semantic  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


MODEL = answer_mod.get_model()


# --- 1. Scheduled reports must not serve cached data --------------------


def test_reports_bypass_the_result_cache():
    """A scheduled report exists to deliver FRESH data. Serving it from the
    interactive cache means Monday's report can silently be Friday's numbers —
    a stale figure that still looks authoritative."""
    print("\n[stale data] scheduled reports query fresh, never from cache")
    original = config.CACHE_ENABLED
    try:
        config.CACHE_ENABLED = True
        answer_mod.clear_cache()
        _harness.reset_app_db()
        reports.pin("fresh-check", "net revenue last month")

        runs = [reports.run("fresh-check") for _ in range(3)]
        check("no report run is served from cache",
              all(not r.cached for r in runs),
              f"cached flags: {[r.cached for r in runs]}")
        check("all runs succeeded", all(r.ok for r in runs))
        check("results are still consistent (determinism, not caching)",
              len({r.sql for r in runs}) == 1)
    finally:
        config.CACHE_ENABLED = original
        answer_mod.clear_cache()


def test_interactive_questions_still_use_cache():
    """The fix must not disable caching wholesale — interactive repeats should
    still be fast."""
    print("\n[cache] interactive questions DO still hit the cache")
    original = config.CACHE_ENABLED
    try:
        config.CACHE_ENABLED = True
        answer_mod.clear_cache()
        first = answer_mod.ask("net revenue last month")
        second = answer_mod.ask("net revenue last month")
        check("first ask is a miss", first.cached is False)
        check("second ask is a hit", second.cached is True)
    finally:
        config.CACHE_ENABLED = original
        answer_mod.clear_cache()


# --- 2. Cache must be size-bounded -------------------------------------


def test_cache_is_size_bounded():
    """Cached payloads contain full ROW SETS, so a TTL alone isn't enough: a busy
    day of distinct questions grew the cache forever and never released it."""
    print("\n[memory] the result cache evicts instead of growing without bound")
    original_enabled = config.CACHE_ENABLED
    original_max = config.CACHE_MAX_ENTRIES
    try:
        config.CACHE_ENABLED = True
        config.CACHE_MAX_ENTRIES = 5
        answer_mod.clear_cache()

        for i in range(25):
            answer_mod.run_selection(
                semantic.Selection(metrics=["net_revenue"], date_range="last_30_days", limit=i + 1),
                model=MODEL,
            )
        stats = answer_mod.cache_stats()
        check("entry count respects the cap", stats["entries"] <= 5, str(stats))
        check("eviction actually happened", stats["evictions"] > 0, str(stats))
        check("cap is reported for observability", stats["max_entries"] == 5, str(stats))
    finally:
        config.CACHE_ENABLED = original_enabled
        config.CACHE_MAX_ENTRIES = original_max
        answer_mod.clear_cache()


# --- 3. Filter dimensions must not become groupings --------------------


def test_named_filter_dimension_does_not_group():
    """'revenue by region from the phone channel' groups by region ONLY.

    The earlier logic checked whether the question contained "by" ANYWHERE, so
    the literal word "channel" plus a "by" produced a grouping on channel too —
    answering a different question than the one asked.
    """
    print("\n[over-grouping] a filtered dimension only groups if asked for after 'by'")
    cases = [
        ("revenue by region from the phone channel today", ["region"], ["channel"]),
        ("revenue by region from the mobile app last month", ["region"], ["channel"]),
        ("how much revenue came from the mobile app last month?", [], ["channel"]),
        ("revenue by channel and region last month", ["channel", "region"], []),
    ]
    for question, expect_dims, expect_filters in cases:
        s = selector.select(question, MODEL)
        check(
            f"dims={expect_dims} for {question[:44]!r}",
            sorted(s.dimensions) == sorted(expect_dims),
            f"got {s.dimensions}",
        )
        check(
            f"  ...filters={expect_filters}",
            sorted(f.dimension for f in s.filters) == sorted(expect_filters),
            f"got {[f.dimension for f in s.filters]}",
        )


def test_explicit_grouping_on_filtered_dimension_is_honoured():
    """If someone genuinely asks to group by the thing they also filtered on,
    that's a legitimate (if unusual) request and must still work."""
    print("\n[over-grouping] an explicit 'by channel' still groups, even when filtered")
    s = selector.select("revenue by channel from the mobile app last month", MODEL)
    check("channel IS grouped (explicitly requested)", "channel" in s.dimensions, str(s.dimensions))
    check("channel is also filtered", any(f.dimension == "channel" for f in s.filters),
          str([f.dimension for f in s.filters]))


# --- 4. Empty results must be explicable -------------------------------


def test_empty_result_is_a_clear_answer_not_a_blank():
    """A zero-row answer printed bare table headers, which reads like a bug. The
    query was valid; the period simply has no matching rows, and the derivation
    still needs showing because 'why is this empty?' is a question about filters."""
    print("\n[empty results] a zero-row answer is still a complete, explicable answer")
    result = answer_mod.ask("revenue by region from the phone channel today")
    check("query succeeded (not refused)", result.ok and not result.refused, result.message)
    check("row_count is zero", result.row_count == 0, str(result.row_count))
    check("chart type reports empty", result.chart.get("type") == "empty", str(result.chart))
    check("derivation is still present", bool(result.sql), "no SQL returned for an empty result")
    check("filters are still shown (needed to explain the emptiness)",
          bool(result.filters_applied), str(result.filters_applied))
    check("date range still shown", bool(result.date_range), result.date_range)


# --- 5. The cost check must not itself be expensive --------------------


def test_table_counts_are_memoized():
    """The cost estimator recomputed four COUNT(*) scans on EVERY query, making
    the cost check one of the more expensive things the system did."""
    print("\n[performance] cost-estimator table counts are computed once, not per query")
    original = config.CACHE_ENABLED
    try:
        config.CACHE_ENABLED = False  # force real execution each time
        guardrails.invalidate_table_counts()
        check("counts start empty", guardrails._cached_counts is None)

        answer_mod.ask("net revenue last month")
        after_first = guardrails._cached_counts
        check("counts populated after first query", after_first is not None)

        snapshot = dict(after_first)
        for _ in range(5):
            answer_mod.ask("order count last month")
        check("same counts reused across subsequent queries",
              guardrails._cached_counts == snapshot, str(guardrails._cached_counts))
    finally:
        config.CACHE_ENABLED = original


def test_reseed_invalidates_counts():
    """Memoizing is only correct if a re-seed clears it — otherwise the cost
    estimator would use stale table sizes forever."""
    print("\n[performance] re-seeding the warehouse invalidates memoized counts")
    from app import warehouse

    answer_mod.ask("net revenue last month")
    check("counts are populated", guardrails._cached_counts is not None)
    warehouse.seed()
    check("counts cleared by seed()", guardrails._cached_counts is None,
          str(guardrails._cached_counts))


def main():
    print("=" * 78)
    print("Production audit regressions")
    print("=" * 78)

    test_reports_bypass_the_result_cache()
    test_interactive_questions_still_use_cache()
    test_cache_is_size_bounded()
    test_named_filter_dimension_does_not_group()
    test_explicit_grouping_on_filtered_dimension_is_honoured()
    test_empty_result_is_a_clear_answer_not_a_blank()
    test_table_counts_are_memoized()
    test_reseed_invalidates_counts()

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("All five audit findings are pinned by regressions.")


if __name__ == "__main__":
    main()
