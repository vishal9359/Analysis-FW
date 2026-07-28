"""Load one unit (one <layer>_<type>) into its table.

The worker is deliberately usable two ways:
  - in-process, given a live Store (tests, single-worker runs);
  - in a child process, given only picklable data, where it builds its own
    Store and message class (parallel runs).

Both call `load_unit`, so the decode/row-building path is identical and tested
once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from google.protobuf.message import DecodeError

from .errors import InputError
from .framing import describe_failure, iter_messages
from .registry import TableSchema
from .store.base import Store

# Header timestamp per format spec §3.1, e.g. "Sat Jun 27 14:40:48 2026",
# optionally wrapped in brackets: "[Sat Jun 27 14:40:48 2026]".
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


def parse_ts(s: str) -> datetime:
    original = s
    s = s.strip()
    m = _BRACKETS.match(s)
    if m:
        s = m.group(1).strip()
    if not s:
        # An empty header timestamp on data that decoded "successfully" almost
        # always means records are being split at the wrong boundaries.
        raise InputError(
            "header timestamp is empty — records are likely being read with the "
            "wrong framing (empty messages decode as all-defaults). Check the "
            "framing with:  python tools/inspect_pb.py <file.pb>")
    try:
        return datetime.strptime(s, _TS_FORMAT)
    except ValueError as exc:
        raise InputError(
            f"cannot parse header timestamp {original!r} "
            f"(expected e.g. 'Sat Jun 27 14:40:48 2026'): {exc}") from exc


def _rows_from_file(pb_path: Path, schema: TableSchema, cls,
                    run_id: str, loaded_at: datetime, sut_expected: str | None,
                    column_names: Sequence[str], batch_size: int, store: Store,
                    framing: str):
    """Decode one .pb file, emit rows in column order, insert in batches.
    Returns (read, inserted, sut_id_seen)."""
    batch: list[tuple] = []
    read = inserted = 0
    sut_id = sut_expected
    payload_fields = schema.payload_fields

    # The framing layer (iter_messages) can raise InputError on an implausible
    # length, and the parser can raise DecodeError on bad content. A wrong
    # framing shows up as either, so both route through one diagnosis.
    records = iter_messages(pb_path, framing)
    while True:
        try:
            raw, offset = next(records)
        except StopIteration:
            break
        except InputError as exc:
            raise InputError(describe_failure(
                pb_path, cls, framing, read + 1, str(exc))) from None

        read += 1
        msg = cls()
        try:
            msg.ParseFromString(raw)
        except DecodeError:
            # Turn "Wire format was corrupt" into a specific, forwardable
            # diagnosis (framing mismatch / schema mismatch / bad record).
            raise InputError(describe_failure(
                pb_path, cls, framing, read, "protobuf wire format was corrupt",
                raw=raw)) from None

        host = msg.hostname
        if sut_id is None:
            sut_id = host
        elif host != sut_id:
            # One run directory is one SUT. A second hostname means misrouted
            # data — fail rather than write it into the wrong partition.
            raise InputError(
                f"{pb_path.name}: record {read} has hostname {host!r} but the "
                f"run is {sut_id!r} — one run belongs to one SUT"
            )

        payload = msg.payload
        row = (
            run_id, sut_id, parse_ts(msg.timestamp),
            msg.tag, msg.loglevel, msg.component, loaded_at,
            *(getattr(payload, f) for f in payload_fields),
        )
        batch.append(row)
        if len(batch) >= batch_size:
            store.insert(schema.stem, column_names, batch)
            inserted += len(batch)
            batch.clear()

    if batch:
        store.insert(schema.stem, column_names, batch)
        inserted += len(batch)
    return read, inserted, sut_id


def load_unit(pb_parts: list[Path], schema: TableSchema, store: Store,
              run_id: str, batch_size: int, framing: str = "varint") -> UnitResult:
    """Load all .pb parts of one unit into its table via `store`."""
    store.ensure_table(schema)
    loaded_at = datetime.now().replace(microsecond=0)
    column_names = [c.name for c in schema.columns]
    read = inserted = 0
    sut_id: str | None = None
    for part in pb_parts:
        r, i, sut_id = _rows_from_file(
            part, schema, schema.message_cls, run_id, loaded_at, sut_id,
            column_names, batch_size, store, framing)
        read += r
        inserted += i
    return UnitResult(stem=schema.stem, table=schema.stem,
                      files=len(pb_parts), read=read, inserted=inserted)
