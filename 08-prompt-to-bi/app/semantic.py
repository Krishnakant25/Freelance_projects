"""
Semantic model loader and the Selection type.

A `Selection` is the ONLY thing that can become SQL. It names metrics,
dimensions, a date range, and filters — all by identifier, all validated
against the model. The LLM's job is to produce one of these; it never produces
SQL. That's the whole premise change from the architecture doc §6.1.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import yaml

from . import config

logger = logging.getLogger(__name__)


@dataclass
class Metric:
    name: str
    label: str
    sql: str
    description: str = ""
    format: str = "number"
    requires_joins: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)


@dataclass
class Dimension:
    name: str
    label: str
    sql: str = ""
    description: str = ""
    time_dimension: bool = False
    requires_joins: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)


@dataclass
class DateRange:
    name: str
    label: str
    start: str
    end: str


@dataclass
class SemanticModel:
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    date_ranges: dict[str, DateRange]
    time_grains: dict[str, str]
    joins: dict[str, dict]
    base_table: str
    glossary: dict[str, str]

    def metric_names(self) -> list[str]:
        return sorted(self.metrics.keys())

    def dimension_names(self) -> list[str]:
        return sorted(self.dimensions.keys())

    def describe_for_prompt(self) -> str:
        """The vocabulary handed to the selector. Deliberately compact — this is
        the entire space of things that can be asked for, which is what makes
        selection tractable where free-form SQL generation isn't."""
        lines = ["METRICS:"]
        for m in self.metrics.values():
            lines.append(f"  {m.name}: {m.description or m.label}")
        lines.append("DIMENSIONS (group by):")
        for d in self.dimensions.values():
            lines.append(f"  {d.name}: {d.description or d.label}")
        lines.append("DATE RANGES:")
        lines.append("  " + ", ".join(self.date_ranges.keys()))
        lines.append("TIME GRAINS:")
        lines.append("  " + ", ".join(self.time_grains.keys()))
        return "\n".join(lines)


def load_model(path=None) -> SemanticModel:
    path = path or config.SEMANTIC_MODEL_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    metrics = {}
    for name, spec in (raw.get("metrics") or {}).items():
        metrics[name] = Metric(
            name=name,
            label=spec.get("label", name),
            sql=spec["sql"],
            description=spec.get("description", ""),
            format=spec.get("format", "number"),
            requires_joins=spec.get("requires_joins", []) or [],
            synonyms=[s.lower() for s in (spec.get("synonyms") or [])],
        )

    dimensions = {}
    for name, spec in (raw.get("dimensions") or {}).items():
        dimensions[name] = Dimension(
            name=name,
            label=spec.get("label", name),
            sql=spec.get("sql", ""),
            description=spec.get("description", ""),
            time_dimension=bool(spec.get("time_dimension", False)),
            requires_joins=spec.get("requires_joins", []) or [],
            synonyms=[s.lower() for s in (spec.get("synonyms") or [])],
        )

    date_ranges = {
        name: DateRange(name=name, label=spec.get("label", name), start=spec["start"], end=spec["end"])
        for name, spec in (raw.get("date_ranges") or {}).items()
    }

    model = SemanticModel(
        metrics=metrics,
        dimensions=dimensions,
        date_ranges=date_ranges,
        time_grains=raw.get("time_grains") or {},
        joins=raw.get("joins") or {},
        base_table=raw.get("base_table", ""),
        glossary=raw.get("glossary") or {},
    )
    _validate(model)
    return model


class SemanticModelError(ValueError):
    pass


def _validate(model: SemanticModel) -> None:
    """Fails loudly on a malformed model.

    A silently-broken semantic model would produce silently-wrong SQL, which is
    exactly the failure mode this whole design exists to eliminate — so the
    model is validated at load, not discovered at query time.
    """
    if not model.base_table:
        raise SemanticModelError("base_table is required")
    if not model.metrics:
        raise SemanticModelError("at least one metric is required")

    known_joins = set(model.joins.keys())
    for m in model.metrics.values():
        for j in m.requires_joins:
            if j not in known_joins:
                raise SemanticModelError(f"metric {m.name!r} requires unknown join {j!r}")
    for d in model.dimensions.values():
        for j in d.requires_joins:
            if j not in known_joins:
                raise SemanticModelError(f"dimension {d.name!r} requires unknown join {j!r}")
        if not d.time_dimension and not d.sql:
            raise SemanticModelError(f"dimension {d.name!r} needs sql or time_dimension: true")

    # Join prerequisites must themselves be known.
    for jname, jspec in model.joins.items():
        for req in jspec.get("requires", []) or []:
            if req not in known_joins:
                raise SemanticModelError(f"join {jname!r} requires unknown join {req!r}")

    # Synonym collisions are a real hazard: if "revenue" maps to two metrics the
    # selector's behaviour becomes order-dependent and therefore unpredictable.
    seen: dict[str, str] = {}
    for m in model.metrics.values():
        for syn in [m.name.lower()] + m.synonyms:
            if syn in seen and seen[syn] != m.name:
                raise SemanticModelError(
                    f"synonym {syn!r} is claimed by both {seen[syn]!r} and {m.name!r} — "
                    "ambiguous mapping would make selection order-dependent"
                )
            seen[syn] = m.name


@dataclass
class Filter:
    dimension: str
    operator: str   # = | != | in | not_in
    values: list

    def describe(self) -> str:
        if self.operator in ("in", "not_in") and len(self.values) > 1:
            joined = ", ".join(str(v) for v in self.values)
            verb = "is one of" if self.operator == "in" else "is not one of"
            return f"{self.dimension} {verb} [{joined}]"
        verb = {"=": "is", "!=": "is not", "in": "is", "not_in": "is not"}[self.operator]
        return f"{self.dimension} {verb} {self.values[0]}"


@dataclass
class Selection:
    """A validated request. The only input the SQL builder accepts."""
    metrics: list[str]
    dimensions: list[str] = field(default_factory=list)
    date_range: str = ""
    time_grain: str = ""
    filters: list[Filter] = field(default_factory=list)
    order_by_metric: Optional[str] = None
    descending: bool = True
    limit: Optional[int] = None
    # Human-readable notes about anything the system decided FOR the user.
    # Surfaced with every answer so a default can never masquerade as a request.
    assumptions: list[str] = field(default_factory=list)


class SelectionError(ValueError):
    """Raised when a Selection references something the model doesn't define.

    This is the mechanism that makes refusal possible: an unknown metric is a
    hard error, not a best-effort guess at a join."""


def validate_selection(selection: Selection, model: SemanticModel) -> Selection:
    for m in selection.metrics:
        if m not in model.metrics:
            raise SelectionError(f"unknown metric {m!r}")
    if not selection.metrics:
        raise SelectionError("at least one metric is required")
    for d in selection.dimensions:
        if d not in model.dimensions:
            raise SelectionError(f"unknown dimension {d!r}")
    if selection.date_range and selection.date_range not in model.date_ranges:
        raise SelectionError(f"unknown date range {selection.date_range!r}")
    if selection.time_grain and selection.time_grain not in model.time_grains:
        raise SelectionError(f"unknown time grain {selection.time_grain!r}")
    for f in selection.filters:
        if f.dimension not in model.dimensions:
            raise SelectionError(f"unknown filter dimension {f.dimension!r}")
        if f.operator not in ("=", "!=", "in", "not_in"):
            raise SelectionError(f"unsupported filter operator {f.operator!r}")
        if not f.values:
            raise SelectionError(f"filter on {f.dimension!r} has no values")
    if selection.order_by_metric and selection.order_by_metric not in selection.metrics:
        raise SelectionError(
            f"order_by_metric {selection.order_by_metric!r} is not among the selected metrics"
        )
    return selection
