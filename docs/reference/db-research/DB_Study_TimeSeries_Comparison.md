# Time-Series Database Study — Comparison & Shortlist (Activity 1.2)

**Use case:** store per-IO profiling events (very high volume, high cardinality: pid, LBA, queue id…) plus SSD telemetry and system metrics; query by time range; feed Grafana.

**Selection criteria:**
1. Very high volume ingest (target: millions of events/sec across SUTs)
2. Fast time-range queries (seconds on billions of rows)
3. Free / open-source license (no paid tier required for core needs)
4. Grafana support
5. Scalable — multi-node support **in the free version**

**Key insight up front:** our per-IO data is *event/analytical* data (wide rows, many high-cardinality fields), not classic *metrics* data (numeric samples with few labels). Classic metrics TSDBs (Prometheus family, VictoriaMetrics, InfluxDB) are built for the second shape and break on the first. The right category is **columnar event/OLAP stores with time-series features**.

---

## Full candidate list (free/open-source landscape)

### Tier 1 — meets or nearly meets all five criteria (bake-off candidates)

| DB | License | Multi-node (free) | Ingest | Time-range queries | Grafana | Notes |
|---|---|---|---|---|---|---|
| **ClickHouse** | Apache 2.0 | Yes (sharding + replication, ClickHouse Keeper) | Excellent — millions of rows/sec/node | Excellent (time-ordered MergeTree keys, TS codecs: Delta/DoubleDelta/Gorilla + zstd) | Official plugin, first-class | De-facto engine behind modern observability platforms (SigNoz, Uber logs, Cloudflare analytics). Handles high-cardinality event rows natively. Materialized views for rollups; TTL tiering hot→cold; single-binary ops. |
| **GreptimeDB** | Apache 2.0 | Yes — full distributed system is open source | High (purpose-built for observability events) | Good (SQL + PromQL) | Yes (plugin + MySQL/PG protocol) | "Observability 2.0" unified events/metrics/logs DB. Modern architecture (object-storage-native, Flow engine for continuous aggregation). Young project (~v1.0 2025) — less production hardening. |
| **Apache Druid** | Apache 2.0 | Yes — native distributed | Very high (built for real-time event streams) | Very good (time-partitioned segments) | Yes (plugin) | Battle-tested at huge scale. Cost: operationally heavy for a small team (multiple node roles + ZooKeeper + deep storage); rollup-centric; ad-hoc SQL weaker than ClickHouse. |
| **Apache Pinot** | Apache 2.0 | Yes | Very high | Very good | Community plugin | Similar class to Druid (real-time OLAP). Ops-heavy; Grafana integration less mature. |
| **TDengine** | AGPL 3.0 (cluster included since 3.0) | Yes | Very high for IoT-shaped data | Good | Yes (plugin) | "Super table" model = many devices × regular metric samples; awkward for irregular high-cardinality IO event streams. AGPL fine for internal use. |
| **Apache IoTDB** | Apache 2.0 | Yes | Very high (industrial telemetry) | Good | Yes (connector) | Same data-model caveat as TDengine: built for device telemetry, not analytical event rows. |

### Tier 2 — strong databases that fail at least one criterion (verified July 2026)

| DB | Fails on | Detail |
|---|---|---|
| **TimescaleDB** | Multi-node | Multi-node support **deprecated** — TimescaleDB 2.13 was the last version with it; core is Apache 2.0, Community features under TSL (company now TigerData). Also ~10× lower ingest ceiling than columnar stores. |
| **QuestDB** | Multi-node in free version | Superb single-node ingest and open source (Apache 2.0), but **replication/HA is Enterprise-only** with no plan to open-source it. |
| **InfluxDB 3 Core** | Multi-node + long time ranges | Open source (MIT/Apache 2.0) but single-node and **designed for short query ranges (~72-hour window by default; the compactor that enables long ranges is Enterprise-only)**. Disqualifying for week-long run analysis. |
| **VictoriaMetrics** | Data model | Cluster version is genuinely open source (Apache 2.0) and excellent — but it is a *metrics* store (samples + labels). Per-IO fields (pid, LBA, cid) as labels = cardinality explosion. Viable only as a metrics sidecar, not the event store. |
| **DuckDB + Parquet** | Not a service, no multi-node | Excellent as a cheap offline-analysis baseline and possible cold-storage query layer; not the primary DB. |

### Tier 3 — ruled out quickly

- **Prometheus (+ Thanos / Mimir / Cortex)** — metrics samples only, pull model, cardinality limits; Mimir is AGPL. Not an event store. (Prometheus's only role for us: optional metrics sidecar — separate decision, Activity 1.2/12.)
- **OpenTSDB** — legacy, HBase dependency, effectively dormant.
- **M3DB** — metrics-oriented; development slowed since Uber stepped back; ops-heavy.
- **Cassandra / ScyllaDB** — massive write scaling but no columnar analytics: every query pattern must be pre-designed; ad-hoc time-range aggregations are poor. ScyllaDB moved to source-available.
- **Elasticsearch / OpenSearch** — document-oriented; heavy storage overhead and lower ingest efficiency for numeric event firehoses.
- **HoraeDB (Apache incubating)** — too early/immature.
- **kdb+, DolphinDB** — proprietary/paid; fail the license criterion.

---

## Shortlist for hands-on benchmark (Activity 1.4)

1. **ClickHouse — primary hypothesis.** Only candidate that is simultaneously: proven at our data shape and scale, Apache 2.0 with free multi-node, top-tier Grafana support, and operable by a 1-person team (single binary). The wider industry converging on it for exactly this workload (per-event observability stores) is strong prior evidence.
2. **GreptimeDB — challenger.** Fully-free distributed system purpose-built for the events+metrics unified model; validates whether the modern purpose-built option beats the general columnar engine. Risk to test: maturity under sustained firehose load.
3. **Fallback challenger if GreptimeDB fails early: Apache Druid** (battle-tested but ops-heavy — only worth benchmarking if GreptimeDB disappoints).

**Benchmark yardstick (from Activity 1.3):** sustained ingest (events/sec/node), disk bytes per million events, the 6–8 defined time-range queries on billion-row data, query-under-ingest, TTL/retention behavior.

---

## Feature comparison of all candidates

**Why each feature matters to us:**

| Feature | Why we need it |
|---|---|
| Retention / TTL | Auto-expire raw per-IO events after N days while keeping aggregates for months — our main volume-control lever |
| Continuous queries / continuous aggregation | Per-second rollups (IOPS, latency percentiles, QD) computed automatically as data arrives — feeds live Grafana and long-term trends without re-scanning raw events |
| Downsampling of old data | Old runs shrink to coarse resolution automatically instead of being deleted |
| Storage tiering | Hot recent data on local NVMe, old runs on cheap/object storage — needed for week-long runs × 12 SUTs |
| Time-series SQL (ASOF join, gap-fill, sampling) | ASOF join = correlate an IO event with the nearest-in-time GPU/CPU sample — the core "system analysis" query shape |
| File import (CSV/Parquet) | Our POC 1 input is offline data files — the loader depends on this |
| Insert dedup / idempotent load | Safe re-load of the same run's files (loader requirement 2.4) and at-least-once streaming later |

### A. Data lifecycle features

| DB | Retention / TTL | Continuous aggregation | Downsampling old data | Storage tiering |
|---|---|---|---|---|
| **ClickHouse** | Table **and column** TTL; TTL actions: delete, move, or **`TTL … GROUP BY` (rollup-on-expiry: raw events auto-collapse into aggregates as they age)** | Incremental materialized views (update on every insert) + refreshable MVs + aggregating engines | Yes — MVs + TTL GROUP BY | Storage policies: hot/cold volumes, S3-backed disks, TTL-driven moves — all free |
| **GreptimeDB** | Table/DB-level TTL | **Flow engine** — continuous incremental aggregation over streams (in OSS) | Via Flow | Object-storage-native by design (S3/GCS/Azure), free |
| **Apache Druid** | Automated load/drop rules per time interval | Ingest-time rollup (pre-aggregation); no general MVs | Auto-compaction can re-granularize old segments | Hot/cold historical node tiers + deep storage (S3/HDFS), free |
| **Apache Pinot** | Per-table retention manager | Ingest-time aggregation + star-tree pre-aggregation index; merge-rollup minion tasks | Via minion merge-rollup | Tiered storage config, free |
| **TDengine** | `KEEP` per database (auto expiry) | Streams (continuous processing) + SMA pre-aggregates | Built-in multi-level `RETENTIONS` with auto rollup | Multi-level storage (different disks per data age), free |
| **Apache IoTDB** | TTL per database | Continuous queries (CQ) | CQ + GROUP BY time | Multi-dir storage, free |
| **TimescaleDB** | Automated retention policies (drop old chunks) | **Continuous Aggregates** — most polished UX of all (materialized + real-time combination) | Cont. aggregates + raw retention | S3 tiering is Timescale **Cloud only** |
| **QuestDB** | TTL (partition-based drop), on tables and views | Incremental **materialized views** (added 2025; own TTL, replication-aware) | SAMPLE BY + mat views | Parquet conversion for cold data; native compression limited (filesystem-level) |
| **InfluxDB 3 Core** | Retention settings, but ~72h practical query window | Processing engine (embedded Python plugins); classic CQs gone | Via plugins (manual) | Parquet/object storage design, but compactor = Enterprise |
| **VictoriaMetrics** | Single global retention (multiple retentions = Enterprise) | Recording rules + **stream aggregation at ingest (free)** | **Historical downsampling = Enterprise-only** | — |
| **DuckDB + Parquet** | Manual file management | None (batch tool, not a service) | Manual re-aggregation jobs | Files anywhere (cheap by nature) |

### B. Query capabilities

| DB | Language | Time-series extensions | Joins (events × metrics correlation) |
|---|---|---|---|
| **ClickHouse** | Rich SQL (quantiles, histograms, window functions, arrays) | **ASOF JOIN**, `WITH FILL` gap-fill, time bucketing | Full joins incl. ASOF — best fit for IO-event ↔ GPU-metric correlation |
| **GreptimeDB** | SQL (DataFusion) **+ PromQL** | Range/align queries | Standard SQL joins |
| **Apache Druid** | Druid SQL | Time granularity native | Limited (broadcast/lookup joins) |
| **Apache Pinot** | SQL (multistage engine) | Time bucketing | Limited, improving |
| **TDengine** | SQL + TS extensions | `INTERVAL`, `FILL` | Limited (within super-table model) |
| **Apache IoTDB** | IoTDB-SQL (own dialect) | GROUP BY time | Weak |
| **TimescaleDB** | **Full PostgreSQL SQL** (richest ecosystem) | time_bucket, gap-fill hyperfunctions | Full PG joins |
| **QuestDB** | SQL with best-in-class TS syntax | **SAMPLE BY, LATEST ON, native ASOF JOIN** | ASOF + standard joins |
| **InfluxDB 3 Core** | SQL + InfluxQL (FlightSQL) | Basic | Basic |
| **VictoriaMetrics** | MetricsQL (PromQL superset) — **no SQL** | PromQL-style | No real joins |
| **DuckDB + Parquet** | Excellent SQL incl. ASOF JOIN | Good | Full |

### C. Ingest, operations, integration

| DB | Ingest paths (bold = our offline-file need) | Insert dedup / idempotent load | Grafana | Free multi-node | Ops complexity |
|---|---|---|---|---|---|
| **ClickHouse** | HTTP, native, Kafka engine, async inserts; **direct CSV/JSON/Parquet/Arrow file ingest** | **Automatic insert-block dedup** (replicated tables) + ReplacingMergeTree | Official plugin | **Yes** | Low-medium (single binary; Keeper for cluster) |
| **GreptimeDB** | gRPC, HTTP, InfluxDB line protocol, Prometheus remote write, OTLP; **`COPY FROM` CSV/Parquet** | Primary-key upsert semantics | Official plugin + MySQL/PG wire | **Yes** | Medium (3 component types in cluster; single-binary standalone) |
| **Apache Druid** | Kafka (exactly-once), **batch from local/S3 (CSV/JSON/Parquet)** | Interval-replace batch loads (idempotent per time chunk) | Plugin | **Yes** | **High** (ZK + 5 node roles + deep storage + metadata DB) |
| **Apache Pinot** | Kafka realtime, **batch Parquet/CSV** | Upsert tables | Community plugin | **Yes** | **High** (ZK + 4 node roles) |
| **TDengine** | Native, InfluxDB/OpenTSDB line protocols, **CSV import** | Timestamp-keyed upsert | Official plugin | **Yes** (AGPL) | Medium |
| **Apache IoTDB** | Session API, MQTT, **CSV import**, Kafka connectors | Timestamp overwrite | Connector | **Yes** | Medium |
| **TimescaleDB** | SQL `COPY`/inserts (**CSV**) | PG unique constraints/upsert | Native PG datasource | **No** (deprecated) | Low (it's Postgres) |
| **QuestDB** | ILP (very fast), HTTP, PG wire, **CSV import** | Dedup on designated timestamp + keys | Official plugin + PG | **No** (Enterprise) | Very low (single binary) |
| **InfluxDB 3 Core** | Line protocol, HTTP | Last-write-wins | Plugin | **No** (Enterprise) | Low |
| **VictoriaMetrics** | Prometheus remote write + many protocols | Last-write-wins samples | Prometheus datasource | **Yes** | Low |
| **DuckDB + Parquet** | **Files (Parquet/CSV) natively** | N/A (files) | No direct datasource | No | Trivial (embedded) |

### Shape-preserving downsampling (LTTB / M4) — verified July 2026

Average-based rollups flatten spikes — a 2-second latency spike vanishes from a week-view chart. Two families of shape-preserving algorithms fix this at **query/display time** (reduce millions of points to the ~2000 a chart can show, keeping its visual shape):

- **LTTB (Largest-Triangle-Three-Buckets):** picks *real data points* that best preserve the line's visual shape; excellent at keeping local minima/maxima (spikes). Variant: MinMaxLTTB (faster at scale).
- **M4:** keeps min, max, first, last per pixel-column bucket — provably pixel-perfect for line charts, and **expressible in plain SQL on any database** (`min/max/argMin/argMax` per time bucket), so it's our universal fallback.

| DB | Native shape-preserving support |
|---|---|
| **ClickHouse** | ✅ **Native `largestTriangleThreeBuckets(n)(x,y)` aggregate (alias `lttb`)** since v23.10; can even be chained in materialized views. M4 trivial in SQL. |
| **TimescaleDB** | ✅ `lttb()` hyperfunction in timescaledb-toolkit (+ `asap_smooth`). |
| **Apache IoTDB** | ✅ Native **M4-LSM operator** (pixel-perfect visualization, published research) + sampling UDF library. |
| **QuestDB** | ❌ Open feature request (#2912); only min/max/avg via SAMPLE BY (min/max ⇒ manual M4 possible). |
| **GreptimeDB** | ❌ No LTTB/M4 found; M4-in-SQL possible manually. |
| **Druid / Pinot / TDengine / InfluxDB 3 / VictoriaMetrics** | ❌ No native LTTB; min/max aggregations allow manual M4-style queries. |
| **DuckDB** | Community LTTB extension; full SQL M4. |

**Design rule this adds (two layers of downsampling):**
1. **Storage rollups** (continuous aggregation) must carry min/max/percentile-sketches — never averages alone — so zoomed-out views still show that a spike exists.
2. **Display downsampling** (LTTB/M4) is computed at query time from raw/fine data for whatever window the user views — it cannot be fully pre-stored because its output depends on the target resolution (zoom level).

Sources: [ClickHouse lttb docs](https://clickhouse.com/docs/sql-reference/aggregate-functions/reference/largestTriangleThreeBuckets), [ClickHouse 23.10 release](https://clickhouse.com/blog/clickhouse-release-23-10), [MV chaining discussion](https://github.com/ClickHouse/ClickHouse/discussions/62849), [timescaledb-toolkit lttb](https://github.com/timescale/timescaledb-toolkit/blob/main/docs/lttb.md), [QuestDB feature request #2912](https://github.com/questdb/questdb/issues/2912), [IoTDB M4-LSM paper (SIGMOD)](https://dl.acm.org/doi/10.1145/3639290), [MinMaxLTTB/tsdownsample](https://arxiv.org/html/2307.05389)

### Additional comparison dimensions (to evaluate during the bake-off)

Beyond the tables above, these matter for our use case and should be scored on the two shortlisted DBs during Activity 1.4:

| Dimension | What to check | Early notes |
|---|---|---|
| High-cardinality handling | Millions of distinct pids/LBAs/cids — performance must not depend on distinct-value counts | Columnar stores (ClickHouse, Greptime) are indifferent by design; metric stores fail here |
| Compression efficiency | Measured bytes per event on disk with realistic data | Benchmark output, not a spec-sheet claim |
| Schema evolution | Cost of adding new columns as Profile FW grows (ALTER on TB-scale tables) | ClickHouse: cheap metadata-only ALTER ADD COLUMN; verify GreptimeDB |
| Update / delete of bad data | Re-ingest a run after a parser bug; remove poisoned data | ClickHouse mutations are heavyweight (prefer drop-partition-and-reload — design run→partition mapping accordingly) |
| Query concurrency + resource limits | Live dashboards + engineers' ad-hoc SQL + full-rate ingest simultaneously; per-user quotas | ClickHouse has quotas/settings profiles; verify GreptimeDB |
| Backup / restore | Snapshot a run's data; restore after node loss | |
| Python client quality | Analysis FW code will drive the DB from Python | clickhouse-connect is mature; verify GreptimeDB clients |
| DB self-monitoring | Ingest lag, merge/compaction backlog, disk use visible to Grafana | ClickHouse system tables; Greptime Prometheus endpoints |
| Community / docs maturity | Issue response, release cadence, production war stories | ClickHouse very mature; GreptimeDB young — this is its main risk |

### What the feature comparison changes

- **ClickHouse's lead widens.** It is the only candidate combining: rollup-on-expiry TTL (`TTL … GROUP BY` — an automatic aggregate-first pipeline in one clause), incremental MVs, ASOF JOIN for event↔metric correlation, direct Parquet/CSV ingest for our offline loader, automatic insert dedup for safe re-loads, free multi-node, and low ops burden.
- **TimescaleDB has the single nicest continuous-aggregation UX** and full Postgres SQL — a genuine loss — but no multi-node kills it for our scale regardless of features.
- **QuestDB is now surprisingly feature-complete** (TTL, incremental mat views, native ASOF JOIN, dedup) and the easiest to operate; still excluded by Enterprise-only multi-node, but worth remembering if a single-node edge deployment (e.g. on-SUT local analysis) ever becomes a requirement.
- **GreptimeDB remains the right challenger**: its Flow engine + object-storage-native design cover our lifecycle needs on paper; the bake-off must prove maturity under firehose load.
- **Druid/Pinot** are feature-capable but their ingest-time-rollup orientation (raw detail partially sacrificed at ingest unless configured carefully) plus heavy ops make them fallbacks only.
- **VictoriaMetrics** loses its last consideration as a main store: even downsampling of stored history is paywalled.

## Facts verified via web (July 2026) — sources

- InfluxDB 3 Core query-range limitation and Core-vs-Enterprise split: [InfluxData blog](https://www.influxdata.com/blog/influxdb3-open-source-public-alpha-jan-27/), [Which InfluxDB 3 should I use](https://docs.influxdata.com/influxdb3/which-influxdb-3/), [InfoQ — InfluxDB 3 OSS GA](https://www.infoq.com/news/2025/04/influxdb3-open-source/)
- TimescaleDB multi-node deprecation and licensing: [MultiNodeDeprecation.md (timescale/timescaledb)](https://github.com/timescale/timescaledb/blob/main/docs/MultiNodeDeprecation.md), [TigerData license page](https://www.tigerdata.com/legal/licenses), [TimescaleDB editions](https://www.tigerdata.com/docs/get-started/choose-your-path/timescaledb-editions)
- QuestDB replication Enterprise-only: [QuestDB Enterprise](https://questdb.com/enterprise/), [QuestDB HA reads blog](https://questdb.com/blog/highly-available-reads-with-questdb/)
- GreptimeDB open-source distributed build and Apache 2.0 license: [GreptimeTeam/greptimedb (GitHub)](https://github.com/GreptimeTeam/greptimedb), [GreptimeDB vs Victoria Stack](https://greptime.com/compare/victoria-stack)
- VictoriaMetrics scope (metrics/Prometheus replacement): [VictoriaMetrics open source](https://victoriametrics.com/products/open-source/)
- QuestDB TTL and materialized views: [QuestDB TTL concept](https://questdb.com/docs/concepts/ttl/), [QuestDB materialized views](https://questdb.com/docs/concepts/materialized-views/), [QuestDB 2025 year in review](https://questdb.com/blog/questdb-2025-year-in-review/)
- VictoriaMetrics downsampling Enterprise-only, stream aggregation free: [VictoriaMetrics Enterprise features](https://docs.victoriametrics.com/victoriametrics/enterprise/), [Stream aggregation docs](https://docs.victoriametrics.com/victoriametrics/stream-aggregation/)
