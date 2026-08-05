"""Read records from a .pb file.

Profile FW writes records inside a single wrapper message with a `repeated`
field (e.g. BlockDeviceStatLog { repeated BlockStatSample ... }). Protobuf's
repeated encoding delimits the records internally, so no external framing /
length prefix is used: each .pb file is one complete wrapper message.

We parse the whole file as the wrapper and iterate its repeated field. (If a
producer ever defines no wrapper, we fall back to treating the file as a single
record message.) A decode failure is turned into a clear, forwardable error.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from google.protobuf.message import DecodeError

from .errors import InputError
from .registry import TableSchema


def iter_records(pb_path: Path, schema: TableSchema) -> Iterator:
    """Yield each record message from one .pb wrapper file.

    Reads the whole file into memory and parses it as one message — this is
    inherent to the wrapper format (the file is one message). Profile FW caps
    file size at rotation and splits big runs into several .pb files, so memory
    stays bounded per file.
    """
    data = pb_path.read_bytes()

    if schema.wrapper_cls is not None:
        wrapper = schema.wrapper_cls()
        try:
            wrapper.ParseFromString(data)
        except DecodeError as exc:
            raise InputError(_decode_error(pb_path, schema.wrapper_cls, data, exc)) from None
        yield from getattr(wrapper, schema.repeated_field)
        return

    # no wrapper defined: the whole file is a single record
    rec = schema.record_cls()
    try:
        rec.ParseFromString(data)
    except DecodeError as exc:
        raise InputError(_decode_error(pb_path, schema.record_cls, data, exc)) from None
    yield rec


def _decode_error(pb_path: Path, cls, data: bytes, exc: Exception) -> str:
    return (
        f"{pb_path.name}: could not parse the file as "
        f"{cls.DESCRIPTOR.full_name} ({exc}).\n"
        f"  file size: {len(data)} bytes; first bytes: {data[:32].hex(' ')}\n"
        f"  => the .pb likely does not match its .proto (wrong schema/version), "
        f"or the file is truncated/corrupt. Confirm the .proto with Profile FW."
    )
