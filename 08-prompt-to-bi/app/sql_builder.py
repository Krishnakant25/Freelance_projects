"""
Deterministic SQL generation from a validated Selection.

No model output reaches this file. Everything emitted is assembled from
identifiers that were validated against the semantic model, and every literal
value is bound as a PARAMETER rather than interpolated.

That combination is what removes the two failure modes of free-form NL→SQL at
once: the model can't invent a join (only joins in the model are emitted), and
it can't inject anything (values are parameters, never string-concatenated).
"""
import logging
from dataclasses import dataclass, field

from . import config
from .semantic import SemanticModel, Selection

logger = logging.getLogger(__name__)


@dataclass
class BuiltQuery:
    sql: str
    params: list
    # Every answer displays these. Architecture doc §6.4: never show a number
    # without its derivation, because that's what makes the remaining accuracy
    # risk checkable by a human instead of invisible.
    assumptions: list[str] = field(default_factory=list)
    metric_definitions: dict[str, str] = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    date_range_label: str = ""


def _resolve_joins(selection: Selection, model: SemanticModel) -> list[str]:
    """Collects the joins needed, following prerequisites, in model order.

    Only joins declared in the model can appear. A metric or dimension that
    needs `products` pulls in `order_items` first because the model says
    products are only reachable through it — which is how "the model invented a
    plausible but wrong join" stops being possible.
    """
    needed: set[str] = set()

    def add(join_name: str):
        if join_name in needed:
            return
        spec = model.joins.get(join_name)
        if spec is None:
            raise ValueError(f"join {join_name!r} is not defined in the semantic model")
        for prereq in spec.get("requires", []) or []:
            add(prereq)
        needed.add(join_name)

    for m in selection.metrics:
        for j in model.metrics[m].requires_joins:
            add(j)
    for d in selection.dimensions:
        for j in model.dimensions[d].requires_joins:
            add(j)
    for f in selection.filters:
        for j in model.dimensions[f.dimension].requires_joins:
            add(j)

    # Emit in the model's declared order so generated SQL is byte-stable for the
    # same Selection — required for the frozen-report guarantee and for tests
    # that compare SQL directly.
    return [model.joins[name]["sql"] for name in model.joins if name in needed]


def build(selection: Selection, model: SemanticModel) -> BuiltQuery:
    assumptions = list(selection.assumptions)

    # --- SELECT ---
    select_parts: list[str] = []
    group_by_parts: list[str] = []
    columns: list[str] = []
    metric_definitions: dict[str, str] = {}

    for dim_name in selection.dimensions:
        dim = model.dimensions[dim_name]
        if dim.time_dimension:
            grain = selection.time_grain or config.DEFAULT_TIME_GRAIN
            if not selection.time_grain:
                assumptions.append(f"grouped by {grain} (default grain)")
            expr = model.time_grains[grain]
            select_parts.append(f"{expr} AS {dim_name}")
            group_by_parts.append(expr)
        else:
            select_parts.append(f"{dim.sql} AS {dim_name}")
            group_by_parts.append(dim.sql)
        columns.append(dim_name)

    for metric_name in selection.metrics:
        metric = model.metrics[metric_name]
        select_parts.append(f"{metric.sql} AS {metric_name}")
        columns.append(metric_name)
        metric_definitions[metric_name] = metric.sql

    # --- FROM / JOIN ---
    from_clause = [f"FROM {model.base_table}"] + _resolve_joins(selection, model)

    # --- WHERE ---
    where_parts: list[str] = []
    params: list = []

    date_range_name = selection.date_range or config.DEFAULT_DATE_RANGE
    if not selection.date_range:
        assumptions.append(
            f"date range defaulted to {model.date_ranges[date_range_name].label}"
        )
    dr = model.date_ranges[date_range_name]
    # Date bounds are SQL expressions from the model (not user input), so they're
    # inlined; everything derived from the question is parameterised below.
    where_parts.append(f"o.ordered_at >= {dr.start} AND o.ordered_at < {dr.end}")

    for f in selection.filters:
        dim = model.dimensions[f.dimension]
        if dim.time_dimension:
            raise ValueError("filtering on the time dimension is done via date_range, not filters")
        if f.operator in ("in", "not_in") or len(f.values) > 1:
            placeholders = ", ".join("?" for _ in f.values)
            negate = "NOT " if f.operator in ("!=", "not_in") else ""
            where_parts.append(f"{dim.sql} {negate}IN ({placeholders})")
            params.extend(f.values)
        else:
            op = "=" if f.operator == "=" else "!="
            where_parts.append(f"{dim.sql} {op} ?")
            params.append(f.values[0])

    # --- ORDER BY ---
    order_clause = ""
    if selection.order_by_metric:
        direction = "DESC" if selection.descending else "ASC"
        order_clause = f"ORDER BY {selection.order_by_metric} {direction}"
    elif selection.dimensions:
        time_dims = [d for d in selection.dimensions if model.dimensions[d].time_dimension]
        if time_dims:
            # Time series read chronologically, not by magnitude.
            order_clause = f"ORDER BY {time_dims[0]} ASC"
        elif selection.metrics:
            order_clause = f"ORDER BY {selection.metrics[0]} DESC"

    # --- LIMIT ---
    # Always present. Architecture doc §6.6: an unbounded result set is a
    # latency and memory problem that only shows up once the data grows.
    limit = min(selection.limit or config.MAX_RESULT_ROWS, config.MAX_RESULT_ROWS)
    if selection.limit and selection.limit > config.MAX_RESULT_ROWS:
        assumptions.append(f"result capped at {config.MAX_RESULT_ROWS} rows")

    sql_lines = ["SELECT " + ", ".join(select_parts)]
    sql_lines.extend(from_clause)
    sql_lines.append("WHERE " + " AND ".join(where_parts))
    if group_by_parts:
        sql_lines.append("GROUP BY " + ", ".join(group_by_parts))
    if order_clause:
        sql_lines.append(order_clause)
    sql_lines.append(f"LIMIT {int(limit)}")

    return BuiltQuery(
        sql="\n".join(sql_lines),
        params=params,
        assumptions=assumptions,
        metric_definitions=metric_definitions,
        columns=columns,
        date_range_label=dr.label,
    )
