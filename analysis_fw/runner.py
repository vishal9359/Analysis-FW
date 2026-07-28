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
from .errors import AnalysisFWError, DatabaseError
from .registry import build_schema, build_schema_from_descriptor
from .store.clickhouse import ClickHouseStore
from .store.base import Store
from .worker import UnitResult, load_unit


@dataclass
class LoadReport:
    run_id: str
    producer: str
    database: str
    units: list[UnitResult] = field(default_factory=list)
    status: str = "complete"

    @property
    def read(self) -> int:
        return sum(u.read for u in self.units)

    @property
    def inserted(self) -> int:
        return sum(u.inserted for u in self.units)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "producer": self.producer,
            "database": self.database,
            "totals": {"read": self.read, "inserted": self.inserted,
                       "units": len(self.units)},
            "units": [
                {"table": u.table, "files": u.files,
                 "read": u.read, "inserted": u.inserted, "ok": u.ok}
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
    message_full_name: str
    run_id: str
    host: str
    port: int
    database: str
    batch_size: int
    async_insert: bool
    framing: str


def _run_job(job: _Job) -> UnitResult:
    """Executed in a child process. Rebuilds everything from picklable inputs —
    descriptor bytes and paths — because message classes and live connections
    cannot be pickled."""
    schema = build_schema_from_descriptor(
        job.stem, job.descriptor_bytes, job.message_full_name, source=job.stem)
    store = ClickHouseStore(job.host, job.port, job.database, job.async_insert)
    store.connect()
    try:
        return load_unit([Path(p) for p in job.pb_parts], schema, store,
                         job.run_id, job.batch_size, job.framing)
    finally:
        store.close()


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def run_load(input_dir: Path, cfg: Config, store: Store | None = None) -> LoadReport:
    """Discover and load a run. If `store` is given, run sequentially against it
    (used by tests). Otherwise use ClickHouse with the configured worker pool."""
    run_id = input_dir.resolve().name
    units = discover(input_dir)
    schemas = {u.stem: build_schema(u.stem, u.proto_path) for u in units}

    report = LoadReport(run_id=run_id, producer=cfg.producer_name,
                        database=cfg.store.database)

    # sequential path (injected store, or workers == 1)
    if store is not None or cfg.store.workers == 1:
        own = store is None
        st = store or ClickHouseStore(cfg.store.host, cfg.store.port,
                                      cfg.store.database, cfg.store.async_insert)
        if own:
            st.connect()
        try:
            for u in units:
                report.units.append(
                    load_unit(u.pb_parts, schemas[u.stem], st, run_id,
                              cfg.store.batch_size, cfg.framing))
        finally:
            if own:
                st.close()
        return report

    # parallel path — one job per unit, across a process pool
    jobs = [
        _Job(stem=u.stem, pb_parts=[str(p) for p in u.pb_parts],
             descriptor_bytes=schemas[u.stem].descriptor_bytes,
             message_full_name=schemas[u.stem].message_full_name,
             run_id=run_id, host=cfg.store.host, port=cfg.store.port,
             database=cfg.store.database, batch_size=cfg.store.batch_size,
             async_insert=cfg.store.async_insert, framing=cfg.framing)
        for u in units
    ]
    workers = min(cfg.store.workers, len(jobs))
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(_run_job, jobs):
            report.units.append(res)
    return report
