# Glossary — domain terms

*One place for the vocabulary. If a term here names a file/field/flag, verify it
still exists in the code before relying on it.*

## Project / system

- **System Analysis Framework** — the umbrella framework (profile + analyze SSD/host
  overheads for AI workloads). See [overview.md](overview.md).
- **Analysis FW** — this project: ingest → DB → queries → UI. Owns everything from the
  SUT outward.
- **Profile FW** — sibling framework that collects data on the SUT (eBPF) and writes
  the `.proto`/`.pb` files Analysis FW consumes.
- **Use-case Run & Deploy FW** — sibling framework that deploys/configures the SUT and
  runs the workloads.
- **SUT** — System Under Test: the host+SSD being profiled.
- **Controller node** — the node that runs Analysis FW, reaches out to SUTs, hosts the DB.
- **DGX Spark** — the starting target platform (ARM/Grace, GPU desktop). Local disk is
  the "Presto drive".
- **JBOF** — Just a Bunch Of Flash: remote SSD enclosure reached over **NVMe-oF**.
- **SUT count** — 6–12 near-term, 10–30 at production scale.

## Run identity

- **run_id** — identifies one profiling run. In the MVP it is the run directory name
  (`ProfileData-<tag>-<timestamp>`). ClickHouse `PARTITION BY run_id`.
- **record_id** — `UInt64` sequence the loader assigns per record, only when child
  tables exist; links a main row to its child rows. MVP stopgap
  ([ADR-0005](decisions/0005-uint64-ids-stopgap.md)).
- **tag** — a human label for a run (e.g. `fio-4k-randread`), part of the directory name.

## IO stack (what we profile)

Layers, top to bottom: **syscall → VFS → page cache → filesystem (ext4/jbd2) →
block / blk-mq → NVMe driver → IRQ completion**.

- **blk-mq** — the multi-queue block layer; the **IO scheduler lives inside it**
  (mq-deadline/kyber/bfq are blk-mq elevators). "Generic block layer" = "BIO layer";
  `submit_bio` is the entry.
- **O_DIRECT** — IO that bypasses the page cache.
- **buffered write** — asynchronous; writeback kworkers submit the bios later, so the
  submitting process ≠ the calling process (a core attribution problem).
- **mmap / page-fault IO** — model loading uses mmap; syscall-only tracing misses it.
- **io_uring** — async submission that may have no syscall per IO.
- **readahead** — kernel-prefetched reads.
- **completion path** — IRQ → `nvme_complete_rq` → `bio_endio`.
- **GPUDirect Storage (cuFile / nvidia-fs)** — GPU-direct IO path on DGX.
- **device vs host latency split** — `device latency ≈ t(nvme_complete_rq) −
  t(nvme_setup_cmd)`; **host overhead = total − device**. Measurable only down at the
  NVMe driver.
- **IO identity stitching** — correlating one IO across layers (fd+offset → bio →
  request → NVMe cid). The core hard problem, deferred (see [roadmap.md](roadmap.md)).

## Profiling / streaming

- **eBPF** — the decided profiling mechanism (Profile FW's domain).
- **tracepoints** — `nvme:nvme_setup_cmd` / `nvme_complete_rq` are cheap and carry
  qid/cid/opcode → NVMe queue stats.
- **never-block rule** — nothing in the profiling hot path touches the network or waits
  on a consumer.
- **ring buffer** — the in-kernel/local buffer the profiler writes to; a separate stage
  drains it. Loss safety comes from local buffering, not from a fast receiver.
- **two-lane** — fast lane (in-kernel aggregated metrics, ~1–5 s fresh, for live
  dashboards) vs bulk lane (raw per-IO events trailing ~30–60 s).
- **tiered buffering** — RAM queue in steady state; overflow-spill to local scratch
  disk (never the SUT's SSD) only past a threshold.
- **transport ladder** — Vector (default) → custom gRPC gateway (escape hatch) → Kafka
  (last resort). See [ADR-0007](decisions/0007-streaming-transport-ladder.md).

## Data format (the `.pb` files)

- **StatLogs** — the wrapper message; one per `.pb` file; holds `repeated StatLog`.
- **StatLog** — one record: a `generic_format` (header) + a `payload` (layer data).
- **GenericFormat** — the header: timestamp, hostname, component, tag, log_level.
- **Payload** — layer-specific fields. Mapped by shape: scalars and singular
  sub-messages → main table; `repeated <Message>` → child tables.
- **1:1 group** — a singular sub-message (e.g. syscall `io_flags`), exactly one per
  record; **flattened** onto the main row as `<field>_<leaf>` columns (`io_flags_direct`).
- **child table** — `<stem>_<field>` for a repeated sub-message (e.g. NVMe `per_queue`,
  `per_core`); additive rows (1 main + N + M), linked by `record_id`.
- **leaf table** — for a repeated message nested inside another (`per_core_seq_random[]
  .entries[]`): one table of the innermost elements, named for the whole path
  (`<stem>_per_core_seq_random_entries`), with each outer level's scalars (`cpu_id`,
  `dir`) **denormalized** onto every row. The intermediate level gets no table of its
  own — one measurement is one row, no JOIN. See
  [decisions/0010-payload-field-shapes-to-tables.md](decisions/0010-payload-field-shapes-to-tables.md).
- Detection is **by structure/field-name, not message name**.

## Database / analysis (ClickHouse)

- **ClickHouse** — the chosen columnar OLAP DB ([ADR-0001](decisions/0001-database-clickhouse.md)).
- **MergeTree** — the table engine; `PARTITION BY` + `ORDER BY` are the key schema
  choices (nothing is automatic — the sort order is ours to pick).
- **AggregatingMergeTree / materialized view (MV)** — pre-computed rollups; store
  **min/max/percentile sketches (e.g. `quantileTDigestState`), never averages alone**
  (spikes must survive).
- **TTL … TO VOLUME** — age-based tiering / retention (demote raw to cold storage
  instead of deleting).
- **LTTB / M4** — display-downsampling algorithms (keep spikes visible at ~2000 px).
  ClickHouse has native `largestTriangleThreeBuckets` (`lttb`); M4 works in plain SQL
  anywhere.
- **rollup tiers** — multi-resolution (raw / 1s / 1m / 1h), routed by Grafana
  `$__interval` ("map tiles"). Upsampling cannot recover downsampled detail — the
  detail must still exist somewhere.
- **auto-rebalance** — ClickHouse OSS does **not** auto-rebalance shards; handled by a
  layered strategy (shard weights, loader-targeted writes, re-ingest from raw). See
  [ADR-0001](decisions/0001-database-clickhouse.md).
