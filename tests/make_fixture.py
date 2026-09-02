#!/usr/bin/env python3
"""
Generate a ProfileData-* tree from real .proto files, with FULL payloads.

Reads every .proto in tests/sample_protos/ and generates wrapper .pb data that
fills every generic and payload field. It reuses the loader's own schema
detection (src.registry), so the fixture can never drift from what the
loader expects.

The data is a realistic time series: each record gets an increasing per-second
timestamp, and the integer fields are monotonically-increasing cumulative
counters — so per-second delta queries (IOPS, bandwidth, latency) produce
sensible values, not zeros.

    python make_fixture.py [output_root] [--protos DIR]

A .pb file is one wrapper message holding a `repeated` list of records. A big
unit is split into several .pb files, each a complete wrapper.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.registry import build_schema  # reuse the loader's detection

DEFAULT_PROTOS = HERE / "sample_protos"

# records per unit, and how many .pb files to split it across
COUNTS = {"linux_block_1_stats": (1000, 2), "linux_block_2_misc": (400, 1),
          "linux_nvme_1_stats": (300, 1)}
DEFAULT_COUNT, DEFAULT_SPLITS = 200, 1

LAYER_DIRS = {
    "block": "Linux/Block", "nvme": "Linux/NVMe", "syscall": "Linux/Syscall",
    "memory": "Linux/Memory", "filesystem": "Linux/Filesystem",
    "platform": "Platform", "ssd": "SSD",
}

BASE_TIME = datetime(2026, 7, 27, 18, 2, 21, tzinfo=timezone.utc)
HOSTNAME = "spark-e97e"
TAG = "test1"

# Per-second increments for known /proc/diskstats counters, chosen so the
# derived metrics are realistic: ~2 ms/read, ~3 ms/write, ~8 sectors (4 KiB)/IO.
_COUNTER_STEP = {
    "read_ios": 200, "write_ios": 600,
    "sectors_read": 1600, "sectors_written": 4800,
    "read_time_ms": 400, "write_time_ms": 1800,
}


def layer_dir_for(stem: str) -> str:
    for tok in stem.split("_"):
        if tok in LAYER_DIRS:
            return LAYER_DIRS[tok]
    return "Linux/Other"


def _iso(gi: int) -> str:
    """RFC 3339 UTC timestamp, one second per record."""
    return (BASE_TIME + timedelta(seconds=gi)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _counter_value(field, gi: int) -> int:
    """A monotonically-increasing cumulative counter: linear + a gentle ramp so
    per-second deltas rise over the run. The ramp cancels in latency ratios
    (read_time/read_ios), so latency stays ~constant while IOPS/bandwidth climb."""
    step = _COUNTER_STEP.get(field.name, (field.number + 1) * 50)
    return step * gi + step * gi * gi // 2000


def _set_scalar(msg, field, gi: int) -> None:
    cpp = field.cpp_type
    if cpp == field.CPPTYPE_STRING:
        setattr(msg, field.name, "nvme0n1" if field.name == "device" else f"s{gi}")
    elif cpp in (field.CPPTYPE_FLOAT, field.CPPTYPE_DOUBLE):
        setattr(msg, field.name, float(gi % 100) + 0.5)
    elif cpp == field.CPPTYPE_BOOL:
        setattr(msg, field.name, gi % 2 == 0)
    elif cpp == field.CPPTYPE_ENUM:
        # cycle through the declared values, skipping the 0 default where the
        # enum has real ones — otherwise a column like `dir` is UNSPECIFIED on
        # every row and never exercises the values queries group by.
        vals = [v.number for v in field.enum_type.values if v.number != 0] or [0]
        setattr(msg, field.name, vals[gi % len(vals)])
    else:  # any int -> cumulative counter
        setattr(msg, field.name, _counter_value(field, gi))


# A record covers one second of wall clock. Leaf measurement windows
# (per_core_seq_random[].entries[]) tile that second, so start_time/end_time are
# a real interval instead of two unrelated counters — and so `ORDER BY
# start_time` returns them in the order they were measured, which is the only
# ordering the database can give back (ClickHouse does not preserve insertion
# order; see docs/decisions/0010-payload-field-shapes-to-tables.md).
_NS = 1_000_000_000
_EPOCH_NS = int(BASE_TIME.timestamp()) * _NS
_WINDOW_FIELDS = {"start_time": 0, "end_time": 1}


def _window(gi: int, slot: int, j: int, n: int) -> tuple[int, int]:
    """The j-th of n measurement windows in record `gi`, for group `slot`.
    Non-overlapping within a group, skewed across groups so cores don't all
    report byte-identical timestamps."""
    span = _NS // max(n, 1)
    start = _EPOCH_NS + gi * _NS + (slot * 7919) % span + j * span
    return start, start + span * 3 // 4          # 75% busy, 25% idle gap


def _enum_dimension(msg_desc):
    """The categorical dimension a grouping level fans out over, if it has one.

    NvmeCoreSeqRandom{cpu_id, dir, entries[]} is measured for every combination
    of its key dimension and its enum — 12 cores x {READ, WRITE} = 24 groups,
    the same cpu_id appearing once per direction. Without this each core would
    get exactly one direction and the fixture would never produce the shape the
    leaf table exists to hold.
    """
    from google.protobuf.descriptor import FieldDescriptor as FD
    for f in msg_desc.fields:
        if not f.is_repeated and f.type == FD.TYPE_ENUM:
            vals = [v.number for v in f.enum_type.values if v.number != 0]
            if vals:
                return f.name, vals
    return None, [None]


def _fill_repeated(parent, field, gi: int, depth: int = 0, slot: int = 0) -> None:
    """Add elements to a repeated message field, recursing when an element
    itself holds a repeated message (per_core_seq_random[].entries[])."""
    rep = getattr(parent, field.name)
    enum_name, enum_vals = (_enum_dimension(field.message_type) if depth == 0
                            else (None, [None]))
    n = _sub_count(field.name, gi)
    i = 0
    for k in range(n):
        for ev in enum_vals:                 # key dimension x direction
            child_slot = i if depth == 0 else slot
            win = _window(gi, child_slot, k, n) if depth else None
            _fill_element(rep.add(), gi, k, depth, child_slot, enum_name, ev, win)
            i += 1


def _fill_element(msg, gi: int, k: int, depth: int, slot: int = 0,
                  enum_name: str | None = None, enum_val=None, win=None) -> None:
    """Fill one repeated element. At the outermost level the first scalar is the
    key dimension (queue_id / cpu_id) so it enumerates 0..N and the enum is
    pinned to the value this group stands for; a leaf element gets a real
    measurement window rather than two independent counters."""
    from google.protobuf.descriptor import FieldDescriptor as FD
    key_dim = depth == 0
    for f in msg.DESCRIPTOR.fields:
        if f.is_repeated and f.type == FD.TYPE_MESSAGE:
            _fill_repeated(msg, f, gi, depth + 1, slot)
        elif f.is_repeated:
            continue                       # repeated scalar — loader rejects these
        elif f.type == FD.TYPE_MESSAGE:
            _fill_nested(getattr(msg, f.name), gi)
        elif f.name == enum_name:
            setattr(msg, f.name, enum_val)  # the direction this group stands for
        elif key_dim:
            setattr(msg, f.name, k)         # key dimension
            key_dim = False
        elif win and f.name in _WINDOW_FIELDS:
            setattr(msg, f.name, win[_WINDOW_FIELDS[f.name]])
        else:
            # phase counters by group and element, so cores differ from each
            # other instead of every group reporting the same numbers
            _set_scalar(msg, f, gi + k + slot)


def _fill_nested(msg, gi: int) -> None:
    """Fill a singular sub-message, recursing through further 1:1 groups."""
    from google.protobuf.descriptor import FieldDescriptor as FD
    for f in msg.DESCRIPTOR.fields:
        if f.is_repeated:
            continue
        if f.type == FD.TYPE_MESSAGE:
            _fill_nested(getattr(msg, f.name), gi)
        else:
            _set_scalar(msg, f, gi)


def _sub_count(field_name: str, gi: int) -> int:
    """Varying, record-dependent number of sub-elements (per_queue / per_core)."""
    if "queue" in field_name:
        return 3 + (gi % 4)      # 3..6 queues
    if "core" in field_name or "cpu" in field_name:
        return 8 + (gi % 5)      # 8..12 cores
    return 2 + (gi % 3)


def make_wrapper(schema, count: int, start: int = 0):
    """Build one wrapper with `count` records, numbered gi = start .. start+count-1
    so split files continue one global time series."""
    from google.protobuf.descriptor import FieldDescriptor as FD
    wrapper = schema.wrapper_cls()
    records = getattr(wrapper, schema.repeated_field)
    for j in range(count):
        gi = start + j
        rec = records.add()
        g = getattr(rec, schema.generic_field)
        g.timestamp = _iso(gi)
        g.hostname = HOSTNAME
        g.component = 1
        g.tag = TAG
        g.log_level = 3
        p = getattr(rec, schema.payload_field)
        for f in p.DESCRIPTOR.fields:
            if f.is_repeated and f.type == FD.TYPE_MESSAGE:
                _fill_repeated(p, f, gi)
            elif not f.is_repeated and f.type == FD.TYPE_MESSAGE:
                # 1:1 group (e.g. io_flags) — the loader flattens it onto the
                # row, so fill it too or those columns would always be zero.
                _fill_nested(getattr(p, f.name), gi)
            elif not f.is_repeated:
                _set_scalar(p, f, gi)
    return wrapper


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("output_root", nargs="?", default="./fixture_out", type=Path)
    ap.add_argument("--protos", type=Path, default=DEFAULT_PROTOS,
                    help="directory of .proto files (default: tests/sample_protos)")
    args = ap.parse_args()

    protos = sorted(args.protos.glob("*.proto"))
    if not protos:
        sys.exit(f"no .proto files in {args.protos}")

    run_dir = args.output_root / "ProfileData-fixture-20260727-180221"
    (run_dir / "Config").mkdir(parents=True, exist_ok=True)
    (run_dir / "Config" / "run_config.log").touch()

    total = 0
    for proto in protos:
        stem = proto.stem
        schema = build_schema(stem, proto)
        count, splits = COUNTS.get(stem, (DEFAULT_COUNT, DEFAULT_SPLITS))

        d = run_dir / layer_dir_for(stem)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.proto").write_text(proto.read_text())

        per = (count + splits - 1) // splits
        for idx in range(splits):
            lo, hi = idx * per, min((idx + 1) * per, count)
            if lo >= hi:
                continue
            wrapper = make_wrapper(schema, hi - lo, start=lo)   # continuous series
            name = f"{stem}.pb" if splits == 1 else f"{stem}.{idx:03d}.pb"
            (d / name).write_bytes(wrapper.SerializeToString())

        total += count
        print(f"  {layer_dir_for(stem)}/{stem}: {count} records, {splits} file(s), "
              f"{len(schema.payload_fields)} payload fields")

    print(f"\n{total} records total -> {run_dir}")


if __name__ == "__main__":
    main()
