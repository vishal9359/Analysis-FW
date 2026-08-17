"""Run a load: discover units, build schemas, load each unit, summarize.

Two execution modes, same per-unit code (`worker.load_unit`):
  - sequential (workers == 1): in-process, one shared store. Simple, testable.
  - parallel  (workers > 1): a process pool, one child per .pb-unit. Each child
    builds its own store and message class from picklable data (the descriptor
    bytes), because message classes and live connections are not picklable.

There is no coordinator: units are independent append-only jobs, so the pool is
a plain fan-out with no cross-unit state.
"""
from __future__ import annotations

import concurrent.futures as cf
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config
from .discover import Unit, discover
from .errors import AnalysisFWError, DatabaseError, ExitCode, InputError
from .registry import build_schema, build_schema_from_descriptor
from .store.base import Store
from .store.factory import make_store
from .worker import UnitResult, load_unit


@dataclass
class LoadReport:
    run_id: str
    producer: str
    database: str
    units: list[UnitResult] = field(default_factory=list)
    status: str = "complete"

    @property
    def records(self) -> int:
        return sum(u.records for u in self.units)

    @property
    def rows(self) -> int:
        return sum(u.total_rows for u in self.units)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "producer": self.producer,
            "database": self.database,
            "totals": {"records": self.records, "rows": self.rows,
                       "units": len(self.units)},
            "units": [
                {"stem": u.stem, "files": u.files, "records": u.records,
                 "tables": u.table_rows, "ok": u.ok}
                for u in self.units
            ],
        }


# --------------------------------------------------------------------------
# parallel worker entry point — receives only picklable data
# --------------------------------------------------------------------------

@dataclass
class _Job:
    stem: str
    pb_parts: list[str]
    descriptor_bytes: bytes
    source: str            # proto filename, so the child finds the primary file
    run_id: str
    kind: str
    host: str
    port: int
    database: str
    batch_size: int
    options: dict


def _run_job(job: _Job) -> UnitResult:
    """Executed in a child process. Rebuilds everything from picklable inputs —
    descriptor bytes and paths — because message classes and live connections
    cannot be pickled."""
    schema = build_schema_from_descriptor(
        job.stem, job.descriptor_bytes, source=job.source)
    store = make_store(job.kind, job.host, job.port, job.database, job.options)
    store.connect()
    try:
        return load_unit([Path(p) for p in job.pb_parts], schema, store,
                         job.run_id, job.batch_size)
    finally:
        store.close()


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def run_load(input_dir: Path, cfg: Config, store: Store | None = None) -> LoadReport:
    """Discover and load a run. If `store` is given, run sequentially against it
    (used by tests). Otherwise build the configured store and use the worker
    pool."""
    run_id = input_dir.resolve().name
    units = discover(input_dir)
    schemas = {u.stem: build_schema(u.stem, u.proto_path) for u in units}

    report = LoadReport(run_id=run_id, producer=cfg.producer_name,
                        database=cfg.store.database)

    # sequential path (injected store, or workers == 1)
    if store is not None or cfg.store.workers == 1:
        own = store is None
        st = store or make_store(cfg.store.kind, cfg.store.host, cfg.store.port,
                                 cfg.store.database, cfg.store.options)
        if own:
            st.connect()
        try:
            for u in units:
                report.units.append(
                    load_unit(u.pb_parts, schemas[u.stem], st, run_id,
                              cfg.store.batch_size))
        finally:
            if own:
                st.close()
        return _reconcile(report)

    # parallel path — one job per unit, across a process pool
    jobs = [
        _Job(stem=u.stem, pb_parts=[str(p) for p in u.pb_parts],
             descriptor_bytes=schemas[u.stem].descriptor_bytes,
             source=u.proto_path.name,
             run_id=run_id, kind=cfg.store.kind, host=cfg.store.host,
             port=cfg.store.port, database=cfg.store.database,
             batch_size=cfg.store.batch_size, options=cfg.store.options)
        for u in units
    ]
    workers = min(cfg.store.workers, len(jobs))
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(_run_job, jobs):
            report.units.append(res)
    return _reconcile(report)


def _reconcile(report: LoadReport) -> LoadReport:
    """A run is 'failed' if any unit's main-table rows != records read."""
    if any(not u.ok for u in report.units):
        report.status = "failed"
    return report


# --------------------------------------------------------------------------
# batch: load every ProfileData-* run under a parent directory, sequentially
# --------------------------------------------------------------------------

RUN_PREFIX = "ProfileData-"


def find_runs(parent: Path) -> list[Path]:
    """The ProfileData-* run directories directly under `parent`, name-sorted.
    Empty if there are none (i.e. `parent` is itself a single run)."""
    if not parent.is_dir():
        return []
    return sorted(d for d in parent.iterdir()
                  if d.is_dir() and d.name.startswith(RUN_PREFIX))


@dataclass
class RunOutcome:
    run_id: str
    status: str                 # "complete" | "failed" | "skipped"
    exit_code: int = 0
    error: str = ""
    report: dict | None = None  # LoadReport.to_dict(), when the run was attempted


@dataclass
class BatchReport:
    parent: str
    outcomes: list[RunOutcome] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        # first failing run's code (0 if none failed)
        for o in self.outcomes:
            if o.status == "failed":
                return o.exit_code
        return ExitCode.OK

    @property
    def status(self) -> str:
        return "complete" if all(o.status == "complete" for o in self.outcomes) else "failed"

    def to_dict(self) -> dict:
        n = {s: sum(1 for o in self.outcomes if o.status == s)
             for s in ("complete", "failed", "skipped")}
        return {
            "mode": "batch",
            "parent": self.parent,
            "status": self.status,
            "totals": {"runs": len(self.outcomes), **n},
            "runs": [
                {"run_id": o.run_id, "status": o.status,
                 **({"exit_code": o.exit_code, "error": o.error}
                    if o.status == "failed" else {}),
                 **({"totals": o.report["totals"]} if o.report else {})}
                for o in self.outcomes
            ],
        }


def run_batch(parent: Path, cfg: Config, store: Store | None = None,
              continue_on_error: bool = False) -> BatchReport:
    """Load each ProfileData-* run under `parent`, sequentially (each run still
    parallelizes its own units). Default is fail-fast: the first failed run stops
    the batch and the rest are marked skipped. With continue_on_error, every run
    is attempted and failures are collected. A run 'fails' on a raised error OR a
    reconciliation mismatch."""
    run_dirs = find_runs(parent)
    if not run_dirs:
        raise InputError(f"no {RUN_PREFIX}* directories under {parent}")

    batch = BatchReport(parent=str(parent))
    stop = False
    for run_dir in run_dirs:
        if stop:
            batch.outcomes.append(RunOutcome(run_dir.name, "skipped"))
            continue
        try:
            report = run_load(run_dir, cfg, store)
        except AnalysisFWError as exc:
            batch.outcomes.append(RunOutcome(run_dir.name, "failed",
                                             exc.exit_code, str(exc)))
            stop = not continue_on_error
            continue
        if report.status != "complete":
            bad = [u.stem for u in report.units if not u.ok]
            batch.outcomes.append(RunOutcome(
                run_dir.name, "failed", ExitCode.INTEGRITY,
                f"reconciliation mismatch in: {', '.join(bad)}", report.to_dict()))
            stop = not continue_on_error
        else:
            batch.outcomes.append(RunOutcome(run_dir.name, "complete",
                                             report=report.to_dict()))
    return batch
