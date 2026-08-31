# Architecture — current state and production target

*The "how". Charter/why is in [overview.md](overview.md); the phase plan is in
[roadmap.md](roadmap.md); design decisions are in [decisions/](decisions/).*

## End-to-end data flow (the whole picture)

```
 SUT (system under test)                 Analysis FW (this repo)          Consumers
 ───────────────────────                 ───────────────────────          ─────────
 Profile FW  ──writes──►  profiling data  ──►  ingest  ──►  ClickHouse  ──►  Grafana
 (eBPF, on the SUT)       (per-IO events                    (typed tables)    (dashboards)
                          + telemetry)                                         Python / SQL
                                                                              (ad-hoc analysis)
```

Analysis FW owns everything right of the SUT. The seam with Profile FW is the
**data-format + folder contract** (below).

## Two ingest modes — and which one is "now"

| Mode | When | Status |
|---|---|---|
| **Offline batch loader** (Module 1) | POC 1 / MVP | **Built — this repo today.** Also the permanent replay/backfill/repair path. |
| **Live streaming** | Production / POC 2 | **Design decided, not yet built.** See "Production target" below. |

Offline files were an explicit **POC-1 temporary mechanism** — do not treat them as
the production model. Streaming is the target; the offline loader stays on as the
reprocess-from-raw path.

## Current: the offline loader (Module 1)

Given a `ProfileData-<tag>-<timestamp>` run directory, it decodes every record and
writes it to typed ClickHouse tables — completely, correctly, repeatably. The schema
is **derived from the producer's `.proto` at runtime**, so new fields / layers /
producers need no code change.

Pipeline (each stage a module under `src/`; full code map is in the
[README](../README.md)):

```
discover → registry → reader → worker → store
```
- **discover** — walk the run dir, pair each `<stem>.proto` with its `.pb` part(s).
- **registry** — compile the `.proto` at runtime → derive ClickHouse table(s)+columns.
- **reader** — parse each `.pb` (one wrapper message), iterate records.
- **worker** — flatten a record → row(s): scalars and 1:1 groups → main table,
  repeated → child tables (leaf rows carry their group's scalars).
- **store** — create tables if absent; batched INSERT (ClickHouse; in-memory for tests).

Design rationale is in [design.md](design.md); the block-metric queries in
[block-metrics.md](block-metrics.md); timestamp handling in
[timestamp-format.md](timestamp-format.md).

### The input contract (the Profile FW ↔ Analysis FW seam)

- A run is a directory `ProfileData-<tag>-<timestamp>/` with `Config/` plus
  `Linux/<layer>/<layer>_<type>.proto` + `.pb` files.
- **Each `.pb` file is one `StatLogs` wrapper** holding a `repeated StatLog` list —
  protobuf's repeated encoding delimits records, so there is **no length-prefix
  framing**. A big run is split into several `.pb` files, each a complete wrapper.
- A **record** = a message with a `generic_format` field (header: timestamp,
  hostname, component, tag, log_level) and a `payload` field (layer data). The loader
  finds record/header/payload/wrapper **by structure and field name, not by message
  name** — so protos may reuse `StatLog`/`Payload` names freely.
- Payload fields map to tables **by shape**: a singular sub-message (e.g. syscall
  `io_flags`) is flattened onto the main row with prefixed columns; a
  `repeated <Message>` (e.g. NVMe `per_queue`, `per_core`) becomes a **child table**
  linked by `record_id`; a repeated message *inside* a repeated message (e.g.
  `per_core_seq_random[].entries[]`) becomes **one leaf table** with the outer level's
  scalars denormalized onto every row. Full rules:
  [decisions/0010-payload-field-shapes-to-tables.md](decisions/0010-payload-field-shapes-to-tables.md).

Full format notes: [reference/requirements/profile-log-format-details.txt](reference/requirements/profile-log-format-details.txt).

### What lands in ClickHouse

- **One database per producer**, **one table per `<layer>_<type>`**.
- `ENGINE = MergeTree`, `PARTITION BY run_id`, `ORDER BY (run_id, hostname, ts)`.
- Metrics like IOPS / bandwidth / latency are **derived at query time** from the
  cumulative counters (see [block-metrics.md](block-metrics.md)).

### Known MVP limitations (deliberate, deferred)

- **Append-only** — re-loading a run duplicates rows (drop-partition reload deferred;
  the partition key is ready for it). See [ADR-0006](decisions/0006-mvp-append-only-ingest.md).
- **`run_id`/`record_id` are single-node schemes** — multi-node-safe IDs deferred.
  See [ADR-0005](decisions/0005-uint64-ids-stopgap.md).
- **Schema evolution** — adding a column to an existing table needs a one-time
  `ALTER TABLE ADD COLUMN` (auto-migrate deferred).

## Production target (POC 2) — live streaming, never-block

The hard problem POC 1 deferred. Steady state is **memory-only, zero disk writes**;
disk is only an overflow tier.

```
ring buffer → consumer (pinned core, drain-only) → shipper (cgroup-capped:
  batch+encode+compress) → gateway → ClickHouse
                       └─ overflow-spill to local scratch disk (NOT the SUT's SSD)
```

- **Two lanes:** (1) fast lane — in-kernel aggregated metrics (~1–5 s fresh, powers
  live Grafana); (2) bulk lane — raw per-IO events trailing ~30–60 s. The live view
  never waits on the firehose.
- **Tiered buffering:** RAM queue in steady state; spill to disk only when it passes a
  threshold (gateway slow/outage). Backpressure grows the spill — it **never** reaches
  back to the ring buffer (costs freshness, never safety).
- **No-loss accounting:** drop counters at every stage + automated per-run
  reconciliation (events emitted vs rows in DB).
- **Transport ladder** (preference order): Vector agent → ClickHouse (default);
  custom gRPC batch → stateless gateway → ClickHouse (escape hatch); Kafka (last
  resort). See [ADR-0007](decisions/0007-streaming-transport-ladder.md).

## Database

**ClickHouse (OSS, Apache-2.0), self-hosted** — chosen for high-cardinality trace
ingestion, cross-layer JOINs, ad-hoc queries, and free horizontal scaling. Full
reasoning, the auto-rebalance caveat, and revisit triggers are in
[ADR-0001](decisions/0001-database-clickhouse.md) and the DB-research snapshots under
[reference/db-research/](reference/db-research/).
