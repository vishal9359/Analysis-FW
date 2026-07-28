"""Schema registry: compile a unit's .proto and derive its table.

Each .proto defines one message following the format contract:
  - header scalar fields 1-5 (timestamp, hostname, component, tag, loglevel)
  - a nested Payload message in field 6, whose fields are scalars

From that we derive the ClickHouse columns and CREATE TABLE, and keep the
message class for decoding. Nothing about the schema is written in code — it is
all read from the .proto at runtime, so a new field / new type / new producer
needs no code change.

Each .proto is compiled into its own descriptor pool, so package/message-name
reuse across different types cannot collide.
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

HEADER_FIELDS = ("timestamp", "hostname", "component", "tag", "loglevel")
PAYLOAD_FIELD = "payload"

# protobuf field type -> ClickHouse type
_PROTO_TO_CH = {
    FD.TYPE_STRING: "String",
    FD.TYPE_BYTES: "String",
    FD.TYPE_BOOL: "Bool",
    FD.TYPE_INT32: "Int32", FD.TYPE_SINT32: "Int32", FD.TYPE_SFIXED32: "Int32",
    FD.TYPE_INT64: "Int64", FD.TYPE_SINT64: "Int64", FD.TYPE_SFIXED64: "Int64",
    FD.TYPE_UINT32: "UInt32", FD.TYPE_FIXED32: "UInt32",
    FD.TYPE_UINT64: "UInt64", FD.TYPE_FIXED64: "UInt64",
    FD.TYPE_FLOAT: "Float32",
    FD.TYPE_DOUBLE: "Float64",
    FD.TYPE_ENUM: "Int32",
}

# Fixed header columns, added to every table. Order matters — worker rows follow it.
HEADER_COLUMNS = [
    ("run_id", "String"),
    ("sut_id", "String"),
    ("ts", "DateTime"),
    ("tag", "UInt32"),
    ("loglevel", "UInt8"),
    ("component", "UInt8"),
    ("_loaded_at", "DateTime"),
]


@dataclass(frozen=True)
class Column:
    name: str
    ch_type: str


@dataclass
class TableSchema:
    stem: str                 # == table name
    message_cls: type
    payload_fields: list[str]  # payload scalar field names, in column order
    columns: list[Column]      # header columns + payload columns, in order
    descriptor_bytes: bytes    # serialized FileDescriptorSet (picklable for workers)
    message_full_name: str
    schema_version: str        # sha256 of descriptor_bytes


def _compile(proto_path: Path) -> tuple[bytes, list]:
    """Compile one .proto into a FileDescriptorSet (bytes + parsed files)."""
    inc = Path(grpc_tools.__file__).parent / "_proto"
    out = Path(tempfile.mkdtemp()) / "d.desc"
    rc = protoc.main([
        "protoc", f"-I{proto_path.parent}", f"-I{inc}",
        f"--descriptor_set_out={out}", "--include_imports", str(proto_path),
    ])
    if rc != 0:
        raise SchemaError(f"{proto_path.name}: protoc failed to compile")
    raw = out.read_bytes()
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(raw)
    return raw, list(fds.file)


def _message_class_from_bytes(descriptor_bytes: bytes, full_name: str):
    """Rebuild a message class from descriptor bytes in an isolated pool.

    Used both here and inside worker processes (bytes are picklable; message
    classes are not).
    """
    pool = descriptor_pool.DescriptorPool()
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(descriptor_bytes)
    for f in fds.file:
        pool.Add(f)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(full_name))


def _pick_message(files, primary_name: str) -> str:
    """The unit's .proto must define exactly one top-level message (the record).

    `files` may include imported protos (e.g. a shared header); the primary is
    the one whose filename matches, so imports don't confuse message selection.
    """
    primary = next((f for f in files if Path(f.name).name == primary_name), files[-1])
    msgs = list(primary.message_type)
    if len(msgs) != 1:
        raise SchemaError(
            f"{primary_name}: expected exactly one top-level message, found "
            f"{[m.name for m in msgs]}"
        )
    pkg = primary.package
    return f"{pkg + '.' if pkg else ''}{msgs[0].name}"


def build_schema(stem: str, proto_path: Path) -> TableSchema:
    """Compile a .proto file and derive its table schema."""
    descriptor_bytes, files = _compile(proto_path)
    full_name = _pick_message(files, proto_path.name)
    return build_schema_from_descriptor(stem, descriptor_bytes, full_name,
                                        source=proto_path.name)


def build_schema_from_descriptor(stem: str, descriptor_bytes: bytes,
                                 message_full_name: str,
                                 source: str = "<descriptor>") -> TableSchema:
    """Derive a table schema from already-compiled descriptor bytes.

    Both the parent (via build_schema) and worker child processes use this, so
    the header/payload/column rules exist in exactly one place.
    """
    full_name = message_full_name
    cls = _message_class_from_bytes(descriptor_bytes, full_name)
    desc = cls.DESCRIPTOR

    # validate the mandated header
    by_name = {f.name: f for f in desc.fields}
    missing = [h for h in HEADER_FIELDS if h not in by_name]
    if missing:
        raise SchemaError(
            f"{source}: message '{desc.name}' missing contract header "
            f"field(s): {missing}"
        )

    # locate the payload (nested message in field 6 / named 'payload')
    if PAYLOAD_FIELD not in by_name:
        raise SchemaError(f"{source}: message has no '{PAYLOAD_FIELD}' field")
    payload_fd = by_name[PAYLOAD_FIELD]
    if payload_fd.message_type is None:
        raise SchemaError(f"{source}: '{PAYLOAD_FIELD}' must be a message")

    # derive payload columns — scalars only
    payload_fields: list[str] = []
    payload_cols: list[Column] = []
    for f in payload_fd.message_type.fields:
        if f.is_repeated:
            raise SchemaError(f"{source}: payload.{f.name} is repeated "
                              f"(payloads must be scalars only)")
        if f.type == FD.TYPE_MESSAGE:
            raise SchemaError(f"{source}: payload.{f.name} is a nested "
                              f"message (payloads must be scalars only)")
        ch = _PROTO_TO_CH.get(f.type)
        if ch is None:
            raise SchemaError(f"{source}: payload.{f.name} has unsupported "
                              f"protobuf type {f.type}")
        payload_fields.append(f.name)
        payload_cols.append(Column(f.name, ch))

    columns = [Column(n, t) for n, t in HEADER_COLUMNS] + payload_cols
    return TableSchema(
        stem=stem,
        message_cls=cls,
        payload_fields=payload_fields,
        columns=columns,
        descriptor_bytes=descriptor_bytes,
        message_full_name=full_name,
        schema_version="sha256:" + hashlib.sha256(descriptor_bytes).hexdigest()[:16],
    )


def create_table_ddl(database: str, schema: TableSchema) -> str:
    cols = ",\n    ".join(f"`{c.name}` {c.ch_type}" for c in schema.columns)
    return (
        f"CREATE TABLE IF NOT EXISTS `{database}`.`{schema.stem}`\n"
        f"(\n    {cols}\n)\n"
        f"ENGINE = MergeTree\n"
        f"PARTITION BY (run_id, sut_id)\n"
        f"ORDER BY (run_id, sut_id, ts)"
    )
