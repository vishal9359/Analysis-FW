"""Step 0: find the database that holds a run, and compare its columns with the catalog.

Each run is loaded into a database of its own, and Profile FW's protos still
change between runs (docs/phase1-design.md, sections 5.1 and 12). So before
anything else the runner asks ClickHouse where the run is and which described
columns it has. Everything here only reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .catalog import Catalog, Column

EXAMPLE_RUN = "ProfileData-fio-20261008-101500"


class RunError(Exception):
    """The run cannot be used; the message says why."""


class Database(Protocol):
    """What step 0 needs from ClickHouse. clickhouse.ClickHouse is the real one."""

    def find_tables(self, names: list[str]) -> list[tuple[str, str]]:
        """(database, table) of every table, not view, with one of these names."""

    def has_run(self, database: str, table: str, run: str) -> bool:
        """True if the table holds rows of this run."""

    def run_ids(self, database: str, table: str) -> list[str]:
        """The runs the table holds (a few at most), to say what is there when a run is not."""

    def columns(self, database: str) -> dict[str, dict[str, str]]:
        """{table: {column: ClickHouse type}} for every table, not view, of the database."""


def run_name(arg: str) -> str:
    """The run's name, from the name or the path of its folder. The loader names a
    run after its folder (run_id, src/runner.py), so a path gives the same name."""
    if not arg.strip():
        raise RunError(f"no run given: name the run folder, for example {EXAMPLE_RUN}")
    path = Path(arg)
    return path.resolve().name if path.is_dir() else path.name


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


def find_database(db: Database, run: str, catalog: Catalog, database: str | None = None) -> str:
    """The database whose described tables hold this run. The request names only
    the run, so this works whatever the database is called; `database` (--db)
    limits the search to that one database."""
    names = sorted(catalog.tables)
    by_db: dict[str, list[str]] = {}
    for d, table in db.find_tables(names):
        by_db.setdefault(d, []).append(table)
    if database is not None:
        if database not in by_db:
            raise RunError(f"database {database!r} has none of the described tables "
                           f"({', '.join(names)}). Is the name right? Databases that have "
                           f"them: {', '.join(sorted(by_db)) or 'none'}.")
        by_db = {database: by_db[database]}
    found = [d for d, tables in sorted(by_db.items())
             if any(db.has_run(d, t, run) for t in sorted(tables))]
    if not found and run in by_db:          # a database's name, given as the run's
        held = db.run_ids(run, sorted(by_db[run])[0])
        raise RunError(f"{run!r} is the name of a database, not of a run. It holds "
                       f"{', '.join(held[:5]) or 'no run'}. Give the run's name - the run "
                       f"folder's name - and, if you like, --db {run}.")
    if not found:
        where = (f"database {database!r}" if database is not None else
                 f"any database with a described table ({', '.join(names)})")
        raise RunError(f"run {run!r} was not found in {where}. {_runs_loaded(db, by_db)} "
                       f"The run name is the run folder's name, for example {EXAMPLE_RUN}.")
    if len(found) > 1:
        raise RunError(f"run {run!r} is in more than one database: {', '.join(found)}. "
                       f"Was it loaded twice? Choose one with --db, or drop the extra database.")
    return found[0]


def _runs_loaded(db: Database, by_db: dict[str, list[str]], limit: int = 10) -> str:
    """'Runs loaded: a (in db1), b (in db2).' - what is there, for an error message."""
    seen: list[str] = []
    for d, tables in sorted(by_db.items()):
        seen += [f"{r} (in {d})" for r in db.run_ids(d, sorted(tables)[0])]
        if len(seen) > limit:
            break
    if not seen:
        return "No runs are loaded there."
    more = ", and more" if len(seen) > limit else ""
    return "Runs loaded: " + ", ".join(seen[:limit]) + more + "."


def check_run(db: Database, run: str, catalog: Catalog, database: str | None = None) -> RunCheck:
    """Step 0: where the run is, and which described columns it has."""
    database = find_database(db, run, catalog, database)
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
