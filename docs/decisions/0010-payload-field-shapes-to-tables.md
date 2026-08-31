# 0010 — Payload field shapes map to tables by structure

**Status:** Accepted.

> Numbered 0010, not 0008: `0008` (database seam) and `0009` (Go implementation) are
> already taken on the `query` and `go_version` branches. Reserving those numbers keeps
> the ADR log unambiguous when the branches converge.

## Context

The loader derives its schema from the producer's `.proto` at runtime
([0002](0002-runtime-schema-from-proto.md)) and must never hardcode field names. So
"what table does this field go in?" has to be answered from the **shape** of the field
alone — its cardinality relative to the record.

Two producer changes forced the rules to be stated explicitly:

- Syscall profiling added `IoFlags` — a **singular** sub-message, exactly one per
  record. The loader had no rule for it and failed with
  *"payload.io_flags is a singular nested message"*.
- LBA-randomness profiling added `per_core_seq_random[].entries[]` — a **repeated
  message inside a repeated message**, i.e. two levels of fan-out. The loader failed
  with *"NvmeCoreSeqRandom.entries is repeated"*.

Both are legitimate profiling shapes, not producer mistakes.

## Decision

Map each payload field by its cardinality against the parent record:

| Shape | Cardinality | Becomes |
|---|---|---|
| scalar | 1:1 | a column on the main row |
| singular message | 1:1 | **flattened** onto the main row, columns prefixed `<field>_<leaf>` (`io_flags.direct` → `io_flags_direct`), recursing through further 1:1 groups |
| repeated message | 1:N | a **child table**, one row per element, linked by `record_id` |
| repeated inside repeated | 1:N:M | **one table of the LEAF elements**; each ancestor level's scalars are denormalized onto every leaf row |
| repeated scalar | — | **rejected** (`SchemaError`) |

A repeated message that itself contains a repeated message is a **grouping level, not a
table**. `per_core_seq_random[].entries[]` yields exactly one table,
`<stem>_per_core_seq_random_entries`, whose rows carry the group's `cpu_id` and `dir`
alongside the entry's own fields. Recursion handles any depth, guarded by
`MAX_NEST_DEPTH = 8` against cyclic message graphs.

Child `ORDER BY` is `(run_id, hostname, <first scalar of the outermost level>, ts)` — by
convention the producer puts the key dimension first (`queue_id`, `cpu_id`), so one
core's measurements sit together on disk.

## Consequences

- **One measurement is one row.** No JOIN is needed to know which core/direction a
  window belongs to — the common query (`WHERE cpu_id = 3 AND dir = 1 ORDER BY
  start_time`) reads one table.
- **Row counts are additive, never multiplicative.** 12 cores × {read, write} with 1–3
  windows each produces the *sum* of the windows (e.g. 23 rows), not cores × windows.
- The ancestor columns repeat on every leaf row. This is deliberate: ClickHouse is
  columnar and `cpu_id`/`dir` are low-cardinality and sorted, so they compress to
  almost nothing. Denormalization costs storage that the engine largely reclaims and
  buys the JOIN-free query.
- **Ordering is the producer's job, not the file's.** ClickHouse does not preserve
  insertion order, so proto element order is not recoverable from the table. Queries
  must `ORDER BY start_time` (with `record_id` to disambiguate across records). The
  producer must therefore emit a usable ordering key on every leaf element — for
  `NvmeSeqRandomEntry` that is `start_time`.
- Intermediate levels get **no table of their own**. A query that wants per-group
  aggregates computes them with `GROUP BY cpu_id, dir` over the leaf table. If a group
  ever carries a fact that is not derivable from its leaves, that is the trigger to
  revisit this and emit a per-level table too.
- Adding a nesting level to a producer `.proto` **adds a table**; the old table name
  (`<stem>_per_core_seq_random`) is not what gets created. Renaming or re-nesting a
  repeated field is a breaking schema change ([0006](0006-mvp-append-only-ingest.md):
  no auto-migrate yet).
