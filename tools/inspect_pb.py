#!/usr/bin/env python3
"""
Diagnose a .pb file: which framing does it use, and does it match its .proto?

Use this when a load fails with a decode error, or to hand the Profile FW team a
concrete answer about how their file is written.

    python tools/inspect_pb.py <file.pb> [--proto <file.proto>] [--message <Name>]

If --proto is omitted, it looks for a .proto with the same stem beside the .pb.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

# <stem>.pb or <stem>.<NNN>.pb  ->  the .proto is <stem>.proto
_STEM_RE = re.compile(r"^(?P<stem>.+?)(?:\.\d+)?\.pb$")


def proto_for(pb: Path) -> Path:
    m = _STEM_RE.match(pb.name)
    stem = m.group("stem") if m else pb.stem
    return pb.parent / f"{stem}.proto"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import grpc_tools
from grpc_tools import protoc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from analysis_fw.framing import FRAMINGS, diagnose_framing


def load_message_class(proto_path: Path, message: str | None):
    inc = Path(grpc_tools.__file__).parent / "_proto"
    out = Path(tempfile.mkdtemp()) / "d.desc"
    rc = protoc.main(["protoc", f"-I{proto_path.parent}", f"-I{inc}",
                      f"--descriptor_set_out={out}", "--include_imports",
                      str(proto_path)])
    if rc != 0:
        sys.exit(f"protoc failed to compile {proto_path}")
    pool = descriptor_pool.DescriptorPool()
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(out.read_bytes())
    for f in fds.file:
        pool.Add(f)
    primary = next((f for f in fds.file
                    if Path(f.name).name == proto_path.name), fds.file[-1])
    msgs = [m.name for m in primary.message_type]
    if not msgs:
        sys.exit(f"{proto_path.name} defines no messages")
    name = message or msgs[0]
    if message is None and len(msgs) > 1:
        print(f"note: {proto_path.name} has several messages {msgs}; "
              f"using {name!r} (override with --message)")
    pkg = primary.package
    full = f"{pkg + '.' if pkg else ''}{name}"
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(full)), full


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose a .pb file's framing.")
    ap.add_argument("pb", type=Path)
    ap.add_argument("--proto", type=Path)
    ap.add_argument("--message", help="message name if the .proto has several")
    args = ap.parse_args()

    if not args.pb.is_file():
        sys.exit(f"not a file: {args.pb}")
    proto = args.proto or proto_for(args.pb)
    if not proto.is_file():
        sys.exit(f"no .proto found (looked for {proto}); pass --proto")

    data = args.pb.read_bytes()
    print(f"file      : {args.pb.name}  ({len(data):,} bytes)")
    print(f"proto     : {proto.name}")
    print(f"first 48B : {data[:48].hex(' ')}")

    cls, full = load_message_class(proto, args.message)
    print(f"message   : {full}\n")

    report = diagnose_framing(args.pb, cls, sample=20)
    print(f"{'framing':<10} {'decoded/tried':<14} note")
    print("-" * 60)
    best = None
    for r in report:
        note = r["error"] or "OK"
        if r["decoded"] > 0 and (best is None or r["decoded"] > best["decoded"]):
            best = r
        ratio = f"{r['decoded']}/{r['tried']}"
        print(f"{r['framing']:<10} {ratio:<14} {note[:44]}")

    print()
    if best and best["decoded"] == best["tried"] and best["tried"] > 1:
        print(f"=> This file is '{best['framing']}' framed. "
              f"Set input.framing: {best['framing']} in config.yaml.")
    elif best and best["framing"] == "single" and best["decoded"] == 1:
        print("=> This file appears to be ONE un-framed message "
              "(input.framing: single). If it should hold many records, the "
              "writer is not length-delimiting them.")
    elif best:
        print(f"=> Partial match under '{best['framing']}' "
              f"({best['decoded']}/{best['tried']}). The .proto may not fully "
              "match the writer's schema — confirm the version with Profile FW.")
    else:
        print("=> No known framing decodes this as the expected message. "
              "Likely wrong .proto, or the data is compressed/encrypted. "
              "Confirm the schema and encoding with Profile FW.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
