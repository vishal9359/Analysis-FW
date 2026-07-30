"""Schema registry: compile a producer's .proto and derive its table — generically.

Each .proto defines, by convention:
  - GenericFormat   : the common header fields (timestamp, hostname, ...).
  - a payload msg   : the layer-specific fields.
  - a record msg    : { GenericFormat generic_format = 1; <Payload> <name> = 2; }
                      -- one row of data. Identified by having a GenericFormat field.
  - a wrapper msg   : { repeated <record> <name> = 1; }
                      -- what a .pb file actually contains (no delimiters needed;
                      the repeated field structures the records internally).

The loader identifies these by STRUCTURE, not by field names, so a new field, a
renamed payload field, or a new layer needs no code change. The .proto is
compiled at runtime; no generated classes are a build dependency.
"""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

import grpc_tools
from grpc_tools import protoc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.descriptor import FieldDescriptor as FD

from .errors import SchemaError

# Agreed contract with Profile FW: the record message has a singular message
# field named `generic_format` (the header, of type GenericFormat) and a
# singular message field named `payload` (the layer data). Both names are
# required; a .proto that does not follow this is rejected.
GENERIC_FIELD = "generic_format"
PAYLOAD_FIELD = "payload"
GENERIC_MESSAGE = "GenericFormat"

# protobuf field type -> ClickHouse type
_PROTO_TO_CH = {
    FD.TYPE_STRING: "String", FD.TYPE_BYTES: "String", FD.TYPE_BOOL: "Bool",
    FD.TYPE_INT32: "Int32", FD.TYPE_SINT32: "Int32", FD.TYPE_SFIXED32: "Int32",
    FD.TYPE_INT64: "Int64", FD.TYPE_SINT64: "Int64", FD.TYPE_SFIXED64: "Int64",
    FD.TYPE_UINT32: "UInt32", FD.TYPE_FIXED32: "UInt32",
    FD.TYPE_UINT64: "UInt64", FD.TYPE_FIXED64: "UInt64",
    FD.TYPE_FLOAT: "Float32", FD.TYPE_DOUBLE: "Float64", FD.TYPE_ENUM: "Int32",
}

# Loader-added bookkeeping columns (not from the proto).
LEADING_COLUMNS = [("run_id", "String"), ("ts", "DateTime")]
TRAILING_COLUMNS = [("_loaded_at", "DateTime")]


@dataclass(frozen=True)
class Column:
    name: str
    ch_type: str


@dataclass
class TableSchema:
    stem: str                      # == table name
    record_cls: type               # the per-row message (e.g. BlockStatSample)
    wrapper_cls: type | None       # the repeated wrapper (e.g. BlockDeviceStatLog)
    repeated_field: str | None     # the repeated field name inside the wrapper
    generic_field: str             # the GenericFormat field name on the record
    payload_field: str             # the payload field name on the record
    generic_fields: list[str]      # header field names, in column order
    payload_fields: list[str]      # payload field names, in column order
    columns: list[Column]          # full table columns, in row order
    descriptor_bytes: bytes        # picklable, for worker processes
    schema_version: str


def _scalar_columns(msg_desc, source: str) -> tuple[list[str], list[Column]]:
    names, cols = [], []
    for f in msg_desc.fields:
        if f.is_repeated:
            raise SchemaError(f"{source}: {msg_desc.name}.{f.name} is repeated "
                              f"(payload/header fields must be scalars)")
        if f.type == FD.TYPE_MESSAGE:
            raise SchemaError(f"{source}: {msg_desc.name}.{f.name} is a nested "
                              f"message (payload/header fields must be scalars)")
        ch = _PROTO_TO_CH.get(f.type)
        if ch is None:
            raise SchemaError(f"{source}: {msg_desc.name}.{f.name} has unsupported "
                              f"protobuf type {f.type}")
        names.append(f.name)
        cols.append(Column(f.name, ch))
    return names, cols


def _detect(pool, primary_file, source: str):
    """Find the record message and, if present, the wrapper message.

    The record message is identified by the agreed field NAMES: it has a
    singular message field named `generic_format` (header) and a singular
    message field named `payload` (data). Both are required; anything else is
    a contract violation and is rejected with a clear error.
    """
    messages = {m.name: pool.FindMessageTypeByName(
        f"{primary_file.package + '.' if primary_file.package else ''}{m.name}")
        for m in primary_file.message_type}

    record_desc = generic_field = payload_field = None
    for desc in messages.values():
        by_name = {f.name: f for f in desc.fields}
        g = by_name.get(GENERIC_FIELD)
        if g is None:
            continue   # not a record message

        # this message carries `generic_format`, so it must be a valid record
        if g.type != FD.TYPE_MESSAGE or g.is_repeated:
            raise SchemaError(f"{source}: {desc.name}.{GENERIC_FIELD} must be a "
                              f"singular message field")
        if g.message_type.name != GENERIC_MESSAGE:
            raise SchemaError(f"{source}: {desc.name}.{GENERIC_FIELD} must be of "
                              f"type {GENERIC_MESSAGE}, got {g.message_type.name}")
        p = by_name.get(PAYLOAD_FIELD)
        if p is None:
            raise SchemaError(
                f"{source}: record message {desc.name} has '{GENERIC_FIELD}' but "
                f"no '{PAYLOAD_FIELD}' field. The contract requires both a "
                f"'{GENERIC_FIELD}' and a '{PAYLOAD_FIELD}' message field.")
        if p.type != FD.TYPE_MESSAGE or p.is_repeated:
            raise SchemaError(f"{source}: {desc.name}.{PAYLOAD_FIELD} must be a "
                              f"singular message field")
        if record_desc is not None:
            raise SchemaError(
                f"{source}: more than one record message (both {record_desc.name} "
                f"and {desc.name} have a '{GENERIC_FIELD}' field); expected one")
        record_desc, generic_field, payload_field = desc, g, p

    if record_desc is None:
        raise SchemaError(
            f"{source}: no record message found. The contract requires a message "
            f"with a '{GENERIC_FIELD}' field (header) and a '{PAYLOAD_FIELD}' "
            f"field (data).")

    # wrapper = a message with a `repeated <record>` field
    wrapper_desc = repeated_field = None
    for desc in messages.values():
        for f in desc.fields:
            if (f.is_repeated and f.type == FD.TYPE_MESSAGE
                    and f.message_type.name == record_desc.name):
                wrapper_desc, repeated_field = desc, f.name
                break
        if wrapper_desc is not None:
            break

    return record_desc, generic_field, payload_field, wrapper_desc, repeated_field


def build_schema(stem: str, proto_path: Path) -> TableSchema:
    inc = Path(grpc_tools.__file__).parent / "_proto"
    out = Path(tempfile.mkdtemp()) / "d.desc"
    rc = protoc.main(["protoc", f"-I{proto_path.parent}", f"-I{inc}",
                      f"--descriptor_set_out={out}", "--include_imports",
                      str(proto_path)])
    if rc != 0:
        raise SchemaError(f"{proto_path.name}: protoc failed to compile")
    return build_schema_from_descriptor(stem, out.read_bytes(), source=proto_path.name)


def build_schema_from_descriptor(stem: str, descriptor_bytes: bytes,
                                 source: str = "<descriptor>") -> TableSchema:
    """Derive the table schema from compiled descriptor bytes. Used by both the
    parent (from a .proto file) and worker child processes (bytes are picklable;
    classes are not), so detection rules live in exactly one place."""
    pool = descriptor_pool.DescriptorPool()
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(descriptor_bytes)
    for f in fds.file:
        pool.Add(f)
    primary = next((f for f in fds.file if Path(f.name).name == source), fds.file[-1])

    rec_desc, gfield, pfield, wrap_desc, rep_field = _detect(pool, primary, source)

    record_cls = message_factory.GetMessageClass(rec_desc)
    wrapper_cls = (message_factory.GetMessageClass(wrap_desc)
                   if wrap_desc is not None else None)

    generic_names, generic_cols = _scalar_columns(gfield.message_type, source)
    payload_names, payload_cols = _scalar_columns(pfield.message_type, source)

    columns = ([Column(n, t) for n, t in LEADING_COLUMNS]
               + generic_cols + payload_cols
               + [Column(n, t) for n, t in TRAILING_COLUMNS])

    # guard against a name clash between a generic and a payload field
    seen = set()
    for c in columns:
        if c.name in seen:
            raise SchemaError(f"{source}: duplicate column '{c.name}' "
                              f"(a generic and a payload field share a name)")
        seen.add(c.name)

    return TableSchema(
        stem=stem, record_cls=record_cls, wrapper_cls=wrapper_cls,
        repeated_field=rep_field, generic_field=gfield.name,
        payload_field=pfield.name, generic_fields=generic_names,
        payload_fields=payload_names, columns=columns,
        descriptor_bytes=descriptor_bytes,
        schema_version="sha256:" + hashlib.sha256(descriptor_bytes).hexdigest()[:16],
    )


def create_table_ddl(database: str, schema: TableSchema) -> str:
    cols = ",\n    ".join(f"`{c.name}` {c.ch_type}" for c in schema.columns)
    return (
        f"CREATE TABLE IF NOT EXISTS `{database}`.`{schema.stem}`\n"
        f"(\n    {cols}\n)\n"
        f"ENGINE = MergeTree\n"
        f"PARTITION BY run_id\n"
        f"ORDER BY (run_id, hostname, ts)"
    )
