"""Varint length-delimited framing — the on-disk record format.

Each record is: [varint byte-length][message bytes]. This matches Go's
protodelim.MarshalTo, so the same framing works for the Profile FW writer.

The decoder yields raw message bytes and detects a truncated tail at a record
boundary, so a partially written .pb file fails loudly rather than silently
producing garbage.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .errors import InputError


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Return (value, new_pos). Raises InputError on a truncated varint."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise InputError("truncated varint (unexpected end of file)")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise InputError("varint too long — wrong framing or corrupt file")


def iter_messages(path: Path) -> Iterator[bytes]:
    """Yield each message's raw bytes from a varint-delimited .pb file.

    Reads the whole file into memory. A .pb file is one split part and is
    expected to be bounded; the run as a whole is streamed by processing one
    part at a time, so total memory stays bounded regardless of run size.
    """
    data = path.read_bytes()
    pos, n = 0, len(data)
    while pos < n:
        length, pos = read_varint(data, pos)
        end = pos + length
        if end > n:
            raise InputError(
                f"{path.name}: truncated record at byte {pos}: header says "
                f"{length} bytes, only {n - pos} remain"
            )
        yield data[pos:end]
        pos = end
