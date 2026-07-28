#!/usr/bin/env python3
"""
Generate a ProfileData-* tree in the NEW naming convention, for testing Module 1.

Layout produced (per Analysis-FW-Module1-MVP-Design.md):

    ProfileData-<tag>-<timestamp>/
      Config/run_config.log                 (empty)
      Linux/Block/
        block_type1.proto                   schema
        block_type1.000.pb                  data, split part 0
        block_type1.001.pb                  data, split part 1
        block_type2.proto
        block_type2.pb                       data, single file
      Linux/NVMe/
        nvme_type1.proto
        nvme_type1.pb
      SSD/
        ssd_type1.proto
        ssd_type1.pb

Every message follows the format contract: header fields 1-5 + nested Payload
in field 6 (scalars only). Records are varint length-delimited.

    python make_fixture.py [output_root]
"""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

from grpc_tools import protoc
import grpc_tools
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


# Each entry: (relative dir, stem, proto text, record count, split count)
FIXTURE_PROTOS = {
    "block_type1": '''
syntax = "proto3";
package fix.block1;
message Rec {
  string timestamp = 1;
  string hostname  = 2;
  uint32 component = 3;
  uint32 tag       = 4;
  uint32 loglevel  = 5;
  message Payload {
    uint64 event_seq  = 1;
    string disk       = 2;
    string op         = 3;
    uint32 bytes      = 4;
    uint64 latency_ns = 5;
    int32  error      = 6;
  }
  Payload payload = 6;
}''',
    "block_type2": '''
syntax = "proto3";
package fix.block2;
message Rec {
  string timestamp = 1;
  string hostname  = 2;
  uint32 component = 3;
  uint32 tag       = 4;
  uint32 loglevel  = 5;
  message Payload {
    uint64 event_seq   = 1;
    uint32 queue_depth = 2;
    double util_pct    = 3;
    bool   throttled   = 4;
  }
  Payload payload = 6;
}''',
    "nvme_type1": '''
syntax = "proto3";
package fix.nvme1;
message Rec {
  string timestamp = 1;
  string hostname  = 2;
  uint32 component = 3;
  uint32 tag       = 4;
  uint32 loglevel  = 5;
  message Payload {
    uint64 event_seq = 1;
    uint32 qid       = 2;
    uint32 cid       = 3;
    uint64 slba      = 4;
  }
  Payload payload = 6;
}''',
    "ssd_type1": '''
syntax = "proto3";
package fix.ssd1;
message Rec {
  string timestamp = 1;
  string hostname  = 2;
  uint32 component = 3;
  uint32 tag       = 4;
  uint32 loglevel  = 5;
  message Payload {
    uint64 event_seq      = 1;
    float  temperature_c  = 2;
    uint64 power_on_hours = 3;
  }
  Payload payload = 6;
}''',
}

# stem -> (dir, component_id, record_count, split_count)
PLACEMENT = {
    "block_type1": ("Linux/Block", 1, 1000, 2),
    "block_type2": ("Linux/Block", 1, 400, 1),
    "nvme_type1":  ("Linux/NVMe", 2, 600, 1),
    "ssd_type1":   ("SSD", 6, 50, 1),
}

TS = "Sat Jun 27 14:40:48 2026"
HOSTNAME = "dgx-spark-01"
TAG = 7


def build_class(stem: str, text: str):
    tmp = Path(tempfile.mkdtemp())
    (tmp / f"{stem}.proto").write_text(text)
    inc = Path(grpc_tools.__file__).parent / "_proto"
    out = tmp / "d.desc"
    rc = protoc.main(["protoc", f"-I{tmp}", f"-I{inc}",
                      f"--descriptor_set_out={out}", "--include_imports",
                      str(tmp / f"{stem}.proto")])
    if rc != 0:
        raise SystemExit(f"protoc failed for {stem}")
    pool = descriptor_pool.DescriptorPool()
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(out.read_bytes())
    for f in fds.file:
        pool.Add(f)
    # find the one top-level message
    name = [m.name for f in fds.file for m in f.message_type][0]
    pkg = fds.file[0].package
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(f"{pkg}.{name}")), text


def make_records(cls, component: int, n: int) -> list[bytes]:
    out = []
    for i in range(1, n + 1):
        m = cls()
        m.timestamp = TS
        m.hostname = HOSTNAME
        m.component = component
        m.tag = TAG
        m.loglevel = 3
        p = m.payload
        # set whatever scalar payload fields exist, generically
        for f in p.DESCRIPTOR.fields:
            if f.name == "event_seq":
                setattr(p, f.name, i)
            elif f.cpp_type in (f.CPPTYPE_INT32, f.CPPTYPE_INT64,
                                f.CPPTYPE_UINT32, f.CPPTYPE_UINT64):
                setattr(p, f.name, (i * 7) % 1000)
            elif f.cpp_type in (f.CPPTYPE_FLOAT, f.CPPTYPE_DOUBLE):
                setattr(p, f.name, float(i % 100) + 0.5)
            elif f.cpp_type == f.CPPTYPE_BOOL:
                setattr(p, f.name, i % 2 == 0)
            elif f.cpp_type == f.CPPTYPE_STRING:
                setattr(p, f.name, ["read", "write", "flush"][i % 3])
        out.append(m.SerializeToString())
    return out


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def write_split(path_stem: Path, blobs: list[bytes], splits: int) -> None:
    """Write blobs across `splits` .pb files, framed varint-delimited."""
    if splits == 1:
        with open(f"{path_stem}.pb", "wb") as fh:
            for b in blobs:
                fh.write(varint(len(b)) + b)
        return
    per = (len(blobs) + splits - 1) // splits
    for idx in range(splits):
        chunk = blobs[idx * per:(idx + 1) * per]
        with open(f"{path_stem}.{idx:03d}.pb", "wb") as fh:
            for b in chunk:
                fh.write(varint(len(b)) + b)


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./fixture_out")
    run_dir = root / "ProfileData-fixture-20260627-144048"
    (run_dir / "Config").mkdir(parents=True, exist_ok=True)
    (run_dir / "Config" / "run_config.log").touch()

    total = 0
    for stem, text in FIXTURE_PROTOS.items():
        subdir, component, n, splits = PLACEMENT[stem]
        d = run_dir / subdir
        d.mkdir(parents=True, exist_ok=True)
        cls, proto_text = build_class(stem, text)
        (d / f"{stem}.proto").write_text(proto_text)
        blobs = make_records(cls, component, n)
        write_split(d / stem, blobs, splits)
        total += n
        print(f"  {subdir}/{stem}: {n} records, {splits} file(s)")

    print(f"\n{total} records total -> {run_dir}")


if __name__ == "__main__":
    main()
