# Session Context — System Analysis Framework: Analysis FW planning, POC 1, data pipeline & production architecture

*Purpose: hand-off/primer file. Load this at the start of a new session to restore full context on the Analysis Framework work — project understanding, all decisions with their reasoning, POC 1 plan state, database study results, and the production streaming architecture brainstorm.*
*Session date: July 2026 · Companion file: **`CONTEXT-Database-Selection-Session.md`** (separate deep-dive session on DB selection — ClickHouse finalized; contains verified findings on rebalancing, GreptimeDB downgrades, StarRocks/Doris alternatives. Read it too; §8 below reconciles the two.)*

---

## 1. Project & team context

**Source of truth:** `Frameworks.txt` (this folder). A **System Analysis Framework** to run any workload (AI or otherwise) on any storage/platform, profile the full host+SSD stack, and analyze overheads/IO behavior — insight feeds future SSD/storage development. Serves three projects: **On-Device AI, TraceVision, UVP Control** (all need the same profiling/analysis on different SSD types/use-cases).

**Sub-frameworks:** Use-case Run & Deploy FW · Profile FW · Analysis FW (+ UVP Control; "Tool Development & Deployment" = folded into Use-case Run & Deploy FW).

**Team model:** 1 engineer per framework. **The user owns the Analysis FW, including data collection from profiling** (boundary: Profile FW produces data on the SUT; Analysis FW owns everything from the SUT outward — shipping, ingest, DB, queries, UI). Claude is the user's deep-work partner on this. **Agile: 2–3 POCs in phases; each phase teaches the next.** The user's boss supplies high-level activities; the user details them for senior review.

## 2. Decision ledger (all confirmed by user/PM — do not re-ask)

**Architecture & scope**
- Framework runs on its own **controller node**, reaches out to SUTs, runs workloads in parallel per config; **6–12 SUTs** near-term, **10–30 SUTs** at production scale; **multi-node DB required**.
- Start: **Linux + DGX Spark** (ARM/Grace; local disk is the "Presto drive"). Later: other platforms, multi-arch, JBOF with **all transport types**, Windows (P2 — eBPF doesn't exist there → collector layer must hide the backend behind an interface).
- **eBPF-based profiling is the decided direction** (Profile FW engineer's domain). If eBPF doesn't fit DGX Spark, profiling engineer finds an alternative. Custom kernel comes from an expert — hand them eBPF kernel-config needs (e.g. `CONFIG_DEBUG_INFO_BTF`).
- **Extension model:** plugin-style interfaces assumed (pending PM confirmation — PM said "don't know yet").
- **Open-source / free-for-use licenses only** (e.g. Redpanda BSL excluded; Kafka/ClickHouse Apache-2.0 fine; TDengine AGPL acceptable for internal use).
- **Replay** requirement = "same config + same seed + same order" is sufficient.

**Data & analysis**
- **Every raw per-IO event AND aggregates, both durable.** This is the driving volume constraint.
- Users range from SSD firmware engineers to senior management; **UI is primary for everyone; CLI must stay in sync with UI** → both are thin clients over one shared backend/API.
- Real-time model: **everything writes to the DB; Grafana reads the DB.** POC 1 UI = **Grafana default screens only** (Explore + default panels; no custom screen design).
- Queries: both canned catalog and ad-hoc SQL.
- Time sync and collection intervals: configurable.

**Production streaming (this session's brainstorm — user confirmed)**
- Event rate assumption OK: ~200K–1M events/s per SUT (peak ~30M/s aggregate at 30 SUTs).
- Freshness: seconds is fine, aim for live. **Loss is NOT acceptable; lag is.**
- On-SUT budget for profiling+shipping: **8 cores, 8–16 GB RAM**.
- **Separate NIC** for management/data-collection confirmed (avoids perturbing workload/NVMe-oF traffic).
- **All raw data must be centralized** on the Analysis FW node (no keep-raw-on-SUT federation).
- **Kafka = last resort only**; prefer lighter/optimized options.

## 3. IO stack understanding (corrections established this session)

User's mental model was validated with these corrections (full detail in session, key facts):
- **IO scheduler lives INSIDE blk-mq** (mq-deadline/kyber/bfq are blk-mq elevators); "generic block layer" and "BIO layer" are the same thing (`submit_bio` = entry).
- **Buffered write() is asynchronous** — writeback kworkers submit the bios later → the submitting process ≠ the calling process (attribution across this boundary is a core tracing problem).
- Paths that MUST be covered beyond plain read/write: **O_DIRECT**, **mmap/page-fault IO** (model loading uses mmap — syscall-only tracing misses it), **io_uring** (possibly no syscall per IO), **readahead**, **completion path** (IRQ → `nvme_complete_rq` → `bio_endio`), journal/metadata/swap IO, **GPUDirect Storage (cuFile/nvidia-fs)** on DGX.
- **Profile down to the NVMe driver, not just the scheduler** — the host-vs-device overhead split is only measurable there: `device latency ≈ t(nvme_complete_rq) − t(nvme_setup_cmd)`; host overhead = total − device. Tracepoints `nvme:nvme_setup_cmd`/`nvme_complete_rq` are cheap and carry qid/cid/opcode (→ NVMe queue stats).
- Below the driver = not eBPF-visible; comes from host-visible SSD data (SMART, NVMe log pages, OCP telemetry) — P1 scope is host-visible only.
- Per-layer eBPF hook map exists (syscalls→VFS→page cache→ext4/jbd2→block/blk-mq→nvme→IRQ); **stitching IO identity across layers (fd+offset → bio → request → NVMe cid) is the core technical problem**, deferred beyond POC 1.
- NVMe-oF (JBOF) re-enters the same block/nvme-fabrics path → our block-layer profiling works for remote SSDs; NFS/CIFS does not (network stack, no bio).

## 4. Data receive architecture (the settled core principle)

**The Analysis FW never receives pushed data — it collects from behind.** Profile FW's hot path only writes locally; a separate stage ships at its own pace. The never-block rule: nothing in the profiling hot path touches the network or waits for a consumer. Ring-buffer safety comes from local decoupling, NOT from a fast receiver (receiver speed controls lag; local buffering controls loss).

- **POC 1 (temporary):** offline data files, collected after the run. The user explicitly declared offline files a *temporary POC 1 mechanism* — do not carry them into production thinking.
- **Production (this session's design):** pure streaming with **tiered buffering** —
  `ring buffer → consumer (pinned core, drain-only, memcpy to in-RAM queue) → shipper (separate process, cgroup-capped: batch+encode+compress+send) → gateway/agent → ClickHouse`.
  Steady state = memory-only (sub-second lag, **zero disk writes**). **Overflow-spill to local disk only when the RAM queue passes a threshold** (gateway slow/outage). RAM-only math: 6–8 GB ≈ only ~100 s of absorption at 80 MB/s → violates no-loss → disk overflow tier is mandatory. ~100 GB scratch ≈ ~2 h of central outage tolerance. Write-through WAL mode = optional config (only buys crash-recovery of in-RAM seconds; costs ~2–4% of a scratch NVMe's sequential bandwidth — sequential append is cheap, Kafka-style).
  **Spill disk must NOT be the SSD under test** (measurement purity) — SUT provisioning requirement alongside the separate NIC.
- **Two-lane design:** (1) fast lane = **in-kernel aggregated** metrics (BPF map histograms — tiny, ~1–5 s fresh, powers live Grafana); (2) bulk lane = raw per-IO events trailing ~30–60 s. Live view never depends on moving the firehose in real time.
- **Loss accounting is non-negotiable:** drop counters at every stage + automated per-run reconciliation (events emitted vs rows in DB).
- Backpressure chain: slow ACKs → shipper backs off → disk spill grows → stops there. **No code path exists from gateway slowness back to the ring buffer** (user probed this explicitly; answer: costs freshness, never safety).

**Transport ladder (user-approved preference order):**
1. **Default: Vector agent → (optional Vector aggregator) → ClickHouse** — prebuilt version of the same topology; disk buffers, checkpointing, end-to-end ACKs; MPL-2.0. Benchmark at 1M ev/s/SUT within CPU budget.
2. **Escape hatch: custom gRPC batch push → stateless ingest gateway(s) → ClickHouse.** Explained to user in depth: protobuf/Arrow batches with `batch_id`, ACK-only-after-ClickHouse-commit, checkpoint advance on ACK, ClickHouse insert dedup → effectively exactly-once; gateways stateless → horizontal scale; trick: make WAL record format = wire format so shipper never re-encodes per event.
3. **Kafka: last resort** — only if replay-as-a-feature or multiple independent consumers become requirements (no-loss is already met without it).

**Sizing math (keep):** 1M ev/s × ~80 B ≈ 80 MB/s raw per SUT → ~10–17 MB/s compressed (zstd 5–10×) → ~450 MB/s aggregate at 30 SUTs — fine on 25GbE + a 3–5-node ClickHouse cluster. Real risks: per-event CPU cost in userspace (→ binary formats, batching), network sharing (→ separate NIC), silent drops (→ counters).

**Open sizing questions (asked, NOT yet answered):**
1. **Duty cycle** — 24/7 profiling vs per-run only? (Continuous ≈ ~1 TB/day/SUT compressed → ~30 TB/day central at full fleet — order-of-magnitude storage driver.)
2. **Central retention period** for raw events at production scale.
3. **SUT local scratch disk size** for overflow spill (proposal: make ~100 GB a stated spec).

## 5. POC 1 plan state (Analysis FW)

**Main doc: `POC1_Analysis_FW_Activities.md`** — detailed breakdown of the boss's 4 high-level activities (+1 team-added), ~4 weeks, offline files input, weeks 1–2 dev Linux box / weeks 3–4 DGX Spark:
1. **DB service + schema** (1.1–1.8): selection criteria, paper study, benchmark yardstick, hands-on bake-off, decision memo, deploy service, schema v0, retention policy. *(Study portion effectively DONE — see §8.)*
2. **Offline data files input** (2.1–2.7): sample files (Profile FW engineer's + self-generated fio/eBPF — never blocked), formats list, record mapping, loader CLI (validating, idempotent re-load), synthetic file generator (doubles as benchmark instrument), loader perf numbers, folder-hierarchy contract.
3. **Time-series analysis** (3.1–3.5): query catalog v0, aggregates verified against fio's own summary, run/window comparison, billion-row scale test, ad-hoc how-to.
4. **UI** (4.1–4.5): Grafana OSS + default screens only; two saved dashboards (block layer, SSD); 1-page guide.
5. **Streaming approach study — decision only** (5.1–5.5, team-added): requirements with real measured numbers, paper comparison (shipper/broker/direct/custom), 2–3-day time-boxed trial of the initial pick, two-lane design decision, **streaming approach decision memo** = first POC 2 input.

Success criteria + "Deferred to POC 2" section (streaming *implementation*, custom UI, multi-node deploy) are in the doc. Working approach agreed: **two tracks** — demo track (thin end-to-end slice on default stack, protected Week-2 milestone) + study track (time-boxed evaluations using the demo pipeline as test bench), connected by fixed seams/contracts.

**Post-DB-finalization action plan (given in session, ClickHouse-concrete, 4 phases):**
- Phase 1: deploy ClickHouse dev instance (scripted), prep data-gen SUT, first real sample files.
- Phase 2 (hard-to-change decisions): schema v0 — table-per-layer; **`ORDER BY (run_id, sut_id, timestamp)`; `PARTITION BY run_id`** (partition-per-run → idempotent reload = drop partition); codecs (DoubleDelta ts, Delta+ZSTD, LowCardinality strings); rollup MVs into AggregatingMergeTree with **min/max/quantileTDigestState** (shape-preserving rule); TTL/storage policy; **file contract + run_id convention agreed with team**.
- Phase 3: generator → loader CLI (clickhouse-connect, ≥100K-row batches) → query catalog (must include one **ASOF JOIN** and one **`lttb()`** query) → Grafana.
- Phase 4: billion-row scale test, volume projection memo, ops basics (DDL in git, health dashboard from system tables, BACKUP tested), hand schema+file contract to Profile FW engineer.

**Other docs:** `POC1_Activities.md` = all-three-frameworks split (Profiling P1–P9, Analysis, Use-case U1–U8, contracts section, integrated Spark demo). ⚠️ **Its Analysis FW section is STALE** (pre-boss-list AN1–AN13) — sync or trim before circulating.

## 6. POC 1 ↔ boss communication notes

- Boss's high-level list is offline-only; the hard production problem (streaming, never-block) moved to POC 2 — flagged to user as worth an explicit conversation; Activity 5 (decision-only study) was added as the bridge so POC 2 starts building on day one.
- Docs for seniors: every detail traceable to the boss's 4 items; "Deferred to POC 2" section prevents absence-reads-as-oversight; team-added items labeled as such.

## 7. Downsampling / visualization knowledge (established)

- **Upsampling cannot recover downsampled detail** — design so detail still exists: (a) multi-resolution rollup tiers (raw/1s/1m/1h) with Grafana `$__interval` routing ("map tiles"); (b) tiered retention — demote raw to cold storage instead of deleting (`TTL … TO VOLUME`); (c) replay as last resort.
- **Two layers of downsampling:** storage rollups must carry **min/max/percentile sketches, never averages alone** (data honesty — spikes survive); display downsampling (**LTTB/M4**) computed at query time (chart honesty — spikes visible in 2000 pixels). LTTB can't be fully pre-stored (output depends on zoom resolution).
- Verified support: **ClickHouse has native `largestTriangleThreeBuckets`/`lttb`** (since 23.10); TimescaleDB toolkit has `lttb()`; IoTDB has native M4-LSM; QuestDB/GreptimeDB/others: none (open feature request for QuestDB). **M4 (min/max/first/last per bucket) works in plain SQL on ANY database** — universal fallback.
- Timestamp indexing: TSDBs physically organize by time rather than B-tree-index it. ClickHouse: **nothing is automatic — the `ORDER BY`/`PARTITION BY` choice is ours and is the single most important schema decision** (GreptimeDB forces a TIME INDEX; QuestDB designated timestamp; etc.).

## 8. Database study — results & reconciliation with the DB-selection session

**Status: ClickHouse OSS finalized for POC 1** (per `CONTEXT-Database-Selection-Session.md`; this session's independent study converged on the same answer).

This session's docs: **`DB_Study_TimeSeries_Comparison.md`** (criteria, tiered candidate list, feature matrices: lifecycle/query/ingest-ops, LTTB section, bake-off dimensions, sources) and **`TimeSeries_DB_Landscape.md`** (why each DB exists — five families; per-DB origin/implementation/read-write character; "every DB fossilizes its birth workload"; our data = web-analytics shape → event-OLAP family).
The other session's docs: `Time-Series-Databases-Guide.md`, `Time-Series-Database-Decision.md`, `ClickHouse-Database-Selection.md` (executive doc), plus `TimeSeries-Database-Research.md` / `Time-Series-Databases-Guide.md` (not reviewed in this session).

**Key verified facts (both sessions agree):** TimescaleDB multi-node deprecated; QuestDB replication Enterprise-only; InfluxDB 3 Core single-node + ~72h *per-query span* limit (configurable; not retention — the other session's phrasing is the more precise one); VictoriaMetrics = metrics-only (historical downsampling Enterprise); Prometheus = monitoring system, not an event store.

**⚠️ Reconciliation needed (differences between the two sessions):**
- This session shortlisted **GreptimeDB as bake-off challenger**; the DB-selection session **downgraded GreptimeDB** (auto-rebalance Enterprise-only; OSS HA weaker — single-leader regions, Kafka-dependent failover) and surfaced **StarRocks & Apache Doris** as the only engines meeting all hard constraints AND auto-rebalancing. → If a challenger bake-off still happens, consider StarRocks/Doris alongside or instead of GreptimeDB. Not yet merged into this session's docs.
- The DB-selection session's biggest open issue: **ClickHouse OSS does not auto-rebalance shards** — resolved via layered strategy (shard weights, loader-targeted writes, re-ingest from raw as escape hatch; `clickhouse-copier` is deprecated — don't use). Keep that §3 strategy; it's not duplicated here.
- That session assumed "ingest is batch, not real-time" from Frameworks.txt §117; THIS session's production brainstorm supersedes that for POC 2+: streaming ingest with tiered buffering is the target (batch loading remains the POC 1 mode and the replay/repair path).
- Pending fixes listed in that file's §6 (GreptimeDB glossary corrections, StarRocks/Doris additions, rebalancing section) are still open.

## 9. File inventory (d:\Frameworks)

| File | What it is |
|---|---|
| `Frameworks.txt` | Requirements source of truth (PM's doc) |
| `POC1_Analysis_FW_Activities.md` | **Current Analysis FW POC 1 plan** (boss's 4 items detailed + Activity 5) |
| `POC1_Activities.md` | Three-framework POC 1 split — ⚠️ Analysis section stale |
| `DB_Study_TimeSeries_Comparison.md` | This session's DB comparison (features, LTTB, bake-off dims) |
| `TimeSeries_DB_Landscape.md` | Why-each-DB-exists landscape (5 families, per-DB deep dive) |
| `CONTEXT-Database-Selection-Session.md` | **Companion context** — DB selection session (ClickHouse finalized; rebalancing strategy; verified corrections; StarRocks/Doris) |
| `ClickHouse-Database-Selection.md`, `Time-Series-Database-Decision.md`, `Time-Series-Databases-Guide.md`, `TimeSeries-Database-Research.md` | DB-selection session's docs (decision/executive/reference) |
| `CONTEXT-System-Analysis-FW-Session.md` | This file |

## 10. Where to pick up next

1. **User said: "I will come on these POC 1 activities"** — next concrete step is Phase 1 of the action plan (§5): ClickHouse deployment script + schema v0 DDL for review, then generator/loader.
2. Get answers to the three production sizing questions (§4): duty cycle, central retention, SUT scratch spec.
3. Optional doc hygiene: sync `POC1_Activities.md` Analysis section; merge StarRocks/Doris + GreptimeDB downgrades into `DB_Study_TimeSeries_Comparison.md`; complete the pending fixes in `CONTEXT-Database-Selection-Session.md` §6.
4. Offered but not yet requested: "Production Streaming Architecture" design note (the §4 brainstorm as a formal doc for seniors and POC 2 planning).
5. POC 2 seed exists: Activity 5's decision memo + the transport ladder (Vector default / gRPC gateway escape hatch / Kafka last resort) + forced-outage spill/drain trial.

## 11. Working style notes (how the user works — carry forward)

- Agile/POC mindset; low prior experience with this framework class — explanations valued, but wants to drive decisions himself ("my own brainstorming, clear picture in my mind").
- Asks sharp verification questions (ring-buffer backpressure, WAL cost, avg-downsampling shape loss) — answer with physics/numbers, concede valid points explicitly, correct wrong premises directly.
- Docs are for senior, non-specialist review: trace details to high-level items, mark team-added scope, "deferred" sections to prevent misreading, simple-language point lists preferred over dense tables when asked.
- Verify fast-moving facts (licenses, features) against primary sources before putting them in docs; label vendor benchmarks.
- English is second language — read intent generously; confirm understanding when phrasing is ambiguous.
