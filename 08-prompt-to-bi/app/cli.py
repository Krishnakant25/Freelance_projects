"""
CLI — the demo surface.

Usage:
    python -m app.cli seed                          # build the sample warehouse
    python -m app.cli ask "revenue last month"
    python -m app.cli ask "revenue by channel" --sql # show the generated SQL
    python -m app.cli model                          # list the semantic vocabulary
    python -m app.cli doctor                         # prove the read-only boundary holds

    python -m app.cli report pin weekly-revenue "net revenue by week this year"
    python -m app.cli report run weekly-revenue
    python -m app.cli report list
    python -m app.cli report history weekly-revenue
"""
import argparse
import sys

from . import answer as answer_mod
from . import charts, config, guardrails, reports, semantic, warehouse

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _print_answer(a, show_sql: bool = False, show_plan: bool = False):
    if a.refused:
        print(f"\nCANNOT ANSWER  ({a.refusal_reason})")
        print(f"  {a.message}\n")
        if a.available_metrics:
            print("  Metrics I do have:")
            for m in a.available_metrics:
                print(f"    - {m}")
            print("\n  Dimensions I can break them down by:")
            for d in a.available_dimensions:
                print(f"    - {d}")
        print()
        return

    if a.needs_clarification:
        print(f"\nNEEDS CLARIFICATION\n  {a.message}")
        for o in a.clarification_options:
            print(f"    - {o}")
        print()
        return

    model = answer_mod.get_model()
    chart = a.chart or {}

    print()
    if chart.get("type") == "stat":
        print(f"  {chart['label']}: {charts.format_value(chart['value'], chart['format'])}")
    else:
        widths = {c: max(len(c), 12) for c in a.columns}
        for r in a.rows[:25]:
            for c in a.columns:
                widths[c] = max(widths[c], len(str(r.get(c, ""))))
        header = "  " + "  ".join(c.ljust(widths[c]) for c in a.columns)
        print(header)
        print("  " + "-" * (len(header) - 2))
        for r in a.rows[:25]:
            cells = []
            for c in a.columns:
                v = r.get(c)
                if c in model.metrics:
                    v = charts.format_value(v, model.metrics[c].format)
                cells.append(str(v).ljust(widths[c]))
            print("  " + "  ".join(cells))
        if a.row_count > 25:
            print(f"  ... {a.row_count - 25} more rows")

    # Architecture doc §6.4: never show a number without its derivation.
    print(f"\n  Period:      {a.date_range}")
    if a.filters_applied:
        print(f"  Filters:     {'; '.join(a.filters_applied)}")
    if a.assumptions:
        print(f"  Assumptions: {'; '.join(a.assumptions)}")
    print(f"  Chart:       {chart.get('type')} ({chart.get('reason', 'single value')})")
    print(f"  Rows:        {a.row_count}{' (truncated)' if a.truncated else ''}")
    print(f"  Data as of:  {a.data_freshness}")
    print(f"  Took:        {a.elapsed_ms:.0f} ms{' (cached)' if a.cached else ''}")

    if show_sql:
        print("\n  --- generated SQL (deterministic, from the semantic model) ---")
        for line in a.sql.split("\n"):
            print(f"    {line}")
        if a.params:
            print(f"    params: {a.params}")
        print("\n  --- metric definitions used ---")
        for name, defn in a.metric_definitions.items():
            print(f"    {name} = {defn}")
    print()


def cmd_seed(args):
    stats = warehouse.seed(days=args.days, order_count=args.orders)
    reports.init_db()
    print("Warehouse seeded (deterministic — same seed always produces the same numbers):")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"\nData freshness: {warehouse.freshness()}")


def cmd_ask(args):
    a = answer_mod.ask(args.question)
    _print_answer(a, show_sql=args.sql)
    if a.refused:
        sys.exit(2)


def cmd_model(args):
    model = answer_mod.get_model()
    print(f"\nSemantic model: {config.SEMANTIC_MODEL_PATH.name}")
    print("This is the entire vocabulary. Anything outside it gets refused, not guessed.\n")
    print("METRICS")
    for m in model.metrics.values():
        print(f"  {m.name:22s} {m.label}")
        if m.description:
            print(f"  {'':22s}   {m.description}")
    print("\nDIMENSIONS")
    for d in model.dimensions.values():
        print(f"  {d.name:22s} {d.label}")
        if d.description:
            print(f"  {'':22s}   {d.description}")
    print("\nDATE RANGES")
    print("  " + ", ".join(model.date_ranges.keys()))
    print("\nTIME GRAINS")
    print("  " + ", ".join(model.time_grains.keys()))
    print("\nGLOSSARY (ambiguous terms resolved once, here)")
    for term, meaning in model.glossary.items():
        print(f"  {term}: {meaning.strip()}")
    print()


def cmd_doctor(args):
    """Actively proves the safety boundaries rather than asserting them."""
    print("\nChecking guardrails...\n")
    print("READ-ONLY ENFORCEMENT (attempting writes against the query connection)")
    for label, outcome in guardrails.verify_read_only().items():
        ok = "refused" in outcome
        print(f"  {'OK  ' if ok else 'FAIL'} {label:8s} {outcome}")

    print("\nSTATEMENT RESTRICTIONS")
    for label, sql in [
        ("stacked statement", "SELECT 1; DROP TABLE orders"),
        ("non-select", "DELETE FROM orders"),
        ("pragma", "PRAGMA writable_schema=1"),
    ]:
        try:
            guardrails.execute(sql)
            print(f"  FAIL {label:18s} ALLOWED — boundary broken")
        except guardrails.GuardrailViolation as e:
            print(f"  OK   {label:18s} refused ({e})")
        except Exception as e:  # noqa: BLE001
            print(f"  OK   {label:18s} refused ({type(e).__name__})")

    print("\nWAREHOUSE")
    for table, count in warehouse.table_row_counts().items():
        print(f"  {table:14s} {count:,} rows")
    print(f"  freshness      {warehouse.freshness()}")
    print(f"\n  scan ceiling   {config.MAX_SCAN_ROWS:,} rows")
    print(f"  row cap        {config.MAX_RESULT_ROWS:,}")
    print()


def cmd_report(args):
    reports.init_db()
    if args.report_command == "pin":
        try:
            info = reports.pin(args.name, args.question)
            print(f"Pinned {info['name']!r} v{info['version']}.")
            print("The SELECTION is now frozen — re-runs execute this, they do not re-interpret the question.")
            print(f"  metrics:    {info['selection']['metrics']}")
            print(f"  dimensions: {info['selection']['dimensions']}")
            print(f"  date_range: {info['selection']['date_range'] or '(default)'}")
        except reports.ReportError as e:
            print(f"ERROR: {e}")
            sys.exit(1)

    elif args.report_command == "repin":
        try:
            info = reports.repin(args.name, args.question, note=args.note)
            print(f"Repinned {info['name']!r} -> v{info['version']} (change is versioned and audited).")
        except reports.ReportError as e:
            print(f"ERROR: {e}")
            sys.exit(1)

    elif args.report_command == "run":
        try:
            a = reports.run(args.name)
        except reports.ReportError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
        print(f"\nReport: {args.name}")
        _print_answer(a, show_sql=args.sql)

    elif args.report_command == "list":
        rows = reports.list_reports()
        if not rows:
            print("No pinned reports.")
            return
        print(f"\n{'NAME':26s} {'VER':>4s} {'RUNS':>5s}  QUESTION")
        print("-" * 78)
        for r in rows:
            print(f"{r['name']:26s} {r['version']:>4d} {r['runs']:>5d}  {r['question'][:38]}")
        print()

    elif args.report_command == "history":
        try:
            h = reports.history(args.name)
        except reports.ReportError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
        print(f"\nReport: {h['name']}  (current v{h['current_version']})")
        print(f"Question: {h['question']}\n")
        print("VERSIONS (append-only)")
        for v in h["versions"]:
            print(f"  v{v['version']}  {v['changed_at']}  {v['note']}")
        print("\nRECENT RUNS")
        for r in h["recent_runs"]:
            val = f"{r['primary_value']:,.2f}" if r["primary_value"] is not None else f"{r['row_count']} rows"
            print(f"  v{r['version']}  {r['ran_at']}  {val}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Prompt-to-BI — semantic-layer analytics")
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="Build the deterministic sample warehouse")
    p_seed.add_argument("--days", type=int, default=None)
    p_seed.add_argument("--orders", type=int, default=None)
    p_seed.set_defaults(func=cmd_seed)

    p_ask = sub.add_parser("ask", help="Ask a question")
    p_ask.add_argument("question")
    p_ask.add_argument("--sql", action="store_true", help="Show the generated SQL and metric definitions")
    p_ask.set_defaults(func=cmd_ask)

    sub.add_parser("model", help="Show the semantic vocabulary").set_defaults(func=cmd_model)
    sub.add_parser("doctor", help="Prove the read-only and cost guardrails hold").set_defaults(func=cmd_doctor)

    p_report = sub.add_parser("report", help="Manage frozen scheduled reports")
    rsub = p_report.add_subparsers(dest="report_command", required=True)

    r_pin = rsub.add_parser("pin")
    r_pin.add_argument("name")
    r_pin.add_argument("question")

    r_repin = rsub.add_parser("repin")
    r_repin.add_argument("name")
    r_repin.add_argument("question")
    r_repin.add_argument("--note", default="")

    r_run = rsub.add_parser("run")
    r_run.add_argument("name")
    r_run.add_argument("--sql", action="store_true")

    rsub.add_parser("list")
    r_hist = rsub.add_parser("history")
    r_hist.add_argument("name")

    p_report.set_defaults(func=cmd_report, sql=False)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
