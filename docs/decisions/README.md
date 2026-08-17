# Decision records (ADRs)

Each file captures **one decision**: the context, what was decided, and the
consequences. ADRs are **append-only** — when a decision changes, add a new ADR that
supersedes the old one (mark the old one `Superseded by NNNN`) rather than rewriting
history. This is how "why is it like this?" stays answerable as the project grows.

| # | Decision | Status |
|---|---|---|
| [0001](0001-database-clickhouse.md) | ClickHouse (OSS) as the database | Accepted — revisit after POC 1 |
| [0002](0002-runtime-schema-from-proto.md) | Derive the DB schema from the producer's `.proto` at runtime | Accepted |
| [0003](0003-pb-wrapper-generic-format.md) | The `.pb` wrapper + `generic_format`/`payload` format contract | Accepted |
| [0004](0004-src-layout-and-external-config.md) | `src/` layout, editable `config/config.yaml`, no packaging | Accepted |
| [0005](0005-uint64-ids-stopgap.md) | `UInt64` sequence `run_id`/`record_id` as an MVP stopgap | Accepted — revisit for multi-node |
| [0006](0006-mvp-append-only-ingest.md) | MVP ingest is append-only | Accepted — revisit with the coordinator |
| [0007](0007-streaming-transport-ladder.md) | Streaming transport ladder (Vector → gRPC gateway → Kafka) | Accepted direction — build in POC 2 |
| [0008](0008-database-seam.md) | Database seam: neutral schema types, adapter owns the SQL dialect | Accepted |
| [0009](0009-go-implementation.md) | Go implementation of Module 1 (replaces Python) | Accepted |

## Adding an ADR

Copy the shape of an existing one: **Status · Context · Decision · Consequences · References**.
Number it next in sequence, add a row above, keep it short. If it changes an existing
decision, set that one's status to `Superseded by NNNN`.
