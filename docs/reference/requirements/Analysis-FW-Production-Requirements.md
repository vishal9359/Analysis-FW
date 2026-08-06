# Analysis Framework — Production Requirements

**Status:** Draft v1 for senior review · **Owner:** Analysis FW engineer · **Date:** 2026-07-20
**Scope of this document:** the whole Analysis Framework at production scale — every stage from the SUT outward.

---

## How to read this

- Requirements are grouped **by capability** and written as plain point-lists.
- Each item has a short **tag** (e.g. `COL-3`) so the design doc and reviews can refer to it precisely. Ignore the tags when reading for sense.
- Each item has a **priority**:
  - **[M] Must** — required for the first production release (what POC 2 builds toward).
  - **[S] Should** — important, but can follow the first release.
  - **[L] Later** — a known future need; design must not block it, but we don't build it now.
- This is a **requirements** doc: it says *what* the system must do and *how well*, plus the decisions already locked. It does **not** design the *how* — that is the next document.
- **Why this document exists:** the PM requirements doc (`Frameworks.txt`) covers the Use-case Run & Deploy FW and the Profile FW, and explicitly leaves the Analysis FW to be defined later (Frameworks.txt §2, line 19). This document is that definition.

---

## A. Context and boundary

- The System Analysis Framework runs AI (and other storage) workloads on GPU platforms, profiles the full host + SSD stack, and analyses the overheads. It serves three projects: **On-Device AI, TraceVision, UVP Control**.
- It is built from sub-frameworks. The **Analysis Framework** is one of them. Its job: **turn profiling data into system-behaviour and IO-pattern insight, and present it to people.**
- **The boundary (fixed):** the Profile FW collects data *on the SUT* and writes it locally in the defined log format. The Analysis FW owns **everything from the SUT outward** — getting the data off the SUT, moving it, ingesting it, storing it, querying it, and showing it on UI and CLI.
- **The core principle (locked):** the Analysis FW **collects data from behind**. It never pushes work into the Profile FW's hot path, and nothing in that hot path waits for the Analysis FW. (See `COL` and `CON`.)

---

## B. Users (who this serves)

- **SSD firmware / NAND engineers** — deep IO and device behaviour; ad-hoc SQL; per-event detail.
- **System / performance engineers** — host-vs-device overhead, IO patterns, bottlenecks; catalog queries + dashboards.
- **Senior management** — run summaries, trends, health; dashboards only, plain language.
- One backend serves all of them. The **UI is primary for everyone**; the **CLI must stay in sync with the UI** — both are thin clients over one shared API.

---

## C. Scope

**In scope (this document):** on-SUT collection & shipping, transport, ingest, storage, data model & contracts, analysis & query, UI, CLI, replay, loss accounting, time handling, and the non-functional and operational requirements for all of it.

**Out of scope (owned elsewhere):**
- Profiling itself — *what* is captured and *how* (eBPF, tools, kernel) — is Profile FW's domain. We consume its output.
- Workload setup, SSD pre-conditioning, use-case execution — Use-case Run & Deploy FW.
- Anything below the NVMe driver that the host cannot see — out of scope for the first release (host-visible SSD data only).

---

# Functional requirements

## 1. Collection & shipping — on the SUT (`COL`)

*This is the heart of the production system and the biggest change from POC 1's offline files.*

- **COL-1 [M] Collect from behind.** The Profile FW's collection path writes only to a local buffer. A separate Analysis FW stage reads from that buffer and ships the data. The two are decoupled locally.
- **COL-2 [M] Never block the workload.** Nothing in the shipping path may stall, back-pressure, or add latency to the profiled workload or the Profile FW hot path. If the Analysis FW cannot keep up, it falls behind — it never pushes back onto profiling.
- **COL-3 [M] No data loss.** Under central-side slowness or outage, profiling events must not be dropped. **Lag is acceptable; loss is not.** (This is the single hardest requirement and shapes everything below.)
- **COL-4 [M] Tiered local buffering.** Steady state is memory-only (no disk writes). When the in-memory queue passes a threshold (consumer/gateway slow or down), the shipper **spills to a local scratch disk**. Draining resumes when the downstream recovers.
- **COL-5 [M] Spill disk must not be the SSD under test.** Writing overflow to the measured device would corrupt the measurement. The spill target is separate storage the SUT must provide.
- **COL-6 [M] Two lanes.**
  - **Fast lane:** small, pre-aggregated metrics (e.g. in-kernel histograms) shipped quickly (seconds fresh) to power the live view.
  - **Bulk lane:** the full raw per-event stream, allowed to trail behind (tens of seconds).
  - The live view must **never** depend on moving the full raw firehose in real time.
- **COL-7 [M] Stay within the on-SUT budget.** Collection + shipping together must fit **≤ 8 CPU cores and 8–16 GB RAM** on the SUT, enforced (e.g. cgroup-capped) so they cannot exceed it.
- **COL-8 [M] Use the separate management/data NIC.** Shipping traffic must use the dedicated NIC, not the workload/NVMe-oF data path, so it does not perturb the measurement.
- **COL-9 [M] Count everything.** Every stage (buffer, consumer, shipper, spill) exposes counters: events seen, shipped, spilled, dropped. Drops must be visible, never silent. (See `INT`.)
- **COL-10 [S] Configurable freshness and intervals.** Collection intervals, batch sizes, and per-lane lag targets are configurable per run / per SUT.
- **COL-11 [L] Backend abstraction for non-eBPF platforms.** The collector side must sit behind an interface so a platform without eBPF (e.g. Windows) can supply a different backend without changing the rest of the pipeline.

## 2. Transport — SUT to central (`XPT`)

- **XPT-1 [M] Centralize all raw data.** All raw events and aggregates are shipped to the central Analysis FW node. No "keep raw on the SUT and query it there" federation.
- **XPT-2 [M] Efficient on the wire.** Events are batched and compressed before sending (binary encoding, not text). Target: strong compression so network cost stays well within the dedicated NIC.
- **XPT-3 [M] Delivery is acknowledged and checkpointed.** A batch is considered delivered only after the central side has durably accepted it. The shipper advances its checkpoint only on that acknowledgement, so a crash or outage resumes without loss or (ideally) duplication.
- **XPT-4 [M] Backpressure costs freshness, never safety.** If the central side is slow, the effect is: shipper slows → local spill grows → lag increases. There is **no** path from central slowness back into the profiling hot path.
- **XPT-5 [M] Tolerate a central outage without loss.** The pipeline must ride out a central-side outage of a stated duration (target: **≥ 2 hours** at full event rate) using local spill, then catch up. (Exact duration depends on scratch-disk size — see `SIZ` / `OPEN`.)
- **XPT-6 [M] Preferred transport: lightweight agent.** Default approach is a prebuilt shipping agent (e.g. Vector) that already provides disk buffering, checkpointing, and end-to-end acknowledgement. Must be validated at target rate within the CPU budget.
- **XPT-7 [S] Escape hatch: custom gRPC gateway.** If the agent cannot meet the budget or the loss guarantee, fall back to a custom batch-push (protobuf/Arrow) to a stateless ingest gateway, with acknowledge-after-commit and insert-time de-duplication.
- **XPT-8 [L] Message broker only if forced.** A broker (e.g. Kafka) is a last resort, justified only if replay-as-a-feature or multiple independent consumers become hard requirements — the no-loss guarantee is already met without one.

## 3. Ingest gateway — central side (`ING`)

- **ING-1 [M] Decode and land.** Receive batches, decode the profiling records, and insert them into the store as typed rows.
- **ING-2 [M] Idempotent ingest.** Re-delivery of an already-accepted batch (after a retry or restart) must not create duplicate rows. Reloading a whole run must be safe and leave exactly one copy.
- **ING-3 [S] Stateless and horizontally scalable.** Gateways hold no essential state between batches, so more can be added to scale ingest throughput.
- **ING-4 [M] Reconcile at ingest.** The gateway records how many events it accepted per run, for end-to-end reconciliation against what the SUT emitted. (See `INT`.)
- **ING-5 [S] Schema-version aware.** Ingest tolerates records tagged with a schema/format version, so the SUT side and central side can be upgraded independently within a compatibility window.

## 4. Storage — the database (`STO`)

- **STO-1 [M] ClickHouse (OSS) is the store.** Decided; free/OSS license; columnar, high-ingest, strong compression, Grafana support, multi-node growth path.
- **STO-2 [M] Persist raw AND aggregates, both durable.** Every raw per-event record is stored, and pre-computed aggregates are stored. Both survive; aggregates are not a substitute for raw.
- **STO-3 [M] One table per layer.** Block, NVMe, syscall, filesystem, memory, SSD (and future PCIe/RDMA, platform/CPU-GPU-mem) each have their own table, because their payloads genuinely differ.
- **STO-4 [M] Partition by run.** Data is partitioned per profiling run, so a run can be dropped and reloaded as one cheap operation (this is what makes reload idempotent — see `ING-2`).
- **STO-5 [M] Order data by (run, SUT, time).** The physical sort key supports the dominant query shape (one SUT, one time range). The time key must have real sub-second resolution so the index discriminates at production event rates. **This choice is expensive to change later — settle it before large data exists.**
- **STO-6 [M] Multi-resolution aggregates (rollups).** Maintain rolled-up tiers (e.g. raw / 1s / 1m / 1h) so queries and dashboards read the right resolution for the zoom level.
- **STO-7 [M] Rollups preserve shape, never averages alone.** Every rollup carries **min, max, and percentile/quantile sketches** — not just averages. An average hides the spikes this framework exists to find.
- **STO-8 [M] Retention with tiering.** Raw events have a short-to-medium retention; aggregates are kept longer. Old raw data is **demoted to cheaper/cold storage before deletion**, not deleted outright. Exact durations are configurable (see `SIZ`, `OPEN`).
- **STO-9 [S] Multi-node cluster.** Storage must scale beyond one node for 10–30 SUTs: replication for availability, sharding for capacity/throughput. (Single node is acceptable for the first milestone; the growth path must be real.)
- **STO-10 [S] Rebalancing strategy.** Because OSS ClickHouse does not auto-rebalance shards, the design must state how data spreads across shards as the cluster grows (e.g. shard weighting, loader-targeted writes, re-ingest from raw). *`clickhouse-copier` is deprecated — do not rely on it.*
- **STO-11 [M] Query time is bounded.** Time-range and aggregate queries over a run return in seconds even on billion-row datasets. (Verified in POC 1; a standing requirement.)

## 5. Data model & contracts (`MOD`)

- **MOD-1 [M] One log format, two collection modes.** The record format is identical for offline files (POC 1) and live streaming (production). Streaming does not get its own schema.
- **MOD-2 [M] Generic header on every record.** Every record carries: timestamp, hostname/SUT, component (layer) id, tag, log level — per the defined format.
- **MOD-3 [M] Protobuf is the shared contract.** The `.proto` files are the single source of truth shared by the Profile FW (Go writer) and the Analysis FW (reader/loader). Field changes are contract changes.
- **MOD-4 [M] Stable run identity.** Each run has a `run_id` (folder/partition key) and each SUT a stable id. The `run_id` convention is agreed with the team and used consistently from file naming through to storage partition.
- **MOD-5 [M] Nanosecond timing lives in the payload.** The header timestamp may be coarse (currently 1-second); the fields needed for latency math (issue/complete/setup/enter ns) live in the payload and carry full resolution.
- **MOD-6 [S] Per-layer payloads are defined jointly with Profile FW.** The payload of each layer (block, nvme, syscall, fs, memory, ssd, …) is finalized with the Profile FW engineer; the current shapes are placeholders until then.
- **MOD-7 [L] Cross-layer IO identity.** A design must exist to stitch one IO across layers (syscall → VFS → block request → NVMe command). Fields to carry the linking ids exist now; full stitching is deferred (see `DEF`).
- **MOD-8 [M] Framing is self-delimiting.** On-disk and on-wire records are length-delimited so a reader can find record boundaries and detect a truncated tail. (Concatenated protobuf without framing is unreadable.)

## 6. Analysis & query (`QRY`)

*These trace directly to the parameter list the Profile FW is required to collect (Frameworks.txt §2.2, item 3) and the query requirement (item 5).*

- **QRY-1 [M] One shared query/API layer.** All analysis goes through one backend API. UI and CLI are clients of it; there is no second query path.
- **QRY-2 [M] Canned query catalog.** A versioned catalog of the standard analyses, including at least:
  - latency percentiles over a time window; latency distribution
  - IOPS and throughput timelines
  - queue-depth over time; NVMe queue stats
  - IO size distribution; sequential-vs-random ratio; read/write mix
  - per-process IO attribution; multi-core distribution
  - IO burst detection
  - LBA hotness
  - **host-vs-device latency split** (the overhead this framework exists to measure)
  - SSD health/telemetry trend across a run
- **QRY-3 [M] Ad-hoc SQL.** Engineers can run their own SQL directly against the data, alongside the catalog.
- **QRY-4 [M] Run and window comparison.** The same analysis can be run over two runs or two time windows side by side.
- **QRY-5 [M] Honest display downsampling.** Charts that can't show every point use a shape-preserving reduction (LTTB / M4) computed at query time, so spikes remain visible at any zoom. Averages-only downsampling is not acceptable.
- **QRY-6 [M] Resolution routing.** Queries automatically read the appropriate rollup tier for the requested time range / zoom, falling back to raw when detail is needed.
- **QRY-7 [S] Cross-layer / correlation queries.** As cross-layer identity lands (`MOD-7`), the catalog gains queries that join layers (e.g. a syscall to its block request to its NVMe command).
- **QRY-8 [M] Aggregates verified correct.** Rollup/aggregate results are cross-checked against an independent source (e.g. fio's own end-of-run summary) so the numbers are trusted.

## 7. UI (`VIZ`)

- **VIZ-1 [M] Grafana reads the database.** The UI is Grafana over ClickHouse. Everything shown is backed by stored data; Grafana does not receive pushed data.
- **VIZ-2 [M] Standard dashboards per layer.** Saved dashboards for at least block-layer and SSD analysis; more layers as they come online. Pick run + time range → analyse.
- **VIZ-3 [M] Live view from the fast lane.** The live/near-real-time dashboards are powered by the fast-lane aggregates (`COL-6`), not by the raw firehose.
- **VIZ-4 [S] Audience-appropriate views.** Summary/trend/health views for management; detailed views for engineers — over the same backend.
- **VIZ-5 [L] Custom-designed screens.** Beyond Grafana defaults. Deferred; POC 1/2 use Grafana's built-in capabilities.

## 8. CLI (`CLI`)

- **CLI-1 [M] Thin client over the same API.** The CLI calls the same query/API layer as the UI. No separate logic or data path.
- **CLI-2 [M] Kept in sync with the UI.** Any analysis available in the UI is reachable from the CLI and vice-versa (they expose the same catalog).
- **CLI-3 [S] Scriptable output.** Machine-readable output (JSON/CSV) for automation and for the Use-case Run & Deploy FW to consume later.

## 9. Replay & reproducibility (`RPL`)

- **RPL-1 [M] Reproducible runs.** "Same config + same seed + same order" is sufficient to reproduce a run's behaviour. Run identity and configuration are recorded with the data.
- **RPL-2 [S] Replay as a repair/verification path.** The stored raw data (or its source files) can be re-ingested to rebuild aggregates or recover from a downstream problem — raw is the system of record.

## 10. Loss accounting & data integrity (`INT`)

- **INT-1 [M] Reconciliation is non-negotiable.** For every run, the system reconciles **events emitted on the SUT vs rows landed in the database**, automatically, and reports any gap.
- **INT-2 [M] Drop counters at every stage.** Buffer, consumer, shipper, spill, transport, ingest — each reports what it dropped. Silent loss is a defect.
- **INT-3 [M] Corruption/truncation is detectable.** A partially written file or batch (crash, partial flush) is detected at a record boundary, not silently parsed as garbage.
- **INT-4 [S] Reconciliation is visible.** Per-run reconciliation status is surfaced (UI/CLI/health), so a run known to be complete can be trusted and an incomplete one is flagged.

## 11. Time & synchronization (`TIME`)

- **TIME-1 [M] Configurable time sync and intervals.** Cross-SUT time synchronisation approach and collection intervals are configurable.
- **TIME-2 [M] Consistent, comparable timestamps.** Timestamps from different SUTs and layers must be comparable on one axis (a single run spans multiple SUTs).
- **TIME-3 [M] Settle the nanosecond clock contract.** The payload ns fields must have a defined meaning — **epoch** vs **monotonic/boot-relative + a boot-time offset**. eBPF's native clock is boot-relative; if that reaches the store unconverted, time-range queries silently return nothing. The contract with the Profile FW (Go) side must fix this, and ingest must validate it. *(Raised from hands-on work; see `OPEN`.)*

---

# Non-functional requirements

## 12. Scale & performance (`SCL`)

- **SCL-1 [M] SUT count.** 6–12 SUTs near-term; **design for 10–30** at production scale.
- **SCL-2 [M] Per-SUT event rate.** Sustain **~200K–1M events/second per SUT** on the profiled layers.
- **SCL-3 [M] Aggregate rate.** Sustain the fleet total — **peak ~30M events/second** across 30 SUTs — end to end without loss.
- **SCL-4 [M] Freshness.** Live-view lag in **seconds**; raw-event lag within **tens of seconds**. Aim for live, but freshness is a target, not a safety property (loss is the safety property).
- **SCL-5 [M] Parallel SUTs.** Runs execute on multiple SUTs in parallel; the pipeline handles concurrent streams.
- **SCL-6 [S] Long runs.** Support runs lasting **days to weeks** (Frameworks.txt P2) without degradation or unbounded resource growth.
- **SCL-7 [M] Ingest throughput headroom.** Central ingest + storage sustain the aggregate rate with headroom for catch-up after a lag/outage (draining is faster than real-time).

## 13. Reliability & availability (`REL`)

- **REL-1 [M] No loss under failure.** Restated as the top reliability property: no event loss under central slowness, restart, or outage within the stated tolerance window.
- **REL-2 [M] Graceful degradation.** Under overload the system degrades by **adding lag and spilling to disk**, in a defined order, never by dropping or by stalling the workload.
- **REL-3 [S] Storage availability.** The database tolerates a node failure without data loss and without a full outage (replication) at production scale.
- **REL-4 [S] Recovery is automatic.** After an outage, the pipeline catches up on its own and reconciliation confirms completeness — no manual replay in the normal case.
- **REL-5 [S] Pipeline self-monitoring.** The pipeline reports its own health (lag per lane, spill depth, drop counters, ingest rate) so operators see trouble before data is at risk.

## 14. Storage sizing & retention (`SIZ`)

- **SIZ-1 [M] Duty cycle: configurable, per-run baseline.** The system must support both continuous (24/7) and per-run profiling. **Sizing is baselined on per-run**; the 24/7 ceiling is stated as the upper bound. *(Decision: configurable, per-run typical.)*
- **SIZ-2 [M] Size to measured record size.** Sizing uses the **measured** on-disk record size, not an assumed one. Current measurement ≈ **~179 bytes/event** (invented payloads, likely richer than final). At 1M ev/s that is ~179 MB/s raw per SUT, ~25 MB/s compressed (~7×), ~770 MB/s aggregate at 30 SUTs. **Re-measure once real payloads exist** (see `OPEN`).
- **SIZ-3 [M] Local scratch (spill) sizing is a stated SUT spec.** The SUT must provide a scratch disk sized to the required outage-tolerance window (proposal: ~100 GB ≈ ~2 h at full rate). This is a provisioning requirement on the SUT, alongside the separate NIC.
- **SIZ-4 [M] Central retention is a stated, configurable policy.** Raw-retention and aggregate-retention durations are explicit policy (not a code default), tunable per deployment. Values pending duty-cycle + retention decisions (`OPEN`).
- **SIZ-5 [S] Volume projection is maintained.** A living projection (events/s → bytes/day → TB/month, per duty cycle) informs cluster and retention sizing and is updated as measurements firm up.

## 15. Security & access (`SEC`)

- **SEC-1 [M] Free/OSS licenses only.** Every component must be free-for-our-use / open-source. (e.g. Kafka/ClickHouse Apache-2.0 fine; Redpanda BSL excluded; AGPL acceptable for internal use.) A license check gates any new dependency.
- **SEC-2 [S] Authenticated access.** The database, API, UI, and CLI require authentication in production. (POC defaults with no password are not acceptable for a shared/production deployment.)
- **SEC-3 [S] Network isolation.** Management/data-collection traffic is isolated (dedicated NIC per `COL-8`); production services are not exposed on open host networking without access control.
- **SEC-4 [L] Access scoping.** Role-appropriate access (e.g. management vs engineer views) and per-project/run separation as the fleet and audience grow.

## 16. Portability & platforms (`PORT`)

- **PORT-1 [M] Linux + DGX Spark first.** First target is Linux on DGX Spark (ARM/Grace; local "Presto drive"). Design must not assume x86.
- **PORT-2 [S] Multi-architecture.** Support other platforms (DGX Workstation, server platforms) and both ARM and x86.
- **PORT-3 [S] Remote SSD / JBOF.** Support SUTs whose SSDs are remote over NVMe-oF (JBOF, all transport types). Block-layer profiling still applies; network filesystems (NFS/CIFS) do not (no bio path) and are out of scope for device analysis.
- **PORT-4 [L] Windows.** Support Windows SUTs. eBPF does not exist there, so the collector backend abstraction (`COL-11`) must hide the platform difference from the rest of the pipeline.
- **PORT-5 [M] Runs on a dedicated controller/central node.** The Analysis FW central services run on their own node, reaching out to SUTs — not co-located on the SUT.

## 17. Extensibility (`EXT`)

- **EXT-1 [M] New layers/components.** Adding a new profiling layer (e.g. PCIe/RDMA, CPU/GPU/memory platform metrics) is a matter of adding a proto + table + catalog entries, without reworking the pipeline.
- **EXT-2 [S] New SUT types and use-cases.** Support new SUT/host types and new AI / enterprise-SSD use-cases without core changes (Frameworks.txt P1 overall requirement).
- **EXT-3 [S] Plugin-style interfaces.** Extension points (collector backends, transport, loaders, catalog) are interface-based so pieces can be swapped. *(Extension model assumed pending PM confirmation — see `OPEN`.)*
- **EXT-4 [M] Platform metrics have a home.** `platform.proto` (CPU/GPU/memory utilization) is named in the format but has no place in the current hierarchy — the model must define where it lands (see `OPEN`).

## 18. Operations & observability (`OPS`)

- **OPS-1 [M] Scripted, repeatable deployment.** Central services (DB, gateway, Grafana, agents) deploy from scripts/containers — reproducible install, start/stop, config, data-dir management.
- **OPS-2 [M] Schema and config in version control.** All DDL, catalog SQL, and pipeline config live in git; schema changes are reviewed and versioned.
- **OPS-3 [M] Health dashboard.** An operational dashboard (from DB system tables + pipeline counters) shows ingest rate, lag, spill, drops, part/merge health, disk usage.
- **OPS-4 [S] Backup and restore, tested.** Central data has a tested backup/restore path (raw is the system of record; replay is the deeper fallback).
- **OPS-5 [S] Alerting on integrity.** Drop-counter and reconciliation-gap conditions raise alerts — the operator learns of loss risk proactively.
- **OPS-6 [M] Framework logs.** The Analysis FW keeps its own readable logs (timestamps, flow) for its pipeline, distinct from the profiling data it carries (Frameworks.txt overall requirement).

---

# Constraints (locked decisions) (`CON`)

- **CON-1** Store is **ClickHouse OSS**. (Decided across two selection sessions.)
- **CON-2** **Collect-from-behind / never-block** is architectural, not optional.
- **CON-3** **Loss unacceptable, lag acceptable.**
- **CON-4** On-SUT budget: **≤ 8 cores, 8–16 GB RAM**, enforced.
- **CON-5** **Separate NIC** for management/data collection.
- **CON-6** **All raw data centralized**; no keep-on-SUT federation.
- **CON-7** **Spill disk ≠ the SSD under test.**
- **CON-8** **Free/OSS licenses only.**
- **CON-9** Transport preference order: **lightweight agent → custom gRPC gateway → broker (last resort)**.
- **CON-10** Central services run on a **dedicated controller/central node**.
- **CON-11** **Both raw events and aggregates are durable** — neither is dropped in favour of the other.

---

# Interfaces & contracts (summary)

- **Profile FW ↔ Analysis FW:** the log format + `.proto` files + folder hierarchy + `run_id` convention. Changing any is a cross-team contract change.
- **Profile FW REST API:** trigger / collect / stop for data collection (Profile FW owns; Analysis FW may drive it).
- **Analysis FW API:** one query/analysis API behind UI and CLI.
- **Storage:** ClickHouse schema (tables per layer, rollups) — the contract between ingest and query.
- **Downstream:** Use-case Run & Deploy FW integration (later) consumes run status / summaries via the CLI/API.

---

# Open decisions (need an owner + answer) (`OPEN`)

| # | Decision | Why it matters | Current assumption |
|---|----------|----------------|--------------------|
| OPEN-1 | **Central retention period** for raw events | Sets cluster size and cost | Short raw / long aggregates; number TBD |
| OPEN-2 | **SUT scratch-disk size** for spill | Sets outage tolerance (`XPT-5`) | ~100 GB ≈ ~2 h proposed |
| OPEN-3 | **Real record size** | Sizing math (`SIZ-2`); current ~179 B is from invented payloads | ~179 B/event, re-measure |
| OPEN-4 | **ns clock contract** (epoch vs monotonic+offset) | Wrong choice silently breaks time queries (`TIME-3`) | Assume epoch until Profile FW confirms |
| OPEN-5 | **Final ORDER BY / time-key** | Expensive to change after data exists (`STO-5`) | (run_id, sut_id, ts) with ns time key |
| OPEN-6 | **Extension model** | Shapes EXT interfaces | Plugin-style assumed; PM said "don't know yet" |
| OPEN-7 | **`platform.proto` placement** | CPU/GPU/mem metrics have no home yet (`EXT-4`) | Add a Platform table/layer |
| OPEN-8 | **Cross-layer stitching approach** | Enables correlation queries (`QRY-7`, `MOD-7`) | Deferred; carry link ids now |
| OPEN-9 | **Duty cycle confirmation** | 10× swing in storage/retention | Configurable, per-run baseline (working assumption) |

---

# Assumptions (`ASM`)

- **ASM-1** Profile FW delivers data in the agreed log format / proto; payloads finalized jointly.
- **ASM-2** Each SUT provides a separate NIC and a scratch disk (not the SUT-under-test) — provisioning requirements.
- **ASM-3** eBPF is the profiling mechanism on Linux; non-eBPF platforms come via the collector abstraction.
- **ASM-4** The central node has network reach to all SUTs on the management NIC.
- **ASM-5** Per-run is the common operating mode; continuous is supported but not the sizing baseline.

---

# Deferred to later phases (`DEF`)

- **DEF-1** Live/streaming **implementation** — the *approach* is decided in POC 1 (Activity 5); the build is POC 2. (POC 1 stays on offline files.)
- **DEF-2** Custom-designed UI screens (Grafana defaults until then).
- **DEF-3** Multi-node DB **deployment** (growth plan on paper first; single node acceptable early).
- **DEF-4** Full **cross-layer IO stitching** (`MOD-7`) — link fields exist now.
- **DEF-5** **Windows** SUT support (`PORT-4`).
- **DEF-6** **Below-driver** SSD data (internal device telemetry beyond host-visible SMART/OCP/log pages).
- **DEF-7** **Use-case Run & Deploy FW** automation integration.

---

# Priority summary (first production release = the "Must" set)

- **Data safety:** COL-1..9, XPT-1..6, ING-1/2/4, INT-1..3, REL-1/2.
- **Store & serve:** STO-1..8, STO-11, MOD-1..5/8, QRY-1..6/8, VIZ-1..3, CLI-1/2.
- **Scale target:** SCL-1..5/7.
- **Foundations to settle early (hard to change later):** STO-5 (order/time key), TIME-3 (ns contract), SIZ-1..4 (sizing/retention), CON-* (locked constraints).
- **Everything marked [S]/[L]** follows, but the design must not block it.

---

# Sources / traceability

- **`Frameworks.txt`** — PM requirements source of truth (defers Analysis FW to this doc; supplies the parameter/analysis list, layers, platforms, priorities).
- **`POC1_Analysis_FW_Activities.md`** — the four boss activities + Activity 5 (streaming study) this production system grows from.
- **`CONTEXT-System-Analysis-FW-Session.md` / `CONTEXT-Database-Selection-Session.md`** — the decision ledger and architecture brainstorm behind the locked constraints (CON-*), the transport ladder, the two-lane design, ClickHouse selection, and the rebalancing strategy.
- **Hands-on POC work (this session)** — measured record size (`SIZ-2`), the ns clock contract issue (`TIME-3`), framing requirement (`MOD-8`), idempotent-reload via partitions (`STO-4`).
