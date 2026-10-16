"""Step 0: find the database that holds a run, and compare its columns with the catalog.

Each run is loaded into a database of its own, and Profile FW's protos still
change between runs (docs/phase1-design.md, sections 5.1 and 12). So before
anything else the runner asks ClickHouse where the run is and which described
columns it has. Everything here only reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .catalog import Catalog, Column


class RunError(Exception):
    """The run cannot be used; the message says why."""


class Database(Protocol):
    """What step 0 needs from ClickHouse. clickhouse.ClickHouse is the real one."""

    def find_tables(self, names: list[str]) -> list[tuple[str, str]]:
        """(database, table) of every table, not view, with one of these names."""

    def has_run(self, database: str, table: str, run: str) -> bool:
        """True if the table holds rows of this run."""

    def columns(self, database: str) -> dict[str, dict[str, str]]:
        """{table: {column: ClickHouse type}} for every table, not view, of the database."""


@dataclass
class TableCheck:
    table: str
    usable: list[Column]       # in the run and described (not todo), in catalog order
    not_collected: list[str]   # described, but this run does not have it
    not_described: list[str]   # this run has it, the catalog does not describe it yet
    wrong_type: list[str]      # "column: catalog X, run Y" - the catalog is for another proto


@dataclass
class RunCheck:
    run: str
    database: str
    tables: dict[str, TableCheck]   # the described tables this run has
    missing_tables: list[str]       # described tables this run does not have
    undescribed_tables: list[str]   # tables of this run the catalog does not describe

    @property
    def problems(self) -> list[str]:
        """Reasons to stop: a column whose type differs from the catalog's."""
        return [f"{t.table}.{w}" for t in self.tables.values() for w in t.wrong_type]


def find_database(db: Database, run: str, catalog: Catalog) -> str:
    """The database whose described tables hold this run. The request names only
    the run, so this works whatever the database is called."""
    names = sorted(catalog.tables)
    by_db: dict[str, list[str]] = {}
    for database, table in db.find_tables(names):
        by_db.setdefault(database, []).append(table)
    found = [d for d, tables in sorted(by_db.items())
             if any(db.has_run(d, t, run) for t in sorted(tables))]
    if not found:
        raise RunError(f"run {run!r} was not found in any database with a described table "
                       f"({', '.join(names)}). Is it loaded? The run name is the run folder's "
                       f"name, for example ProfileData-fio-20261008-101500.")
    if len(found) > 1:
        raise RunError(f"run {run!r} is in more than one database: {', '.join(found)}. "
                       f"Was it loaded twice? Drop the extra database, then try again.")
    return found[0]


def check_run(db: Database, run: str, catalog: Catalog) -> RunCheck:
    """Step 0: where the run is, and which described columns it has."""
    database = find_database(db, run, catalog)
    actual = db.columns(database)
    tables: dict[str, TableCheck] = {}
    for name, entry in sorted(catalog.tables.items()):
        if name not in actual:
            continue
        have = actual[name]
        usable, not_collected, wrong = [], [], []
        for col in entry.columns.values():
            if col.name not in have:
                not_collected.append(col.name)
            elif col.type and have[col.name] != col.type:
                wrong.append(f"{col.name}: catalog {col.type}, run {have[col.name]}")
            elif col.usable:
                usable.append(col)
        known = set(entry.columns) | set(catalog.shared)
        tables[name] = TableCheck(
            table=name, usable=usable, not_collected=not_collected,
            not_described=[c for c in have if c not in known], wrong_type=wrong)
    return RunCheck(
        run=run, database=database, tables=tables,
        missing_tables=sorted(set(catalog.tables) - set(tables)),
        undescribed_tables=sorted(set(actual) - set(catalog.tables)))
