"""Read the Measurement Catalog: what every column means (knowledge/catalog/*.yaml).

Engineers write the catalog; `python tools/catalog.py check` keeps it in step
with the protos. This module only reads it: one Column per described column (a
group's entry applies to each of its columns), the shared columns from
_common.yaml, and each table's metrics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "knowledge" / "catalog"

KINDS = {"counter", "gauge", "interval", "dimension", "label", "timestamp", "setting"}
STATUSES = {"todo", "draft", "confirmed"}
USABLE_STATUSES = {"draft", "confirmed"}       # a todo entry is never used


@dataclass(frozen=True)
class Column:
    name: str
    type: str          # ClickHouse type, exactly as the loader creates it
    meaning: str
    unit: str
    kind: str
    status: str

    @property
    def usable(self) -> bool:
        """Described well enough to use: a known kind, and not todo."""
        return self.kind in KINDS and self.status in USABLE_STATUSES


@dataclass(frozen=True)
class Metric:
    name: str
    meaning: str
    formula: str       # in <column>_delta and interval_s (knowledge/README.md)
    unit: str
    status: str


@dataclass
class Table:
    name: str
    counts_since: str              # "boot" or "profiler start"
    observes: str
    columns: dict[str, Column]     # this table's own columns, in catalog order
    metrics: list[Metric] = field(default_factory=list)


@dataclass
class Catalog:
    shared: dict[str, Column]      # columns every table has (_common.yaml)
    tables: dict[str, Table]


def described(table_doc: dict):
    """(column name, entry) for every column, listed on its own or in a group."""
    for e in table_doc.get("columns") or []:
        yield e.get("column"), e
    for g in table_doc.get("groups") or []:
        for name in g.get("columns") or []:
            yield name, g


def _column(name: str, entry: dict) -> Column:
    return Column(name=name, type=str(entry.get("type", "")),
                  meaning=" ".join(str(entry.get("meaning", "")).split()),
                  unit=str(entry.get("unit", "")), kind=str(entry.get("kind", "")),
                  status=str(entry.get("status", "")))


def load(directory: Path = CATALOG_DIR) -> Catalog:
    shared: dict[str, Column] = {}
    tables: dict[str, Table] = {}
    for path in sorted(directory.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if path.name.startswith("_"):                  # shared columns
            for e in doc.get("columns") or []:
                shared[e["column"]] = _column(e["column"], e)
            continue
        metrics = [Metric(name=str(m["metric"]), meaning=" ".join(str(m.get("meaning", "")).split()),
                          formula=str(m.get("formula", "")), unit=str(m.get("unit", "")),
                          status=str(m.get("status", "")))
                   for m in doc.get("metrics") or []]
        for tdoc in doc.get("tables") or []:
            name = tdoc["table"]
            tables[name] = Table(
                name=name,
                counts_since=str(doc.get("counts_since", "")),
                observes=" ".join(str(doc.get("observes", "")).split()),
                columns={n: _column(n, e) for n, e in described(tdoc)},
                # a file's metrics belong to its main table (child tables come later)
                metrics=metrics if name == doc.get("table") else [])
    return Catalog(shared=shared, tables=tables)
