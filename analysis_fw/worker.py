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
# a stray offset appended after 'Z' (e.g. "...Z00:00" or "...Z05:30"). RFC 3339
# uses either 'Z' (UTC) OR an offset, never both — this is a producer bug. 'Z'
# already means UTC, so we drop the trailing offset and keep 'Z'.
_Z_OFFSET = re.compile(r"Z[+-]?\d{2}:?\d{2}$")

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
    files: int
    records: int                    # parent records read
    table_rows: dict                # table name -> rows inserted (main + children)

    @property
    def ok(self) -> bool:
        # reconcile: the main table got exactly one row per parent record
        return self.table_rows.get(self.stem, 0) == self.records

    @property
    def total_rows(self) -> int:
        return sum(self.table_rows.values())


def parse_ts(s: str) -> datetime | None:
    """Parse the header timestamp string to a datetime (aware if the string
    carries a timezone), or None if it matches no known format. The original
    string is always kept in the `timestamp` column, so a parse miss loses
    nothing."""
    if not s:
        return None
    s = s.strip()
    m = _BRACKETS.match(s)
    if m:
        s = m.group(1).strip()
    s = _Z_OFFSET.sub("Z", s)   # normalize malformed "...Z00:00" -> "...Z"

    # ISO 8601 / RFC 3339 first — handles fractional seconds and a Z/offset
    # timezone (e.g. 2026-07-31T16:33:53.005Z or ...+05:30). The Z rewrite keeps
    # this working on Python < 3.11 too.
    iso = (s[:-1] + "+00:00") if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        pass

    # Fallback: human-readable formats. strptime here has no fractional-seconds
    # slot, so drop a trailing millisecond group first.
    s2 = _MILLIS.sub(r"\1", s)
    for fmt, has_year in _TS_FORMATS:
        try:
            dt = datetime.strptime(s2, fmt)
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
    # A tz-aware timestamp (ISO with Z/offset) is converted to the real UTC
    # instant; a naive one is interpreted as UTC. Either way ts is deterministic.
    dt = dt.astimezone(_UTC) if dt.tzinfo is not None else dt.replace(tzinfo=_UTC)
    if dt < _DT_MIN:
        return _DT_MIN
    if dt > _DT_MAX:
        return _DT_MAX
    return dt


def load_unit(pb_parts: list[Path], schema: TableSchema, store: Store,
              run_id: str, batch_size: int) -> UnitResult:
    """Load one unit into its main table plus a child table per repeated payload
    sub-message. A `record_id` (UInt64 sequence, per record) links a main row to
    its child rows."""
    store.ensure_table(schema.stem, schema.columns, schema.order_by)
    for ch in schema.children:
        store.ensure_table(ch.table, ch.columns, ch.order_by)

    loaded_at = datetime.now().replace(microsecond=0)
    generic_field = schema.generic_field
    payload_field = schema.payload_field
    generic_fields = schema.generic_fields
    payload_fields = schema.payload_fields
    has_rid = schema.has_record_id
    children = schema.children

    main_cols = [c.name for c in schema.columns]
    child_cols = {ch.table: [c.name for c in ch.columns] for ch in children}

    table_rows: dict[str, int] = {schema.stem: 0}
    for ch in children:
        table_rows[ch.table] = 0
    main_batch: list[tuple] = []
    child_batches: dict[str, list[tuple]] = {ch.table: [] for ch in children}

    def flush_main():
        if main_batch:
            store.insert(schema.stem, main_cols, main_batch)
            table_rows[schema.stem] += len(main_batch)
            main_batch.clear()

    def flush_child(ch: "object"):
        b = child_batches[ch.table]
        if b:
            store.insert(ch.table, child_cols[ch.table], b)
            table_rows[ch.table] += len(b)
            b.clear()

    records = 0
    record_id = 0
    for part in pb_parts:
        for rec in iter_records(part, schema):
            records += 1
            g = getattr(rec, generic_field)
            p = getattr(rec, payload_field)
            ts = ts_for_db(g.timestamp)
            gen_vals = tuple(getattr(g, f) for f in generic_fields)

            if has_rid:
                main_batch.append((run_id, ts, record_id, *gen_vals,
                                   *(getattr(p, f) for f in payload_fields), loaded_at))
            else:
                main_batch.append((run_id, ts, *gen_vals,
                                   *(getattr(p, f) for f in payload_fields), loaded_at))

            for ch in children:
                cb = child_batches[ch.table]
                for sub in getattr(p, ch.repeated_field):
                    cb.append((run_id, ts, record_id, *gen_vals,
                               *(getattr(sub, f) for f in ch.sub_fields), loaded_at))
                if len(cb) >= batch_size:
                    flush_child(ch)

            record_id += 1
            if len(main_batch) >= batch_size:
                flush_main()

    flush_main()
    for ch in children:
        flush_child(ch)

    return UnitResult(stem=schema.stem, files=len(pb_parts),
                      records=records, table_rows=table_rows)
