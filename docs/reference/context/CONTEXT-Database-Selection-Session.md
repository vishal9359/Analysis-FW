# Session Context — Time-Series Database Selection for the System Analysis Framework

*Purpose: hand-off/primer file. Load this at the start of a new session to restore full context on the database-selection work — the decision, the reasoning, the verified findings, the corrections made, and what is still pending.*
*Session date: July 2026 · Status: **ClickHouse OSS finalized for POC1; decision to be revisited after POC1.***

---

## 1. Project context

**The framework** (source of truth: `Frameworks.txt` in this folder): a **System Analysis Framework** that measures **SSD and host overheads** while running AI workloads on GPU-based systems (DGX Spark, DGX Workstation). It automates workload deployment, system/SSD configuration, execution, profiling, data collection, analysis, and UI.

- **Serves three projects:** On-Device AI, TraceVision, UVP Control.
- **Sub-frameworks:** Use-case Run & Deployment, Profile FW, Analysis FW, Tool Deployment, UVP Control.
- **The database is for the Analysis FW.**

**The data (this drives everything):**
1. **Per-IO trace events** (eBPF / blktrace) — the dominant volume. Millions of events/sec during a run. Fields: timestamp, PID, LBA, size, latency, queue, layer. **High-cardinality, irregular, event-shaped.**
2. **Periodic telemetry** — CPU/GPU/memory utilization, NVMe queue stats, SSD telemetry. Secondary volume.

**Critical architectural constraints (from Frameworks.txt):**
- **Profiling must not perturb the SUT** (§117-118) — collection must not steal CPU/IO from the system being measured, and must not write to the SSD under test (in-RAM vs on-disk vs remote).
- → **Log to files during a run, bulk-load into the DB afterwards.** Ingest is *batch*, not real-time.
- Raw logs kept in a **defined folder hierarchy with defined formats** (§110) → this is the reversibility safety net.
- Analysis queries are an **open P1 task** (§157) — *queries are not known in advance*.
- Analyses needed (§137): latency distributions, LBA hotness, IO burst detection, queue-depth stats, seq/random, per-process stats, page faults, cross-layer correlation (syscall ↔ VFS ↔ FS ↔ block ↔ NVMe ↔ SSD telemetry).
- Multiple SUTs (fan-in), long runs (days–weeks), parallel testing.

---

## 2. THE DECISION

> **ClickHouse (open-source, Apache-2.0, self-hosted) — finalized for POC1. To be revisited after POC1.**

**Deployment plan:** start **single-node + one replica** (HA via ReplicatedMergeTree + ClickHouse Keeper), Grafana for UI, Python (`clickhouse-connect`) for analysis. Kubernetes Operator when/if multi-node.

### The 4 binding requirements (why ClickHouse won)
1. **High-cardinality trace ingestion** (PIDs, LBAs, queues → millions of unique values)
2. **Cross-layer correlation (JOINs)** across a table per stack layer
3. **Ad-hoc queries** — no pre-indexing, because queries are unknown
4. **Completely free horizontal scaling** — no paid tier, no licence trap

**ClickHouse is the only engine meeting all four** *and* being battle-tested at scale (Cloudflare, Uber, Anthropic, OpenAI, Sentry — all observability workloads, i.e. our exact shape).

### Why the others lost (one line each)
- **Prometheus / VictoriaMetrics / Thanos / Mimir / M3DB** — metrics-only; cannot store trace *events*; no JOINs.
- **Druid / Pinot** — fast only for *known*, pre-indexed queries; weak JOINs. Fails ad-hoc (#3).
- **QuestDB** — fastest single-node ingest, but **clustering is paid** (no plans to open-source HA) → fails #4.
- **InfluxDB** — free tier single-node → fails #4. (v3 fixed cardinality; v1/v2 had the wall.)
- **TimescaleDB** — full SQL/JOINs, but **multi-node removed in v2.14 (2024)** → fails #4.
- **Elasticsearch** — free & auto-rebalances, but **weak JOINs**, 2–6× slower analytics, 12–19× more storage.
- **TDengine / IoTDB** — rigid device-schema model; restricted JOINs → wrong data shape.
- **CrateDB** — passes hard constraints but ✗ continuous queries, ✗ downsampling, ◐ perf.
- **GreptimeDB** — clears requirements on paper, **but see §4 corrections** (auto-rebalance is Enterprise; HA weaker). Was "closest alternative"; now downgraded.

---

## 3. ⚠️ THE OPEN ISSUE: ClickHouse does NOT auto-rebalance

**This is the biggest known weakness and was heavily challenged in this session. Do not lose this.**

**Fact (verified, official):** ClickHouse OSS does **not** automatically rebalance data when a new shard is added. Historical data stays on old shards; only new inserts go to the new shard.
→ [Rebalancing Data — ClickHouse Docs](https://clickhouse.com/docs/guides/sre/scaling-clusters) · [GitHub #33947](https://github.com/ClickHouse/ClickHouse/issues/33947)

**Why it does NOT block us (the reasoning that resolved the objection):**
- Our data is **append-only, time-partitioned, cold-tailed** — old runs are rarely queried, so "old data stays on old shards" is fine. Queries fan out to all shards anyway.
- **You CAN scale** — adding shards works. Only the *evenness of historical data* needs attention. ("We can't scale" was the incorrect part of the objection.)
- **Shard weights** = a config change, not a migration (official recipe: *"if 100 GB/day, configure 70 GB to the new shard, 10 GB to each existing"* → weights 7:1:1). Balance in days–weeks, **zero data moved**.
- **We keep raw logs** → a true reshard is a **loader run**, not a data migration. (Contentsquare — a large ClickHouse user — *rebuilt* rather than rebalanced in place.)

### The agreed layered rebalancing strategy

| Layer | Situation | Action | Effort |
|---|---|---|---|
| **0 — Prevent** | From day one | Single node + replica; **scale up before out**; partition by time; `ORDER BY (run_id, timestamp)`; **TTL** to bound growth | — |
| **1 — Normal growth (95%)** | Add a node | **Shard weights 7:1:1** | Config change, minutes; no data moved |
| **2 — Faster balance** | Need it sooner | Point loader at the **new shard only** for a period | Loader config |
| **3 — Escape hatch (rare)** | Changed sharding key / true reshard | **Re-ingest from raw logs** into a new cluster, cut over | A loader run |
| **4 — Surgical** | Move specific old partitions | `DETACH` → rsync → `ATTACH` | Hands-on, per partition |

⚠️ **`clickhouse-copier` is deprecated/removed** — not in official docs anymore. Don't build on it (Contentsquare's 2022 blog uses it).

---

## 4. 🔴 VERIFIED FINDINGS & CORRECTIONS (hard-won — do not re-litigate)

These were each researched and verified against primary sources during the session. Several contradict common AI answers.

| # | Finding | Status |
|---|---|---|
| 1 | **GreptimeDB auto-rebalance is ENTERPRISE-ONLY.** OSS is manual `migrate_region()`. *(An earlier claim in-session that GreptimeDB "auto-rebalances" was **WRONG** and corrected.)* | ✅ Verified — Greptime's own blog/docs |
| 2 | **GreptimeDB OSS HA is weaker than ClickHouse** — single-leader regions, **no serving replicas**, safe failover **requires Kafka** (local-WAL failover *"not recommended… may lead to data loss"*), RTO > 0. Wider stack (Frontend + Datanode + Metasrv + etcd/RDS + Kafka + object storage). | ✅ Verified — GreptimeDB docs |
| 3 | **StarRocks & Apache Doris** (Apache-2.0, columnar MPP) **auto-rebalance tablets** when a node is added, and **meet all 4 hard constraints**. They are the **only** engines that clear hard constraints AND auto-rebalance. Mature (Doris = Apache TLP, ex-Baidu Palo ~2017; StarRocks fork 2020; users incl. Airbnb, Pinterest, Tencent, iQIYI). **Found late in the session — genuine alternatives, not yet in the docs.** | ✅ Verified |
| 4 | **TimescaleDB → TigerData rebrand (June 2025) is REAL.** The *company* rebranded; the *extension* keeps the TimescaleDB name. *(ChatGPT/Gemini reviewers claimed this was false — they were wrong, likely training-cutoff.)* | ✅ Verified |
| 5 | **InfluxDB 3 Core "72 hours" is NOT a retention/age limit.** Any period can be written and queried; the limit is the **time span a *single query* can cover** (~432 Parquet files ≈ 72h, **configurable**). Enterprise's compactor removes it. | ✅ Verified — InfluxData's own blog |
| 6 | **Kafka table engine is NOT supported in ClickHouse Cloud** (ClickPipes is the recommended path). *(A reviewer called this "too absolute" — they were wrong; official docs are explicit.)* | ✅ Verified — ClickHouse docs |
| 7 | **Elasticsearch is open source again** — AGPLv3 added Sept 2024 (tri-licensed SSPL / AGPLv3 / Elastic License v2). | ✅ Verified — Elastic blog |
| 8 | **ClickHouse fan-in nuance:** async inserts handle "hundreds or thousands of clients," but this **mitigates ClickHouse's own small-insert weakness** ("too many parts") — it is **not a unique advantage**. On a **single node all engines are comparable**; the ✓/◐ split in fan-in really tracks **free horizontal scale-out** (overlaps Req #4). ClickHouse **ties** GreptimeDB/Elasticsearch here — it does not win. | ✅ Verified |
| 9 | **Benchmark scope discipline:** ~0.9–4M rows/s = **single node**; 400–500M rows/s = **50-node cluster**; Tinybird's "1B rows/s" headline = **50-machine cluster peak** (sustained 400–500M); Cloudflare's "6M requests/s" = *requests* on a 1,000+ replica cluster (~90M rows/s). **Never mix single-node and cluster numbers.** | ✅ Verified |
| 10 | **ClickHouse batch-size dependency:** ~**56× slower** with tiny inserts (batch 100: 250 ops/s vs TimescaleDB 14,200) but competitive at batch 10,000. **Our log-then-bulk-load pipeline lands in its sweet spot** — its one real ingest weakness never applies to us. | ✅ Verified |
| 11 | **"Scales out" — manual vs automatic sharding:** ClickHouse OSS = **explicit** sharding (you define topology + sharding key; no auto-rebalance). Elasticsearch = **automatic** (auto-places/rebalances shards). GreptimeDB OSS = **manual** (auto is Enterprise). ClickHouse **Cloud**'s SharedMergeTree makes it automatic (paid). | ✅ Verified |
| 12 | **High cardinality — how each engine pays:** ClickHouse/GreptimeDB = **native** (ordinary columns, no series explosion — *not* zero cost, just no "wall"); QuestDB/TimescaleDB = **RAM cost** (dictionary/index must fit in memory); Elasticsearch = **storage-heavy** (inverted index bloats disk+heap); Prometheus = **cardinality wall** (one series per unique label combo → OOM). | ✅ Verified |

---

## 5. Documents produced (state as of session end)

| File | Purpose | State |
|---|---|---|
| `Frameworks.txt` | **Source of truth** for framework requirements (pre-existing) | — |
| `Time-Series-Databases-Guide.md` | Full landscape + history reference ("backup knowledge" — seniors said keep it) | Complete; §12 has the "why not the faster DBs" rationale |
| `Time-Series-Database-Decision.md` | Detailed decision doc (matrices, OSS vs Cloud, adopters, benchmarks, references) | Complete |
| `ClickHouse-Database-Selection.md` | **Executive doc per the latest template** (Intro / 15-param comparison / ClickHouse overview+architecture+APIs+benchmarks+editions+adopters / deploy / references) | Complete |
| `CONTEXT-Database-Selection-Session.md` | This file | — |

---

## 6. ⚠️ PENDING / NOT YET DONE (pick up here)

1. **Fix the GreptimeDB glossary note** in `ClickHouse-Database-Selection.md` (§2 notes) and `Time-Series-Databases-Guide.md`: it currently says *"Elasticsearch and GreptimeDB use automatic distribution — the engine places shards and auto-rebalances."* → **True for Elasticsearch, FALSE for GreptimeDB OSS** (Enterprise-only). Must be corrected.
2. **GreptimeDB HA rating** should be downgraded from ✓ to **◐** wherever it appears (single-leader regions, Kafka dependency, RTO > 0).
3. **Add StarRocks & Apache Doris** to the comparison matrices — they are legitimate Apache-2.0 alternatives that auto-rebalance and meet all hard constraints. Currently absent from all docs.
4. **Add a "Cluster scaling & rebalancing strategy" section** (the layered plan in §3 above) to the decision doc — this was offered but not written.
5. **Framework requirements link placeholder** — `ClickHouse-Database-Selection.md` §2 has *"[link to Analysis Framework requirements page — to be added]"* (user to fill).
6. **Unverified cells flagged:** the auto-rebalance ratings for **Pinot** (triggered rebalance), **TDengine**, and **IoTDB** are assessments, less firmly verified than the others.
7. **Unverified claim:** the "~364× WAL replay amplification" figure for GreptimeDB (from a Claude-UI-generated doc) was not confirmed.

---

## 7. Revisit triggers (post-POC1)

| If… | Then |
|---|---|
| **Auto-rebalance becomes a top-3 requirement** | **Bake-off StarRocks / Apache Doris** — Apache-2.0, mature, auto-rebalance, clear all hard constraints. *Strongest alternative.* |
| Single-node ingest becomes the bottleneck | Bake-off QuestDB (but remember: clustering is paid → caps at one node) |
| Want auto-rebalance + willing to pay | **ClickHouse Cloud** (SharedMergeTree = automatic; same engine/SQL, drop-in) |
| ~2 years out | Re-evaluate **GreptimeDB** — *only if* the Region Balancer is open-sourced and it matures |
| Old trace data becomes *hot* | Revisit sharding — this is the one condition that changes the rebalance math |
| Live dashboards during days-long runs needed | Consider **VictoriaMetrics alongside** ClickHouse for the periodic metrics only (never for trace events) |

**Always:** the only test that truly decides anything is **`clickhouse-benchmark` on our own trace data on our own box**, using the ~10 queries from `Frameworks.txt` §137. Published benchmarks are directional only — every vendor wins its own.

---

## 8. Key references

**Official ClickHouse**
- Architecture overview — https://clickhouse.com/docs/development/architecture
- Rebalancing data (the 4 methods) — https://clickhouse.com/docs/guides/sre/scaling-clusters
- Shards & replicas — https://clickhouse.com/docs/shards
- Async inserts (fan-in) — https://clickhouse.com/docs/optimize/asynchronous-inserts
- Interfaces / APIs — https://clickhouse.com/docs/interfaces/overview
- Kubernetes Operator — https://github.com/ClickHouse/clickhouse-operator
- Adopters — https://clickhouse.com/docs/about-us/adopters
- ClickBench — https://benchmark.clickhouse.com/

**Production evidence**
- Cloudflare (hundreds of millions rows/s) — https://clickhouse.com/blog/how-cloudflare-processes-hundreds-of-millions-of-rows-per-second-with-clickhouse
- Anthropic observability — https://clickhouse.com/blog/how-anthropic-is-using-clickhouse-to-scale-observability-for-ai-era
- OpenAI petabyte-scale — https://clickhouse.com/blog/why-openai-uses-clickhouse-for-petabyte-scale-observability
- Contentsquare scale-out (real reshard) — https://engineering.contentsquare.com/2022/scaling-out-clickhouse-cluster/

**Alternatives / corrections**
- GreptimeDB region migration (Enterprise balancer) — https://www.greptime.com/blogs/2024-03-15-region-migration
- GreptimeDB region failover (Kafka requirement) — https://docs.greptime.com/user-guide/deployments-administration/manage-data/region-failover/
- Doris tablet repair & balance — https://doris.apache.org/docs/3.x/admin-manual/maint-monitor/tablet-repair-and-balance/
- StarRocks vs ClickHouse — https://www.starrocks.io/blog/clickhouse_or_starrocks
- Elasticsearch open source again (AGPL) — https://www.elastic.co/blog/elasticsearch-is-open-source-again
- TimescaleDB 2.14 (multi-node removed) — https://github.com/timescale/timescaledb/releases/tag/2.14.0
- InfluxDB 3 Core 72h explained — https://www.influxdata.com/blog/influxdb3-open-source-public-alpha-jan-27/

**Benchmarks (vendor-biased — always note the publisher)**
- TSBS — https://github.com/timescale/tsbs
- KX TSBS comparison — https://kx.com/blog/benchmarking-kdb-x-vs-questdb-clickhouse-timescaledb-and-influxdb-with-tsbs/
- Neutral aggregator — https://www.timestored.com/data/time-series-database-benchmarks

---

## 9. Working notes (how this work has been reviewed)

- **Every load-bearing claim must have a reference link.** Vendor-published figures must be labelled with the publisher and treated as directional.
- **AI-generated review comments (ChatGPT/Gemini/Claude UI) have been wrong multiple times** in this session (see §4 items 4, 6). **Always verify against primary sources before accepting a correction.**
- **Terse table cells get challenged** — every symbol/qualifier needs a plain-English glossary entry keyed to its row.
- Prefer **honest "it ties / we're not uniquely better"** over overselling ClickHouse; the reviewers spot inflation (e.g. the fan-in Row 9 correction).
- Documents are read by **senior, non-specialist** audiences — lead with the decision, use tables, explain jargon.
