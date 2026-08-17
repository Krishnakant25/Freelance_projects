"""
Frozen scheduled report tests — architecture doc §6.5.

The failure being prevented: a recurring report that REGENERATES its query each
run can silently change what it computes, so a week-over-week trend moves for
reasons unrelated to the business. In an executive report that's the worst
possible failure, because the number looks authoritative and the change looks
like a business event.

Run:  python eval/test_reports.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402

_harness.quiet_logs()
_harness.ensure_warehouse()

from app import answer as answer_mod  # noqa: E402
from app import config, reports, selector, semantic, sql_builder  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail if not cond else ''}")


MODEL = answer_mod.get_model()


def test_same_selection_produces_identical_sql():
    """Determinism is the precondition for everything else here: if the same
    Selection produced different SQL, pinning it would be meaningless."""
    print("\n[determinism] the same Selection always builds byte-identical SQL")
    selection = semantic.Selection(
        metrics=["net_revenue", "order_count"],
        dimensions=["channel"],
        date_range="last_month",
        filters=[semantic.Filter(dimension="region", operator="in", values=["EMEA", "APAC"])],
    )
    builds = [sql_builder.build(selection, MODEL) for _ in range(5)]
    check("all 5 builds produce identical SQL", len({b.sql for b in builds}) == 1)
    check("all 5 builds produce identical params", len({tuple(b.params) for b in builds}) == 1)


def test_pinned_report_sql_is_stable_across_runs():
    print("\n[frozen] a pinned report produces the same SQL on every run")
    _harness.reset_app_db()
    reports.pin("weekly-rev", "net revenue by week this year")

    runs = [reports.run("weekly-rev") for _ in range(3)]
    sqls = {r.sql for r in runs}
    check("SQL identical across 3 runs", len(sqls) == 1, f"{len(sqls)} distinct SQL strings")
    check("all runs succeeded", all(r.ok for r in runs))


def test_pinned_report_ignores_reinterpretation():
    """THE CORE GUARANTEE. Even if the interpretation layer would now produce a
    DIFFERENT selection for the same question text, the pinned report must keep
    computing what it was pinned to compute."""
    print("\n[frozen] re-interpretation cannot change a pinned report")
    _harness.reset_app_db()

    question = "net revenue by channel last month"
    reports.pin("chan-rev", question)
    original = reports.run("chan-rev")
    original_sql = original.sql

    # Simulate the interpretation layer drifting — a new synonym, a model
    # change, a different provider. Here: make the same question resolve to a
    # different metric entirely.
    #
    # NOTE ON PATCHING: answer.py does `from .selector import select`, so it
    # holds its OWN reference in its module namespace. Patching
    # `selector.select` alone leaves answer_mod.select untouched — which made an
    # earlier version of the control assertion below inert, i.e. it looked like
    # proof while testing nothing. Both names must be patched.
    real_selector_select = selector.select
    real_answer_select = answer_mod.select

    def drifted_select(q, model):
        return semantic.Selection(
            metrics=["gross_revenue"],          # different metric!
            dimensions=["payment_method"],       # different grouping!
            date_range="this_year",              # different period!
        )

    selector.select = drifted_select
    answer_mod.select = drifted_select
    # reports.run() must NOT consult the selector at all.
    try:
        after = reports.run("chan-rev")
        # CONTROL: an ad-hoc ask must genuinely change, proving the drift is
        # real and that the pinned report's stability isn't a false negative.
        adhoc = answer_mod.ask(question)
    finally:
        selector.select = real_selector_select
        answer_mod.select = real_answer_select

    check("SQL unchanged despite drifted interpretation", after.sql == original_sql,
          "the pinned report re-interpreted the question")
    check("still reports net_revenue", "net_revenue" in after.columns, str(after.columns))
    check("still grouped by channel", "channel" in after.columns, str(after.columns))
    check("gross_revenue did NOT leak in", "gross_revenue" not in after.columns, str(after.columns))

    check("CONTROL: ad-hoc ask DID change (drift was genuine)",
          "gross_revenue" in adhoc.columns, str(adhoc.columns))
    check("CONTROL: ad-hoc ask picked up the drifted grouping",
          "payment_method" in adhoc.columns, str(adhoc.columns))


def test_repin_is_explicit_and_versioned():
    print("\n[frozen] changing a report requires an explicit repin, and is versioned")
    _harness.reset_app_db()
    reports.pin("rev", "net revenue last month")
    before = reports.run("rev")

    info = reports.repin("rev", "order count last month", note="switched to order volume")
    check("version bumped to 2", info["version"] == 2, str(info["version"]))

    after = reports.run("rev")
    check("SQL changed after explicit repin", after.sql != before.sql)
    check("now reports order_count", "order_count" in after.columns, str(after.columns))

    hist = reports.history("rev")
    check("history records both versions", len(hist["versions"]) == 2, str(len(hist["versions"])))
    check("the note is preserved", any("order volume" in (v["note"] or "") for v in hist["versions"]),
          str(hist["versions"]))
    check("runs recorded against their version", len(hist["recent_runs"]) >= 2, str(len(hist["recent_runs"])))


def test_version_history_is_append_only():
    """The record of what a report USED to compute is exactly what you need when
    a number changes and nobody remembers why — so it must not be rewritable."""
    print("\n[audit] report version history cannot be rewritten")
    _harness.reset_app_db()
    reports.pin("audit-rev", "net revenue last month")

    for label, sql in [
        ("UPDATE", "UPDATE report_versions SET note = 'tampered'"),
        ("DELETE", "DELETE FROM report_versions"),
    ]:
        raised = False
        message = ""
        try:
            with reports.session() as conn:
                conn.execute(sql)
        except Exception as e:  # noqa: BLE001
            raised = True
            message = str(e)
        check(f"{label} rejected", raised, "history was mutable")
        check(f"{label} error explains why", "append-only" in message.lower(), message)


def test_cannot_pin_an_unanswerable_question():
    """A scheduled report that silently returns nothing is worse than one that
    was never created."""
    print("\n[frozen] pinning refuses a question that can't be answered")
    _harness.reset_app_db()
    raised = False
    message = ""
    try:
        reports.pin("bad", "what is our churn rate?")
    except reports.ReportError as e:
        raised = True
        message = str(e)
    check("pin refused", raised, "an unanswerable question was pinned")
    check("error explains the refusal", "cannot pin" in message.lower(), message)


def test_duplicate_pin_rejected():
    print("\n[frozen] pinning over an existing report requires repin, not pin")
    _harness.reset_app_db()
    reports.pin("dupe", "net revenue last month")
    raised = False
    try:
        reports.pin("dupe", "order count last month")
    except reports.ReportError:
        raised = True
    check("duplicate pin rejected", raised)


def test_run_records_primary_value_for_drift_detection():
    """Storing the headline figure per run is what lets someone SEE that a
    number moved, and correlate it with a version bump."""
    print("\n[audit] each run records its headline value")
    _harness.reset_app_db()
    reports.pin("total-rev", "net revenue last month")
    reports.run("total-rev")
    reports.run("total-rev")

    hist = reports.history("total-rev")
    values = [r["primary_value"] for r in hist["recent_runs"]]
    check("values recorded", all(v is not None for v in values), str(values))
    check("repeated runs of a frozen report agree", len(set(values)) == 1, str(values))


def main():
    print("=" * 78)
    print("Frozen scheduled reports: no silent drift")
    print("=" * 78)

    test_same_selection_produces_identical_sql()
    test_pinned_report_sql_is_stable_across_runs()
    test_pinned_report_ignores_reinterpretation()
    test_repin_is_explicit_and_versioned()
    test_version_history_is_append_only()
    test_cannot_pin_an_unanswerable_question()
    test_duplicate_pin_rejected()
    test_run_records_primary_value_for_drift_detection()

    print("\n" + "=" * 78)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("A pinned report computes what it was pinned to compute, and changes are audited.")


if __name__ == "__main__":
    main()
