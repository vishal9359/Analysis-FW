"""The WrenAI data models for one run (docs/phase1-design.md, section 6).

One data model per table the run has, over that run's per-interval view, with
the catalog's meanings and units, and the catalog's metrics as calculated
columns. Generated for each run, never edited by hand. The data model names are
the table names, the same in every run, so our LLM's prompts and examples never
change.

WrenAI contains no AI. Its only job is dry_plan: turn a query written on these
data models into ClickHouse SQL, without running it. Our checks still read the
result (checks.planned_sql_problems): WrenAI's strict mode alone is not enough.
"""
from __future__ import annotations

import base64
import json
import re

from .catalog import Catalog
from .runs import RunCheck
from .views import view_columns, view_name

# WrenAI's names for the ClickHouse types our views have - what
# wren.type_mapping.parse_type(t, "clickhouse") gives in wrenai 0.15.0
# (tests/test_analysis.py checks they still agree).
WREN_TYPES = {
    "String": "TEXT", "DateTime": "DATETIME", "Bool": "BOOLEAN",
    "Int32": "INT", "Int64": "BIGINT", "UInt32": "UINT", "UInt64": "UBIGINT",
    "Float32": "FLOAT", "Float64": "DOUBLE", "Nullable(Int64)": "BIGINT",
}
_NAME = re.compile(r"\b[a-z][a-z0-9_]*\b")


class PlanError(Exception):
    """WrenAI refused the query; the message says why."""


def data_models(run: RunCheck, catalog: Catalog) -> tuple[dict, dict[str, list[str]]]:
    """The run's data models as WrenAI's JSON document, and per table the metrics
    left out because this run lacks a column they need."""
    models, left_out = [], {}
    for table, check in sorted(run.tables.items()):
        entry = catalog.tables[table]
        columns = view_columns(check, catalog)
        have = {c.name for c in columns}
        cols = [{"name": c.name, "type": WREN_TYPES.get(c.ch_type, "TEXT"),
                 "properties": {"description": f"{c.meaning} Unit: {c.unit}."}}
                for c in columns]
        for m in entry.metrics:
            needs = {n for n in _NAME.findall(m.formula) if n.endswith("_delta") or n == "interval_s"}
            if needs <= have:
                cols.append({"name": m.name, "type": "DOUBLE", "isCalculated": True,
                             "expression": m.formula,
                             "properties": {"description": f"{m.meaning} Unit: {m.unit}."}})
            else:
                left_out.setdefault(table, []).append(m.name)
        models.append({
            "name": table,
            "tableReference": {"catalog": "", "schema": run.database, "table": view_name(table)},
            "properties": {"description": (
                f"{entry.observes} One row per sample of run {run.run}. Running totals appear "
                f"only as their change since the previous sample (<column>_delta); "
                f"interval_s is the seconds in between.")},
            "columns": cols,
        })
    return ({"catalog": "wren", "schema": "public", "dataSource": "clickhouse",
             "models": models, "relationships": [], "views": []}, left_out)


def plan(manifest: dict, sql: str) -> str:
    """WrenAI's dry_plan, in strict mode: the ClickHouse SQL for a query written on
    the data models. Nothing is run and no network is used."""
    from wren.config import WrenConfig
    from wren.engine import WrenEngine
    from wren.model.data_source import DataSource

    encoded = base64.b64encode(json.dumps(manifest).encode("utf-8")).decode("ascii")
    engine = WrenEngine(encoded, DataSource.clickhouse, {}, config=WrenConfig(strict_mode=True))
    try:
        return engine.dry_plan(sql)
    except Exception as exc:  # WrenError and planner errors: refused, with the reason
        raise PlanError(" ".join(str(exc).split())) from exc
