# 0002 — Derive the DB schema from the producer's `.proto` at runtime

**Status:** Accepted.

## Context

Profile FW's payload schema evolves (new fields, new layers, new producers), and
Analysis FW must **not** need a code change every time. The payload schema is
Profile FW's concern, not ours.

## Decision

Compile the producer's `.proto` **at runtime** (`protoc` → `FileDescriptorSet` →
`DescriptorPool` → dynamic message classes) and **derive the ClickHouse table +
columns from the descriptor**. No generated protobuf classes are a build dependency.
Detection is by **structure and field name**, not by message name (see
[ADR-0003](0003-pb-wrapper-generic-format.md)).

## Consequences

- A new payload field / new layer / new producer needs **no code change** — the loader
  reads the shipped `.proto` and adapts. (A new field on an *existing* table still
  needs a one-time `ALTER TABLE ADD COLUMN`; auto-migrate is deferred.)
- Implementation notes that must not be lost:
  - protobuf 7.x: use `FieldDescriptor.is_repeated`, **not** `.label`;
    `UnknownFields()` is unavailable on upb dynamic classes.
  - Descriptor **bytes** are picklable (safe to send to worker processes); message
    **classes** are not — so detection is rebuilt from bytes in each worker.
  - Each `.proto` is compiled in its **own** descriptor pool, so identical message
    names across producers never collide.
- The derivation lives in one place (`src/registry.py`) and is unit-tested against the
  real bundled protos.
