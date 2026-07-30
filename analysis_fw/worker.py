"""Load one unit (one <layer>_<type>) into its table.

For each record (from the wrapper's repeated field), flatten the generic header
sub-message and the payload sub-message into one flat row, prefixed with run_id
and a parsed ts, suffixed with the ingest time. Column order matches
TableSchema.columns.

Usable in-process (tests, single worker) or in a child process (parallel) — both
call `load_unit`, so the decode/row-building path is identical and tested once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import InputError
from .reader import iter_records
from .registry import TableSchema
from .store.base import Store

# Header timestamp per spec §3.1, e.g. "Mon Jul 27 18:02:21 2026",
# optionally wrapped in brackets.
_TS_FORMAT = "%a %b %d %H:%M:%S %Y"
_BRACKETS = re.compile(r"^\[(.*)\]$")


@dataclass
class UnitResult:
    stem: str
    table: str
    files: int
    read: int
    inserted: int

    @property
    def ok(self) -> bool:
        return self.read == self.inserted


def parse_ts(s: str) -> datetime | None:
    """Parse the header timestamp to a DateTime. Returns None (stored as the
    zero DateTime) if empty or unparseable, so one odd timestamp never fails a
    whole load — the original string is always kept in the `timestamp` column."""
    if not s:
        return None
    s = s.strip()
    m = _BRACKETS.match(s)
    if m:
        s = m.group(1).strip()
    try:
        return datetime.strptime(s, _TS_FORMAT)
    except ValueError:
        return None


_EPOCH = datetime(1970, 1, 1)


def load_unit(pb_parts: list[Path], schema: TableSchema, store: Store,
              run_id: str, batch_size: int) -> UnitResult:
    """Load all .pb parts of one unit into its table via `store`."""
    store.ensure_table(schema)
    loaded_at = datetime.now().replace(microsecond=0)
    column_names = [c.name for c in schema.columns]
    generic_field = schema.generic_field
    payload_field = schema.payload_field
    generic_fields = schema.generic_fields
    payload_fields = schema.payload_fields

    read = inserted = 0
    batch: list[tuple] = []

    for part in pb_parts:
        for rec in iter_records(part, schema):
            read += 1
            g = getattr(rec, generic_field)
            p = getattr(rec, payload_field)
            ts = parse_ts(g.timestamp) or _EPOCH
            row = (
                run_id, ts,
                *(getattr(g, f) for f in generic_fields),
                *(getattr(p, f) for f in payload_fields),
                loaded_at,
            )
            batch.append(row)
            if len(batch) >= batch_size:
                store.insert(schema.stem, column_names, batch)
                inserted += len(batch)
                batch.clear()

    if batch:
        store.insert(schema.stem, column_names, batch)
        inserted += len(batch)

    return UnitResult(stem=schema.stem, table=schema.stem,
                      files=len(pb_parts), read=read, inserted=inserted)
