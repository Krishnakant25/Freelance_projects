"""
Chart selection from data shape.

Rule-based, deliberately — architecture doc §6.5: "keep chart logic simple/
rule-based rather than another LLM call." The shape of the result set fully
determines the sensible visualisation, so a model adds latency, cost, and
non-determinism for no gain. A time series is a line; a category breakdown is a
bar; a single number is a stat tile. There is no judgement to outsource.
"""
from typing import Any

from .semantic import SemanticModel, Selection


def choose(columns: list[str], rows: list[dict], model: SemanticModel, selection: Selection) -> dict:
    metric_cols = [c for c in columns if c in model.metrics]
    dim_cols = [c for c in columns if c in model.dimensions]
    time_dims = [c for c in dim_cols if model.dimensions[c].time_dimension]
    cat_dims = [c for c in dim_cols if not model.dimensions[c].time_dimension]

    if not rows:
        return {"type": "empty", "reason": "no rows returned"}

    # Single metric, no grouping -> one number.
    if not dim_cols and len(metric_cols) >= 1 and len(rows) == 1:
        primary = metric_cols[0]
        return {
            "type": "stat",
            "value_column": primary,
            "value": rows[0].get(primary),
            "format": model.metrics[primary].format,
            "label": model.metrics[primary].label,
        }

    # Time dimension present -> line (or multi-line for several metrics).
    if time_dims:
        return {
            "type": "line",
            "x": time_dims[0],
            "y": metric_cols,
            "reason": "time dimension present — chronological trend",
            "formats": {m: model.metrics[m].format for m in metric_cols},
        }

    # Categorical breakdown -> bar. Horizontal when there are many categories,
    # since long labels are unreadable on a vertical axis.
    if cat_dims:
        orientation = "horizontal" if len(rows) > 8 else "vertical"
        return {
            "type": "bar",
            "orientation": orientation,
            "x": cat_dims[0],
            "y": metric_cols,
            "reason": f"categorical breakdown by {cat_dims[0]}",
            "formats": {m: model.metrics[m].format for m in metric_cols},
        }

    return {"type": "table", "reason": "shape does not map to a standard chart"}


def format_value(value: Any, fmt: str) -> str:
    if value is None:
        return "—"
    if fmt == "currency":
        return f"${value:,.2f}"
    if fmt == "integer":
        return f"{int(value):,}"
    if fmt == "percent":
        return f"{value:.1%}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)
