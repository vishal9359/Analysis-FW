"""The WrenAI data models for one run (docs/phase1-design.md, section 6).

One data model per model spec of the run (views.model_specs): each sampled table
over that run's per-interval view, each table's latency histograms over its histogram
view, and each run-level table as it is - with the catalog's meanings and units, and
the catalog's metrics as calculated columns. Generated for each run, never edited by
hand. The data model names are the table names (and <table>_histograms), the same in
every run, so our LLM's prompts and examples never change.

WrenAI contains no AI. Its only job is dry_plan: turn a query written on these
data models into ClickHouse SQL, without running it. Our checks still read the
result (checks.planned_sql_problems): WrenAI's strict mode alone is not enough.
"""
from __future__ import annotations

import base64
import json

from .runs import RunCheck
from .views import ModelSpec

# WrenAI's names for the ClickHouse types our views have - what
# wren.type_mapping.parse_type(t, "clickhouse") gives in wrenai 0.15.0
# (tests/test_analysis.py checks they still agree).
WREN_TYPES = {
    "String": "TEXT", "DateTime": "DATETIME", "Bool": "BOOLEAN",
    "Int32": "INT", "Int64": "BIGINT", "UInt8": "UTINYINT", "UInt16": "USMALLINT",
    "UInt32": "UINT", "UInt64": "UBIGINT",
    "Float32": "FLOAT", "Float64": "DOUBLE", "Nullable(Int64)": "BIGINT",
}


class PlanError(Exception):
    """WrenAI refused the query; the message says why."""


def data_models(run: RunCheck, specs: list[ModelSpec]) -> dict:
    """The run's data models as WrenAI's JSON document."""
    models = []
    for s in specs:
        cols = [{"name": c.name, "type": WREN_TYPES.get(c.ch_type, "TEXT"),
                 "properties": {"description": f"{c.meaning} Unit: {c.unit}."}}
                for c in s.columns]
        cols += [{"name": m.name, "type": "DOUBLE", "isCalculated": True,
                  "expression": m.formula,
                  "properties": {"description": f"{m.meaning} Unit: {m.unit}."}}
                 for m in s.metrics]
        models.append({
            "name": s.name,
            "tableReference": {"catalog": "", "schema": run.database, "table": s.source},
            "properties": {"description": s.about},
            "columns": cols,
        })
    return {"catalog": "wren", "schema": "public", "dataSource": "clickhouse",
            "models": models, "relationships": [], "views": []}


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
