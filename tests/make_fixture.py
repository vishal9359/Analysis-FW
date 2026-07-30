#!/usr/bin/env python3
"""
Generate a ProfileData-* tree in the wrapper format Profile FW uses.

Each record is a "sample" message = generic_format (GenericFormat header) + a
payload sub-message. Records are written NOT as delimited messages but inside a
single wrapper message with a `repeated` field, one wrapper message per .pb file
(a big run is split into several .pb files, each a complete wrapper message).

    ProfileData-<tag>-<timestamp>/
      Config/run_config.log
      Linux/Block/
        linux_block_1_stats.proto   linux_block_1_stats.pb
        linux_block_2_misc.proto    linux_block_2_misc.pb

    python make_fixture.py [output_root]
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import grpc_tools
from grpc_tools import protoc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

# Two real-shaped protos: header in GenericFormat, payload in its own message,
# a per-record "sample" message (fields named generic_format + payload, per the
# agreed contract), and a repeated wrapper.
PROTOS = {
    "linux_block_1_stats": '''
syntax = "proto3";
message BlockDeviceStat {
  optional string device = 1;
  optional uint64 read_ios = 2;
  optional uint64 sectors_read = 3;
  optional uint64 write_ios = 4;
  optional uint64 io_time = 5;
}
message GenericFormat {
  optional string timestamp = 1;
  optional string hostname = 2;
  optional uint32 component = 3;
  optional string tag = 4;
  optional uint32 log_level = 5;
}
message BlockStatSample {
  GenericFormat generic_format = 1;
  BlockDeviceStat payload = 2;
}
message BlockDeviceStatLog {
  repeated BlockStatSample block_device_stats = 1;
}''',
    "linux_block_2_misc": '''
syntax = "proto3";
message GenericFormat {
  optional string timestamp = 1;
  optional string hostname = 2;
  optional uint32 component = 3;
  optional string tag = 4;
  optional uint32 log_level = 5;
}
message BlkBioQueuePayload {
  optional uint64 read_splits = 6;
  optional uint64 read_4kb = 7;
  optional uint64 write_4kb = 8;
  optional uint64 read_bios = 9;
  optional uint64 write_bios = 10;
}
message BlkBioQueueEvent {
  GenericFormat generic_format = 1;
  BlkBioQueuePayload payload = 2;
}
message BlkBioQueueEvents {
  repeated BlkBioQueueEvent blk_bio_queue_events = 1;
}''',
}

# stem -> (subdir, component, wrapper msg, repeated field, record msg,
#          payload field, record count, split count)
PLACEMENT = {
    "linux_block_1_stats": ("Linux/Block", 1, "BlockDeviceStatLog",
                            "block_device_stats", "BlockStatSample",
                            "payload", 1000, 2),
    "linux_block_2_misc": ("Linux/Block", 1, "BlkBioQueueEvents",
                           "blk_bio_queue_events", "BlkBioQueueEvent",
                           "payload", 400, 1),
}

HOSTNAME = "spark-e97e"
TAG = "test1"


def classes(text: str):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "s.proto").write_text(text)
    inc = Path(grpc_tools.__file__).parent / "_proto"
    out = tmp / "d.desc"
    if protoc.main(["protoc", f"-I{tmp}", f"-I{inc}",
                    f"--descriptor_set_out={out}", "--include_imports",
                    str(tmp / "s.proto")]) != 0:
        raise SystemExit("protoc failed")
    pool = descriptor_pool.DescriptorPool()
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(out.read_bytes())
    for f in fds.file:
        pool.Add(f)

    def cls(name):
        return message_factory.GetMessageClass(pool.FindMessageTypeByName(name))
    return cls


def make_wrapper(cls, wrapper_name, repeated_field, record_name, payload_field,
                 component, n):
    wrapper = cls(wrapper_name)()
    records = getattr(wrapper, repeated_field)
    for i in range(1, n + 1):
        rec = records.add()
        g = rec.generic_format
        g.timestamp = "Mon Jul 27 18:02:21 2026"
        g.hostname = HOSTNAME
        g.component = component
        g.tag = TAG
        g.log_level = 3
        p = getattr(rec, payload_field)
        for f in p.DESCRIPTOR.fields:
            if f.cpp_type == f.CPPTYPE_STRING:
                setattr(p, f.name, "nvme0n1")
            else:
                setattr(p, f.name, (i * 7) % 100000)
    return wrapper


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./fixture_out")
    run_dir = root / "ProfileData-fixture-20260727-180221"
    (run_dir / "Config").mkdir(parents=True, exist_ok=True)
    (run_dir / "Config" / "run_config.log").touch()

    total = 0
    for stem, text in PROTOS.items():
        subdir, comp, wname, rfield, rname, pfield, n, splits = PLACEMENT[stem]
        cls = classes(text)
        d = run_dir / subdir
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.proto").write_text(text)

        # split the n records into `splits` wrapper messages, one per .pb file
        per = (n + splits - 1) // splits
        for idx in range(splits):
            lo, hi = idx * per, min((idx + 1) * per, n)
            if lo >= hi:
                continue
            wrapper = make_wrapper(cls, wname, rfield, rname, pfield, comp, hi - lo)
            name = f"{stem}.pb" if splits == 1 else f"{stem}.{idx:03d}.pb"
            (d / name).write_bytes(wrapper.SerializeToString())
        total += n
        print(f"  {subdir}/{stem}: {n} records, {splits} wrapper file(s)")

    print(f"\n{total} records total -> {run_dir}")


if __name__ == "__main__":
    main()
