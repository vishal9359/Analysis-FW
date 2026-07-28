"""Record framing — how individual protobuf messages are delimited in a .pb file.

Protobuf is not self-delimiting, so a stream of messages needs a framing. The
default is varint length-delimited (Go's protodelim.MarshalTo). Other producers
may differ, so the framing is configurable, and — importantly — this module can
*diagnose* a file whose framing does not match, turning "Wire format was corrupt"
into an actionable message.

Supported framings:
  varint    [varint length][message]   (default; Go protodelim.MarshalTo)
  uint32be  [4-byte big-endian len][message]
  uint32le  [4-byte little-endian len][message]
  single    the whole file is exactly one message (no framing)
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from google.protobuf.message import DecodeError

from .errors import InputError

FRAMINGS = ("varint", "uint32be", "uint32le", "single")


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
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


def _frames(data: bytes, framing: str) -> Iterator[tuple[bytes, int]]:
    """Yield (message_bytes, byte_offset) for the given framing.

    Raises InputError on a structural problem (truncation, impossible length),
    which is distinct from a protobuf DecodeError on the message content.
    """
    n = len(data)
    if framing == "single":
        if n:
            yield data, 0
        return
    pos = 0
    while pos < n:
        start = pos
        if framing == "varint":
            length, pos = read_varint(data, pos)
        elif framing in ("uint32be", "uint32le"):
            if pos + 4 > n:
                raise InputError(f"truncated length prefix at byte {pos}")
            order = "big" if framing == "uint32be" else "little"
            length = int.from_bytes(data[pos:pos + 4], order)
            pos += 4
        else:
            raise InputError(f"unknown framing {framing!r}; expected one of {FRAMINGS}")
        end = pos + length
        if end > n:
            raise InputError(
                f"truncated record at byte {start}: length prefix says {length} "
                f"bytes but only {n - pos} remain"
            )
        yield data[pos:end], start
        pos = end


def iter_messages(path: Path, framing: str = "varint") -> Iterator[tuple[bytes, int]]:
    """Yield (raw message bytes, byte offset) for each record in a .pb file."""
    data = path.read_bytes()
    yield from _frames(data, framing)


# ---------------------------------------------------------------------------
# diagnosis — turn a decode failure into a specific, forwardable explanation
# ---------------------------------------------------------------------------

def diagnose_framing(path: Path, message_cls, sample: int = 8) -> list[dict]:
    """Try every known framing and report how many records each decodes.

    Lets us say "your file is actually uint32be" or "it's one message per file"
    instead of just "corrupt".
    """
    data = path.read_bytes()
    report = []
    for framing in FRAMINGS:
        decoded = tried = 0
        err = None
        try:
            for raw, _ in _frames(data, framing):
                tried += 1
                m = message_cls()
                m.ParseFromString(raw)
                # A zero-length record decodes to an all-default message without
                # error — which is exactly what a WRONG framing produces (e.g.
                # uint32be data read as varint: the 0x00 length byte yields empty
                # records). Only a non-empty message counts as a real decode, so
                # the probe is not fooled into calling a wrong framing "correct".
                if not raw or m.ByteSize() == 0:
                    err = "produced empty messages (framing splits at wrong boundaries)"
                    break
                decoded += 1
                if tried >= sample:
                    break
        except (InputError, DecodeError) as exc:
            err = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # pragma: no cover
            err = f"{type(exc).__name__}: {exc}"
        report.append({"framing": framing, "decoded": decoded,
                       "tried": tried, "error": err})
    return report


def _best_framing(report: list[dict], configured: str) -> str | None:
    """The framing (other than the configured one) that decoded the most."""
    ranked = sorted(report, key=lambda r: r["decoded"], reverse=True)
    for r in ranked:
        if r["decoded"] > 0 and r["framing"] != configured:
            return r["framing"]
    return None


def describe_failure(path: Path, message_cls, framing: str, record_index: int,
                     underlying: str, raw: bytes | None = None) -> str:
    """Build an actionable message for any read failure — whether it came from
    the framing layer (implausible length) or the protobuf parser (corrupt
    content). Both usually mean the same thing: wrong framing or wrong schema.
    """
    full = message_cls.DESCRIPTOR.full_name
    lines = [
        f"{path.name}: could not read record {record_index} as {full} "
        f"(framing={framing}): {underlying}"
    ]
    if raw is not None:
        lines.append(f"  first bytes of record: {raw[:32].hex(' ')}")
    else:
        lines.append(f"  first bytes of file:   {path.read_bytes()[:32].hex(' ')}")

    if record_index <= 1:
        # A first-record failure is almost always systemic (framing/schema),
        # not a single bad record. Probe every framing and say what fits.
        report = diagnose_framing(path, message_cls)
        alt = _best_framing(report, framing)
        summary = ", ".join(f"{r['framing']}={r['decoded']}/{r['tried']}"
                            for r in report)
        lines.append(f"  framing probe (records decoded): {summary}")
        if alt == "single":
            lines.append("  => the file looks like ONE un-framed message. If each "
                         ".pb holds a single message, set input.framing: single; "
                         "otherwise the writer is not length-delimiting records.")
        elif alt:
            lines.append(f"  => the file looks like '{alt}' framing, not '{framing}'. "
                         f"Set input.framing: {alt}, or ask Profile FW to write "
                         f"varint length-delimited (Go protodelim.MarshalTo).")
        else:
            lines.append("  => no known framing decodes this as the expected "
                         "message. Likely the .proto does not match the writer's "
                         "schema, or the data is compressed/encrypted/corrupt. "
                         "Confirm the .proto version with Profile FW.")
    else:
        lines.append(f"  => {record_index - 1} earlier record(s) read fine, so this "
                     "looks like a single corrupt record, not a framing problem.")
    return "\n".join(lines)
