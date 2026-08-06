# Roadmap — past, present, future

*Where the project has been and where it's going. Charter is in
[overview.md](overview.md); current architecture in [architecture.md](architecture.md).*

The work is organized as **agile POC phases**, each teaching the next.

## Past — POC 1 (offline foundation)

**Question POC 1 answered:** "can we store and analyze profiling data, and how will
we stream it later?" Input mode was **offline files only** (streaming deferred by
design). Boss-level activities (detailed plan:
[reference/context/POC1_Analysis_FW_Activities.md](reference/context/POC1_Analysis_FW_Activities.md)):

1. **Database service + schema** — select a time-series DB, deploy it, define schema
   v0 + retention. → **ClickHouse selected** ([ADR-0001](decisions/0001-database-clickhouse.md)).
2. **Offline data-file input** — a validating, idempotent loader + a synthetic file
   generator + folder-hierarchy contract. → **delivered as the MVP (Module 1)** below.
3. **Time-series analysis** — query catalog, aggregates verified vs fio, run/window
   comparison, billion-row scale test. → block-metric queries in
   [block-metrics.md](block-metrics.md).
4. **UI** — Grafana OSS default screens; two saved dashboards (block, SSD).
5. **Streaming approach study (decision only, team-added)** — the transport ladder
   and two-lane design, as the bridge into POC 2
   ([ADR-0007](decisions/0007-streaming-transport-ladder.md)).

## Present — the MVP (Module 1 offline loader)

**Status: built and in good shape** (this repo). Delivered:

- Runtime schema derivation from `.proto` (no code change for new fields/layers/producers).
- Wrapper + `generic_format`/`payload` format; repeated sub-messages → child tables.
- Batch loading of many runs; fail-fast / `--continue-on-error`; typed exit codes.
- Multi-format timestamp parsing → RFC 3339 UTC; per-run reconciliation.
- 22-test suite against an in-memory store; realistic time-series fixture generator.
- `src/` layout, editable `config/config.yaml`, self-contained docs.

Run/verify: see the [README](../README.md). Carries forward into production as the
**replay / backfill / repair** path, not the primary path.

## Future — POC 2 (live streaming at scale)

**Theme:** get the data in **live, at fleet scale, with zero loss**, and make the DB
and UI production-shaped underneath it. Day-one input is the POC 1 streaming decision
memo. Proposed high-level activities (to confirm with the manager):

- **A. Live streaming ingestion pipeline** — never-block SUT→gateway→ClickHouse with
  two-lane delivery and tiered (RAM→disk-spill) buffering.
- **B. No-loss guarantee** — drop accounting at every stage, per-run reconciliation,
  forced-outage spill/drain resilience test.
- **C. Multi-node ClickHouse cluster** — sharding/replication/HA + the rebalancing
  strategy (POC 1 delivered this on paper only).
- **D. Schema & storage maturity** — rollup MVs (min/max/quantile sketches),
  downsampling tiers (raw/1s/1m/1h) with `$__interval` routing, tiered retention, and
  production `run_id`/`record_id` + idempotent (drop-partition) reload + online schema
  evolution (this absorbs the MVP's known debt).
- **E. Live analysis UI** — purpose-built dashboards on the fast lane; LTTB/M4 display
  downsampling; run/window comparison.
- **F. Orchestration + fleet-scale validation** — integrate with Use-case Run & Deploy
  FW; soak/scale proof on DGX Spark + 6–12 SUTs.

## Deferred to POC 3+ (consciously out of scope for now)

- Custom-designed UI screens (beyond Grafana defaults).
- Windows host support (eBPF absent → a collector interface must hide the backend).
- **Cross-layer IO identity stitching** (fd+offset → bio → request → NVMe cid) — the
  core hard tracing problem, deferred beyond the streaming build.
- Below-driver SSD internals (needs host-visible SMART / NVMe log pages / OCP telemetry).
- Integration depth with the other sub-frameworks beyond the run trigger.

## Open questions that gate sizing (still unanswered)

1. **Duty cycle** — 24/7 profiling vs per-run only? (Drives ~1 TB/day/SUT →
   ~30 TB/day at full fleet.)
2. **Central retention period** for raw events at production scale.
3. **SUT local scratch-disk size** for overflow spill (proposal: state ~100 GB).

Plus a dependency shift: **DGX Spark + the SUT fleet (separate NIC, non-SUT scratch
disk) become required in POC 2**, where they were nice-to-have in POC 1.
