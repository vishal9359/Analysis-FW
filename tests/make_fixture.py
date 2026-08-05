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
        setattr(msg, field.name, 0)
    else:  # any int -> cumulative counter
        setattr(msg, field.name, _counter_value(field, gi))


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
                rep = getattr(p, f.name)
                for k in range(_sub_count(f.name, gi)):
                    sub = rep.add()
                    for idx, sf in enumerate(sub.DESCRIPTOR.fields):
                        if idx == 0:
                            setattr(sub, sf.name, k)        # key dim: queue_id/cpu_id
                        else:
                            _set_scalar(sub, sf, gi + k)
            elif not f.is_repeated and f.type != FD.TYPE_MESSAGE:
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
