# 0006 — MVP ingest is append-only

**Status:** Accepted for the MVP — to be revisited with the coordinator.

## Context

The MVP loader writes rows into ClickHouse with no delete/replace logic. A clean
reload (drop-and-replace of a run) needs a coordinator that the MVP intentionally does
not include.

## Decision

Keep the MVP **append-only**. Re-loading the same run **duplicates** its rows. Tables
are `PARTITION BY run_id`, which makes a future drop-and-replace cheap (drop the run's
partition, then reload).

## Consequences

- Operationally: don't re-load a run unless you first drop its partition, or you'll get
  duplicates. Per-run reconciliation (records read vs rows written) catches load
  failures but not accidental double-loads.
- **Deferred:** a reload/coordinator path (`DROP PARTITION` → reload) and, with it,
  idempotent loads. Part of POC 2 schema/storage maturity ([../roadmap.md](../roadmap.md),
  activity D). The partition key is already chosen to support it.
