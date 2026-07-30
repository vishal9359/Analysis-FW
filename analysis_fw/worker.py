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
from datetime import datetime, timezone
from pathlib import Path

from .errors import InputError
from .reader import iter_records
from .registry import TableSchema
from .store.base import Store

# Header timestamp. Producers have shipped a few shapes, so we try several:
#   "Mon Jul 27 18:02:21 2026"        (abbreviated names + year)
#   "Monday July 27 18:01:46:233"     (full names, NO year, trailing ms)
#   ISO-8601                          "2026-07-27T18:02:21"
# (fmt, has_year); no-year formats get the current year injected.
_TS_FORMATS = [
    ("%a %b %d %H:%M:%S %Y", True),
    ("%A %B %d %H:%M:%S %Y", True),
    ("%Y-%m-%dT%H:%M:%S", True),
    ("%Y-%m-%d %H:%M:%S", True),
    ("%a %b %d %H:%M:%S", False),
    ("%A %B %d %H:%M:%S", False),
]
_BRACKETS = re.compile(r"^\[(.*)\]$")
# a millisecond group tacked onto the time, e.g. "18:01:46:233" or "18:01:46.233"
_MILLIS = re.compile(r"(\d{2}:\d{2}:\d{2})[:.]\d{1,6}\b")

# ClickHouse DateTime is an unsigned 32-bit epoch: 1970-01-01 .. 2106-02-07 UTC.
# We treat the (tz-less) header string as UTC so the stored ts is deterministic
# regardless of the loader machine's timezone, and clamp to this range so a bad
# timestamp can never serialize out of bounds (which crashes the insert).
_UTC = timezone.utc
_DT_MIN = datetime(1970, 1, 1, tzinfo=_UTC)
_DT_MAX = datetime(2106, 2, 7, tzinfo=_UTC)


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
    """Parse the header timestamp string to a naive datetime, or None if it
    matches no known format. The original string is always kept in the
    `timestamp` column, so a parse miss never loses information."""
    if not s:
        return None
    s = s.strip()
    m = _BRACKETS.match(s)
    if m:
        s = m.group(1).strip()
    s = _MILLIS.sub(r"\1", s)   # drop trailing milliseconds
    for fmt, has_year in _TS_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if not has_year:
            dt = dt.replace(year=datetime.now().year)
        return dt
    return None


def ts_for_db(s: str) -> datetime:
    """The `ts` column value: a UTC-aware DateTime, clamped to ClickHouse's
    representable range so it always serializes to a valid unsigned epoch.
    Unparseable timestamps become the epoch (1970-01-01), never a crash."""
    dt = parse_ts(s)
    if dt is None:
        return _DT_MIN
    dt = dt.replace(tzinfo=_UTC)
    if dt < _DT_MIN:
        return _DT_MIN
    if dt > _DT_MAX:
        return _DT_MAX
    return dt


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
            row = (
                run_id, ts_for_db(g.timestamp),
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
