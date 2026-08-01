#!/usr/bin/env python3
"""
Generate a ProfileData-* tree from real .proto files, with FULL payloads.

Reads every .proto in tests/sample_protos/ (the real Profile FW protos), and for
each one generates wrapper .pb data that fills EVERY generic and payload field —
so the fixture always matches the proto, even after fields are added. It reuses
the loader's own schema detection (analysis_fw.registry), so the fixture can
never drift from what the loader expects.

    python make_fixture.py [output_root] [--protos DIR]

A .pb file is one wrapper message holding a `repeated` list of records (no
delimiters). A big unit is split into several .pb files, each a complete wrapper.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from analysis_fw.registry import build_schema  # reuse the loader's detection

DEFAULT_PROTOS = HERE / "sample_protos"

# How many records per unit, and how many .pb files to split it across.
# (Splitting one keeps the multi-file path exercised.)
COUNTS = {"linux_block_1_stats": (1000, 2), "linux_block_2_misc": (400, 1),
          "linux_nvme_1_stats": (300, 1)}
DEFAULT_COUNT, DEFAULT_SPLITS = 200, 1

# Layer subdir from the filename's layer token.
LAYER_DIRS = {
    "block": "Linux/Block", "nvme": "Linux/NVMe", "syscall": "Linux/Syscall",
    "memory": "Linux/Memory", "filesystem": "Linux/Filesystem",
    "platform": "Platform", "ssd": "SSD",
}

TIMESTAMP = "Mon Jul 27 18:02:21 2026"
HOSTNAME = "spark-e97e"
TAG = "test1"


def layer_dir_for(stem: str) -> str:
    for tok in stem.split("_"):
        if tok in LAYER_DIRS:
            return LAYER_DIRS[tok]
    return "Linux/Other"


def _set_scalar(msg, field, i: int) -> None:
    """Set one scalar field to a distinct sample value, by type."""
    cpp = field.cpp_type
    if cpp == field.CPPTYPE_STRING:
        setattr(msg, field.name, "nvme0n1" if field.name == "device" else f"s{i}")
    elif cpp in (field.CPPTYPE_FLOAT, field.CPPTYPE_DOUBLE):
        setattr(msg, field.name, float(i % 100) + 0.5)
    elif cpp == field.CPPTYPE_BOOL:
        setattr(msg, field.name, i % 2 == 0)
    elif cpp == field.CPPTYPE_ENUM:
        setattr(msg, field.name, 0)
    else:  # any int
        setattr(msg, field.name, (i * 7) % 1_000_000)


def _sub_count(field_name: str, i: int) -> int:
    """A varying, record-dependent number of sub-elements, so per_queue and
    per_core counts differ per record and are independent of each other."""
    if "queue" in field_name:
        return 3 + (i % 4)      # 3..6 queues
    if "core" in field_name or "cpu" in field_name:
        return 8 + (i % 5)      # 8..12 cores
    return 2 + (i % 3)


def make_wrapper(schema, count: int):
    """Build one wrapper message with `count` fully-populated records, including
    repeated sub-messages (per_queue / per_core) at varying counts."""
    from google.protobuf.descriptor import FieldDescriptor as FD
    wrapper = schema.wrapper_cls()
    records = getattr(wrapper, schema.repeated_field)
    for i in range(1, count + 1):
        rec = records.add()
        g = getattr(rec, schema.generic_field)
        g.timestamp = TIMESTAMP
        g.hostname = HOSTNAME
        g.component = 1
        g.tag = TAG
        g.log_level = 3
        p = getattr(rec, schema.payload_field)
        for f in p.DESCRIPTOR.fields:
            if f.is_repeated and f.type == FD.TYPE_MESSAGE:
                rep = getattr(p, f.name)
                for k in range(_sub_count(f.name, i)):
                    sub = rep.add()
                    for j, sf in enumerate(sub.DESCRIPTOR.fields):
                        # first field is the key dimension (queue_id / cpu_id)
                        _set_scalar(sub, sf, k if j == 0 else i + k)
            elif not f.is_repeated and f.type != FD.TYPE_MESSAGE:
                _set_scalar(p, f, i)
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
            wrapper = make_wrapper(schema, hi - lo)
            name = f"{stem}.pb" if splits == 1 else f"{stem}.{idx:03d}.pb"
            (d / name).write_bytes(wrapper.SerializeToString())

        n_payload = len(schema.payload_fields)
        total += count
        print(f"  {layer_dir_for(stem)}/{stem}: {count} records, "
              f"{splits} file(s), {n_payload} payload fields")

    print(f"\n{total} records total -> {run_dir}")


if __name__ == "__main__":
    main()
