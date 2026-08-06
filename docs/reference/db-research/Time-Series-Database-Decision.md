# Time-Series Database Selection — Decision Summary

*Selecting the analytical data store for the System Analysis Framework*
*Audience: Engineering leadership · Status: Decision recorded*

> **Bottom line:** We are adopting **ClickHouse (open-source, Apache-2.0)** as the analytical data store. It is the only database evaluated that meets all of our binding requirements at once — high-cardinality event ingestion, cross-layer correlation (JOINs), ad-hoc SQL, and completely free horizontal scaling — while being proven in production at very large scale.

---

## 1. Contents

1. [Contents](#1-contents)
2. [Introduction](#2-introduction)
   - [2.1 The three categories of time-series databases](#21-the-three-categories-of-time-series-databases)
   - [2.2 Columnar databases](#22-columnar-databases)
3. [Decision: ClickHouse](#3-decision-clickhouse)
4. [Supporting analysis](#4-supporting-analysis)
   - [4.1 Comparison matrix](#41-comparison-matrix)
   - [4.2 Requirements comparison matrix](#42-requirements-comparison-matrix)
   - [4.3 ClickHouse OSS vs ClickHouse Cloud](#43-clickhouse-oss-open-source-vs-clickhouse-cloud)
   - [4.4 Current adopters of ClickHouse](#44-current-adopters-of-clickhouse)
   - [4.5 Benchmarking](#45-benchmarking)
5. [References](#5-references)

---

## 2. Introduction

A **time-series database (TSDB)** is a database optimized for data where every record carries a timestamp and time is the primary axis of organization — for example, per-IO trace events, or CPU/GPU/SSD telemetry sampled over time. Such data arrives continuously at high volume, is written once and rarely edited, and is queried by ranges and aggregations rather than by single records. General-purpose databases (MySQL, PostgreSQL, Oracle) are tuned for the opposite pattern and do not scale to this workload.

This document records **which database we will use as the analytical data store** for the **System Analysis Framework** and the reasoning behind it. System Analysis framework collects high-volume host- and SSD-domain trace and telemetry data and must store it for overhead and behaviour analysis. The chosen database must fit the framework's requirements.

> **System Analysis Framework requirements:** *[link to Analysis Framework requirements page — to be added]*

In short, the framework needs a store that can: (1) ingest **high-cardinality trace events** (PIDs, LBAs, queues); (2) run **cross-layer correlation queries (JOINs)** across syscall → block → NVMe → SSD layers; (3) support **ad-hoc queries** that are not known in advance; and (4) **scale horizontally for free**, with no paid tier.

> **What is "cardinality"? (plain-English)** Cardinality is the number of *unique values* in a field. **Low cardinality:** a `country` field has ~200 possible values. **High cardinality:** a process ID (`PID`) or disk logical block address (`LBA`) can take *millions* of distinct values. This matters because some databases (Prometheus, InfluxDB v1) create a separate internal data series for *every unique combination* of label/tag values — so millions of unique PIDs or LBAs create millions of series and exhaust memory, a failure mode known as the **"cardinality wall."** Our per-IO trace data is inherently high-cardinality, so the store must handle it natively.

### 2.1 The three categories of time-series databases

Time-series databases fall into three broad categories, defined by the shape of the data and the questions asked:

| Category | Data it holds | Typical question | Examples |
|---|---|---|---|
| **Monitoring** | Health metrics (numbers + labels), sampled on a schedule | "Alert if CPU > 90%" | Prometheus, VictoriaMetrics |
| **IoT** | Uniform readings from a fixed fleet of identical devices | "This meter's current value" | TDengine, IoTDB |
| **Analytics** | Individual, high-variety **events**, kept in full detail | "Break down X by Y by Z, ad-hoc" | ClickHouse, Druid, Pinot |

Our workload — high-cardinality per-IO trace **events**, correlated across stack layers with unpredictable queries — is an **Analytics** workload. Ingestion is high and steady, but that alone does not classify the workload: all three categories ingest fast. What places us in Analytics is the combination of high cardinality, the need for JOINs, and ad-hoc querying. **This document therefore focuses on Analytics-class time-series databases.**

### 2.2 Columnar databases

Analytics-types engines are almost always **columnar**, and this is the key architectural idea behind the decision.

- **Row-oriented databases** (the traditional kind) store all fields of a record together. This is efficient for transactional work — insert, update, or fetch a *whole record* at a time.
- **Columnar databases** store each *column* together on disk. A query reads only the columns it needs (e.g. 3 of 50), so it moves far less data; and because a column holds many similar values, it **compresses 10× or more**; and values can be processed in bulk batches (vectorized/SIMD execution).

**Use case columnar solves:** analytical queries that scan **many rows but few columns** — aggregations, group-bys, and filters over millions-to-billions of records, returned in sub-second to seconds. This is exactly the shape of overhead analysis (latency distributions, LBA hotness, IO-burst detection, per-process statistics). The trade-off — columnar engines are poor at single-row updates and tiny inserts. This does not affect us, because System Analysis trace data is written bulk-loaded in batches.

---

## 3. Decision: ClickHouse

**We will use ClickHouse (open-source, self-hosted, Apache-2.0).**

**What ClickHouse is / what it solves.** ClickHouse is a general-purpose **columnar OLAP database** built for running arbitrary SQL over billions of events in sub-second time. It was created at Yandex.Metrica for exactly our type of problem: letting analysts ask *any* question over a massive, ever-growing stream of events, when the questions are not known in advance. It stores data as sorted, compressed, immutable columnar parts (the MergeTree engine), skips irrelevant data via sparse indexes, and executes queries vectorized across all CPU cores and cluster nodes.

**Scalability.** ClickHouse runs as a single binary on one machine and scales out — **for free** — to a sharded, replicated cluster. Published and production figures, with sources:

- **Single node:** hundreds of thousands to a few million rows/sec ingest when batched ([Tinybird — "1B rows/sec in ClickHouse"](https://www.tinybird.co/blog/1b-rows-per-second-clickhouse)).
- **Large production clusters:** Cloudflare runs 1,000+ ClickHouse replicas ingesting **hundreds of millions of rows/sec** (~90M inserted rows/sec) ([ClickHouse — How Cloudflare processes hundreds of millions of rows/sec](https://clickhouse.com/blog/how-cloudflare-processes-hundreds-of-millions-of-rows-per-second-with-clickhouse); original [Cloudflare — HTTP analytics at 6M requests/sec](https://blog.cloudflare.com/http-analytics-for-6m-requests-per-second-using-clickhouse/)).
- **Extreme scale:** **quadrillion-row** deployments ([Cloudflare at quadrillion-row scale](https://clickhouse.com/blog/cloudflare); [ClickHouse LogHouse](https://clickhouse.com/blog/a-quadrillion-rows-across-the-three-cloud-scaling-loghouse)) and AI-infrastructure observability at [Anthropic](https://clickhouse.com/blog/how-anthropic-is-using-clickhouse-to-scale-observability-for-ai-era).

Full clustering, sharding, and replication are included in the free Apache-2.0 release — unlike several competitors that paywall or have removed multi-node support.

**Why it fits our use case.**

| Requirement (from the framework) | How ClickHouse meets it |
|---|---|
| High-cardinality trace ingestion | Native; handles PID/LBA/queue cardinality without a "cardinality wall" |
| Cross-layer correlation (JOINs) | Full SQL including JOINs across a table per layer, plus run-metadata joins |
| Ad-hoc queries (no pre-indexing) | Full SQL; any question answerable without pre-declaring indexes or rollups |
| Free horizontal scaling | Apache-2.0; sharding + replication + Kubernetes operator, all free |
| Batch ingestion (log-then-load) | Bulk batched inserts are ClickHouse's strongest ingest mode |
| Visualization / tooling | First-class Grafana integration; mature ecosystem and Python clients |
| Compression / retention | ZSTD + specialized codecs (10×+); table-level TTL for retention |

**Reversibility (why this choice is low-risk).** In plain terms: *the raw trace logs are our permanent originals; the database is only a searchable copy built from them.* The Profile framework already stores every raw log in a defined folder hierarchy, and the database is loaded from those logs. If we ever needed to switch databases, we would simply re-run the loader to read the same raw logs into the new engine — nothing is lost, because the database never holds anything the logs don't.

*Is this really possible?* Yes — it is a standard, proven data-engineering pattern. In lakehouse ("medallion") architectures the raw layer is kept append-only as the single source of truth precisely so that derived tables can be rebuilt or reprocessed later when logic or tooling changes ([Databricks — What is medallion architecture](https://www.databricks.com/blog/what-is-medallion-architecture); [Microsoft Learn](https://learn.microsoft.com/en-us/azure/databricks/lakehouse/medallion)). Mechanically it is easy: ClickHouse (and its alternatives) can bulk-load directly from files and object storage ([ClickHouse S3/file ingestion](https://clickhouse.com/docs/integrations/s3)), so re-ingesting the same logs elsewhere is a loader script, not a data migration.

*The one condition:* this holds only if the raw logs are complete and retained — which the framework already mandates. The switching cost is re-writing the loader for the new engine (real, but bounded), so the decision is not a one-way door.

---

## 4. Supporting analysis

### 4.1 Comparison matrix

Databases evaluated, against the criteria that matter for our workload. **✓** = strong / yes · **◐** = partial / with caveats · **✗** = no / poor.

| Database | Category | Primary use case | Data model | Free multi-node | Full SQL + JOINs | Ad-hoc queries | Fit for our use case | Retention policies | Continuous queries | Downsampling (LTTB) |
|---|---|---|---|---|---|---|---|---|---|---|
| **ClickHouse** | Analytics (OLAP) | Event / metrics analytics | Columnar (MergeTree) | ✓ Apache-2.0 | ✓ | ✓ | ✓ **Chosen** | ✓ (TTL) | ✓ (mat. views) | ✓ (native `lttb`) |
| **QuestDB** | Analytics / high-ingest | Finance, metrics | Columnar, time-ordered | ✗ Paid (Enterprise) | ◐ (SQL + ASOF; limited) | ✓ | ✗ No free scale-out | ✓ (TTL) | ✓ (mat. views) | ◐ (SAMPLE BY, not LTTB) |
| **Prometheus** | Monitoring | Infra metrics + alerting | Per-series TSDB | ✗ By design | ✗ (PromQL, no JOINs) | ◐ (metrics only) | ✗ Metrics only, not events | ✓ | ✓ (recording rules) | ✗ |
| **InfluxDB** | Monitoring / general | DevOps & IoT metrics | TSM (v1) / Parquet (v3) | ✗ Paid | ◐ (SQL in v3) | ◐ | ✗ Single-node in free tier | ✓ | ✓ (tasks) | ◐ (not LTTB) |
| **TimescaleDB** | IoT / general SQL | Time-series + relational | Postgres rows + columnar | ✗ Removed (2024) | ✓ (it is Postgres) | ✓ | ✗ No free horizontal write scaling | ✓ (retention policies) | ✓ (continuous aggregates) | ✓ (`lttb` toolkit) |
| **GreptimeDB** | Analytics / unified | Metrics + logs + events | Columnar (Parquet on S3) | ✓ Apache-2.0 | ✓ (DataFusion) | ✓ | ◐ Viable, but young / less proven | ✓ (TTL) | ✓ (flows) | ◐ (not LTTB) |
| **Elasticsearch** | Search / logs | Full-text search, log search | Inverted index (Lucene) | ✓ AGPLv3 / SSPL | ◐ (weak JOINs) | ◐ | ✗ Costly storage, slow aggregations | ✓ (ILM) | ◐ (transforms) | ◐ (TSDS, not LTTB) |

*Downsampling (LTTB) column: ✓ = native LTTB function; ◐ = downsampling by time-bucket/average, not LTTB; ✗ = none. (LTTB — Largest-Triangle-Three-Buckets — is a downsampling method that preserves the visual shape of a graph.)*

**Reading of the matrix:** Monitoring engines (Prometheus, InfluxDB) fail on the data model — they store metrics, not high-cardinality events. QuestDB and TimescaleDB have the right query model but no free horizontal scaling. Elasticsearch is a search engine repurposed for analytics: it works, but uses far more storage and is much slower at aggregations. **GreptimeDB is the only close alternative** — it meets the requirements on paper, but is set aside on **maturity**, not capability (a few years old versus ClickHouse's decade-plus, with a smaller community and few production references at our scale). It is the primary candidate to re-evaluate in the future.

### 4.2 Requirements comparison matrix

Where 4.1 compares the databases by general capability, this matrix scores each database against the **framework's specific requirements**. **✓** = meets well · **◐** = partial / with caveats · **✗** = does not meet.

*Requirement notes: (5) **online batch** = efficient micro-batching of live inserts; (7) **offline load** = optimized bulk-loading of stored files (native / Parquet / CSV); (8) **online stream** = native streaming-source ingestion (e.g. Kafka); (9) **fan-in** = many concurrent producers writing to one store; (11) **low-effort multi-node** = least operational effort to stand up a multi-node cluster.*

| # | Requirement | ClickHouse | QuestDB | Prometheus | InfluxDB | TimescaleDB | GreptimeDB | Elasticsearch |
|---|---|---|---|---|---|---|---|---|
| 1 | High-cardinality trace ingestion | ✓ native | ◐ RAM cost | ✗ card. wall | ◐ v3 only | ◐ RAM cost | ✓ native | ◐ storage-heavy |
| 2 | Cross-layer correlation (JOINs) | ✓ full SQL | ◐ limited | ✗ none | ◐ limited | ✓ Postgres | ✓ SQL | ◐ weak |
| 3 | Ad-hoc queries (no pre-indexing) | ✓ | ✓ | ◐ metrics only | ◐ | ✓ | ✓ | ◐ query DSL |
| 4 | Free horizontal scaling | ✓ Apache-2.0 | ✗ paid | ✗ single-node | ✗ paid | ✗ removed | ✓ Apache-2.0 | ✓ AGPL |
| 5 | Online batch ingestion | ✓ async inserts | ✓ | ◐ pull/scrape | ✓ | ◐ | ✓ | ✓ bulk API |
| 6 | HA deployment (replication/failover) | ✓ Keeper | ◐ paid | ◐ dup instances | ◐ paid | ✓ Postgres HA | ✓ | ✓ native |
| 7 | Offline bulk file loading (optimized) | ✓ file/S3 fns | ✓ CSV/Parquet | ✗ | ◐ CSV | ✓ COPY | ✓ Parquet | ◐ via tooling |
| 8 | Native online-stream ingestion | ✓ Kafka engine | ✓ line protocol | ◐ remote-write | ✓ Telegraf | ◐ external | ✓ Kafka | ✓ Beats/Logstash |
| 9 | Multi-producer fan-in (concurrent writers) | ✓ scales out + async inserts | ◐ 1 free node | ◐ pull model | ◐ 1 free node | ◐ single primary | ✓ scales out | ✓ scales out |
| 10 | High-performance read/write | ✓ | ✓ (single node) | ◐ metrics only | ◐ | ◐ | ✓ | ◐ slow aggs |
| 11 | Low-effort multi-node deployment | ✓ K8s operator | ◐ MN paid | ◐ needs Thanos/Mimir | ◐ paid | ◐ PG operators | ◐ younger operator | ✓ mature ECK operator |

**Reading:** ClickHouse meets **every** requirement, and is ✓ on all the ones that are *hard constraints* for us — high cardinality (1), JOINs (2), free scaling (4), offline bulk load (7), and fan-in (9). **GreptimeDB** matches it on almost all — its only ◐ is low-effort multi-node deployment, reflecting younger tooling — consistent with its "viable, but less mature" status in 4.1. **Elasticsearch** scores well on scaling, HA, and deployment maturity but poorly on JOINs and analytical-read cost. The remaining engines each fail at least one hard constraint: **QuestDB / InfluxDB / TimescaleDB** on free scaling (4), and **Prometheus** on the event/JOIN data model (1, 2). This requirements view confirms the feature view above: only **ClickHouse and GreptimeDB clear every requirement, and ClickHouse leads on maturity and deployment tooling.**

**Terms used in the cells** *(reference for readers):*

- **Row 1 — how each engine pays for high cardinality:** every engine can *store* high-cardinality data except Prometheus; the qualifier names the *penalty*:
  - **✓ native** (ClickHouse, GreptimeDB) — stored as ordinary columns, so there is no series-explosion penalty (normal storage cost still applies, but no "wall"). [ClickHouse](https://clickhouse.com/resources/engineering/high-cardinality-slow-observability-challenge) · [GreptimeDB](https://greptime.com/blogs/2023-07-31-unveiling-high-cardinality)
  - **◐ RAM cost** (QuestDB, TimescaleDB) — works, but the symbol dictionary / B-tree index must fit in **memory**; millions of unique values consume significant RAM. [QuestDB Symbol](https://questdb.com/docs/concepts/symbol/) · [Timescale](https://medium.com/timescale/how-different-databases-handle-high-cardinality-data-a6e549ae704a)
  - **◐ storage-heavy** (Elasticsearch) — its inverted index swells on **disk and heap** as unique values grow. [Elastic Labs](https://www.elastic.co/search-labs/blog/time-series-data-elasticsearch-storage-wins)
  - **◐ v3 only** (InfluxDB) — the v3 columnar engine fixes cardinality; v1/v2 hit the wall.
  - **✗ card. wall** (Prometheus) — one series per unique label combination, so millions of unique values exhaust memory (the "cardinality wall" — see the callout in §2).
- **Row 3 — "query DSL":** Elasticsearch is queried mainly through a JSON-based **D**omain-**S**pecific **L**anguage rather than full SQL — flexible for search, but weaker for analytical and relational (JOIN) queries.
- **Row 5 — "async inserts":** a ClickHouse feature that buffers many small incoming inserts and writes them together as one efficient batch. **✓ does not mean other engines lack inserts** — every engine inserts data; the rating measures how efficiently it absorbs a high rate of *live* writes (for example, Elasticsearch does this through its "bulk API").
- **Row 6 — HA terms:** HA (High Availability) = staying up when a node fails, via replicas + automatic failover. "**Keeper**" = ClickHouse's built-in replication coordinator; "**Postgres HA**" = TimescaleDB inherits PostgreSQL's mature replication/failover tooling; "**native**" = HA built into the engine's core (Elasticsearch, GreptimeDB). Every ✓ option provides HA — the note only says *how* it is achieved.
- **Row 9 — important (read carefully):** **every** database here accepts concurrent writes. Fan-in has *two* parts: (a) how many concurrent producers **one node** can absorb, and (b) whether writes can be **spread across nodes** as producers grow. On part (a) all engines are broadly comparable — ClickHouse uses **async inserts** (the server buffers many small-insert clients and flushes them as batches) to match databases that handle small writes more natively; note this mitigates ClickHouse's own dislike of many small inserts, it is not a unique advantage. The ✓ vs ◐ in this row therefore really tracks part (b), **free horizontal scale-out** — which overlaps Requirement 4:
  - **✓ scales out** (ClickHouse via shards; GreptimeDB and Elasticsearch via native distribution) — producers are spread across nodes. These three are **comparable here — none is uniquely better** at fan-in.
  - **◐ 1 free node** (QuestDB, InfluxDB) — concurrent writes are fine, but the free version writes to a single node, so total fan-in capacity caps there.
  - **◐ single primary** (TimescaleDB) — all writes funnel through one PostgreSQL primary; no free write scale-out.
  - **◐ pull model** (Prometheus) — it *scrapes* data from producers rather than receiving pushed writes, so large push-based fan-in is not its model.

  *Sources: [ClickHouse async_insert docs](https://clickhouse.com/docs/optimize/asynchronous-inserts), [ClickHouse shards docs](https://clickhouse.com/docs/shards).*
- **"scales out via shards" vs "native distribution" (Rows 9 & 11):** all three ✓-for-scaling engines split data across nodes, but differ in *who manages the shards*. **ClickHouse (OSS)** uses **explicit sharding** — you define the cluster topology and the sharding key, and adding a node does **not** auto-move existing data (you plan the redistribution): more control, more operator effort. **Elasticsearch and GreptimeDB** use **automatic distribution** — the engine places shards and **auto-rebalances** when nodes are added or removed: less effort, less control. (ClickHouse *Cloud*'s SharedMergeTree makes this automatic too, but that is the paid tier — see 4.3.)

### 4.3 ClickHouse OSS (Open-source) vs ClickHouse Cloud

We are adopting **OSS (Open-source)**. It is the same engine as the paid Cloud service — Cloud does not run different SQL or query faster; the differences are operational.

| Area | ClickHouse OSS (self-hosted) — *our choice* | ClickHouse Cloud (managed, paid) |
|---|---|---|
| License / cost | Apache-2.0, free (pay only for own infra) | Consumption-based (compute + storage) |
| Core engine | Full columnar engine, full SQL | **Identical engine** |
| Storage / scaling | ReplicatedMergeTree; you design sharding | SharedMergeTree (proprietary): data on object storage, autoscaling |
| Operations | Self-managed (backups, upgrades, HA) | Fully managed |
| Compliance certs | Self-certify your deployment | SOC 2, ISO 27001, HIPAA, PCI (managed) |
| Deployment | Any cloud, on-prem, **air-gapped** | AWS / GCP / Azure (+ BYOC) |

For a self-hosted, potentially air-gapped analysis system, OSS is the correct fit: full engine, full performance, free clustering, and no loss of capability. The Cloud-only features (SharedMergeTree, autoscaling, managed compliance) address elastic multi-node cloud operations that are not relevant at our scale today.

### 4.4 Current adopters of ClickHouse

ClickHouse is proven for our exact workload class — high-volume machine-generated event/observability data — at major organizations:

| Use case | Adopters |
|---|---|
| Observability / logging (closest to our workload) | **Anthropic** (AI-infra observability), **Cloudflare** (DNS analytics, ~7M rows/s), **Uber** (logging), **eBay**, **Sentry** |
| Product / web analytics | **PostHog**, **Plausible**, **Contentsquare**, **Spotify** |
| Finance / high-volume | **Bloomberg**, **Deutsche Bank**, **Ahrefs** (~1 EB uncompressed) |

Its adopter base centres on exactly our use case (machine-generated events analysed with ad-hoc SQL), which lowers the risk of the choice. (Note: several figures above are vendor-reported; see [References](#5-references).)

### 4.5 Benchmarking

**Important caveat:** most database benchmarks are published by a vendor, and each vendor's benchmark favours its own product. The figures below are **directional**, drawn from multiple sources with the publisher noted; they are not a single controlled test.

**Write (ingest), single node — rows/sec (TSBS-style):**

| Database | Ingest rate | Source | Note |
|---|---|---|---|
| QuestDB | ~1.4M (peak) | [QuestDB](https://questdb.com/blog/clickhouse-vs-questdb-comparison/), [dev.to](https://dev.to/questdb/how-we-achieved-write-speeds-of-1-4-million-rows-per-second-1a9l) | Fastest single-node; capped at one free node |
| ClickHouse | ~0.9–4M | [Tinybird](https://www.tinybird.co/blog/1b-rows-per-second-clickhouse), [Cloudflare](https://clickhouse.com/blog/how-cloudflare-processes-hundreds-of-millions-of-rows-per-second-with-clickhouse) | Mid-upper; scales to **400M+/s on a free cluster** |
| GreptimeDB | > ClickHouse (log benchmark) | [Greptime](https://greptime.com/blogs/2024-08-22-log-benchmark) | Vendor-reported |
| InfluxDB | ~334K | [KX TSBS](https://kx.com/blog/benchmarking-kdb-x-vs-questdb-clickhouse-timescaledb-and-influxdb-with-tsbs/) | Collapses on high cardinality (~38K) |
| TimescaleDB | ~145K | [KX TSBS](https://kx.com/blog/benchmarking-kdb-x-vs-questdb-clickhouse-timescaledb-and-influxdb-with-tsbs/) | |
| Elasticsearch | Slowest, highest resource use | [Greptime](https://greptime.com/blogs/2024-08-22-log-benchmark), [ClickHouse](https://clickhouse.com/blog/elasticsearch-log-analytics-clickhouse) | Index-build overhead |

**Read / analytics & storage (log-analytics comparison):**

| Comparison | Result | Source (bias) |
|---|---|---|
| ClickHouse vs Elasticsearch | ClickHouse **2–6× faster** analytics, **12–19× less storage**, ~5× faster aggregations | [ClickHouse](https://clickhouse.com/blog/clickhouse_vs_elasticsearch_the_billion_row_matchup) *(ClickHouse)* |
| ClickHouse vs Druid / Pinot | Pinot faster for *known* pre-indexed queries; ClickHouse wins general ad-hoc SQL | [StarTree](https://startree.ai/resources/a-tale-of-three-real-time-olap-databases/) *(Pinot)* / [ClickBench](https://benchmark.clickhouse.com/) *(ClickHouse)* |
| ClickHouse vs TimescaleDB / InfluxDB | ClickHouse faster on analytical scans; ~15–30× compression | [sanj.dev](https://sanj.dev/post/clickhouse-timescaledb-influxdb-time-series-comparison/) *(independent)* |
| GreptimeDB vs ClickHouse | Roughly comparable reads; GreptimeDB better compression | [Greptime](https://greptime.com/blogs/2024-08-22-log-benchmark) *(GreptimeDB)* |

**Interpretation for our decision.** ClickHouse is **not** the single-node ingest champion (QuestDB and specialized engines are faster on uniform data), but this is not our bottleneck — we bulk-load from logs, and ClickHouse scales out for free far beyond any single QuestDB node. On the axes that match our workload — analytical query speed, storage efficiency, full-SQL flexibility, and free scaling — ClickHouse leads or ties. The only definitive test is a run on our own trace data, recommended during framework build-out.

---

## 5. References

**Official ClickHouse**
- ClickHouse adopters — https://clickhouse.com/docs/about-us/adopters
- ClickHouse Cloud architecture — https://clickhouse.com/docs/cloud/reference/architecture
- SharedMergeTree — https://clickhouse.com/docs/cloud/reference/shared-merge-tree
- Loading data from S3 / files (re-ingestion) — https://clickhouse.com/docs/integrations/s3
- Asynchronous inserts (fan-in from many clients) — https://clickhouse.com/docs/optimize/asynchronous-inserts
- Sharding & write scale-out — https://clickhouse.com/docs/shards
- ClickBench (benchmark dashboard) — https://benchmark.clickhouse.com/
- ClickHouse vs Elasticsearch (billion-row matchup) — https://clickhouse.com/blog/clickhouse_vs_elasticsearch_the_billion_row_matchup
- ClickHouse for log analytics vs Elasticsearch — https://clickhouse.com/blog/elasticsearch-log-analytics-clickhouse

**Scalability (production evidence)**
- Cloudflare — HTTP analytics at 6M requests/sec — https://blog.cloudflare.com/http-analytics-for-6m-requests-per-second-using-clickhouse/
- ClickHouse — How Cloudflare processes hundreds of millions of rows/sec — https://clickhouse.com/blog/how-cloudflare-processes-hundreds-of-millions-of-rows-per-second-with-clickhouse
- Cloudflare at quadrillion-row scale — https://clickhouse.com/blog/cloudflare
- ClickHouse LogHouse (quadrillion rows) — https://clickhouse.com/blog/a-quadrillion-rows-across-the-three-cloud-scaling-loghouse
- Anthropic observability on ClickHouse — https://clickhouse.com/blog/how-anthropic-is-using-clickhouse-to-scale-observability-for-ai-era
- Tinybird — 1B rows/sec in ClickHouse — https://www.tinybird.co/blog/1b-rows-per-second-clickhouse

**Benchmarks (note vendor bias)**
- TSBS (Time Series Benchmark Suite) — https://github.com/timescale/tsbs
- KX — TSBS across QuestDB/ClickHouse/TimescaleDB/InfluxDB — https://kx.com/blog/benchmarking-kdb-x-vs-questdb-clickhouse-timescaledb-and-influxdb-with-tsbs/
- QuestDB vs ClickHouse — https://questdb.com/blog/clickhouse-vs-questdb-comparison/
- StarTree — ClickHouse vs Druid vs Pinot — https://startree.ai/resources/a-tale-of-three-real-time-olap-databases/
- GreptimeDB vs ClickHouse vs Elasticsearch (log engine) — https://greptime.com/blogs/2024-08-22-log-benchmark
- ClickHouse vs TimescaleDB vs InfluxDB (2026) — https://sanj.dev/post/clickhouse-timescaledb-influxdb-time-series-comparison/
- Neutral aggregator — https://www.timestored.com/data/time-series-database-benchmarks
- Clickhouse query optimisation definitive guide — https://clickhouse.com/resources/engineering/clickhouse-query-optimisation-definitive-guide

**Architecture pattern (reversibility)**
- Medallion architecture — raw layer as source of truth (Databricks) — https://www.databricks.com/blog/what-is-medallion-architecture
- Medallion lakehouse architecture (Microsoft Learn) — https://learn.microsoft.com/en-us/azure/databricks/lakehouse/medallion

**Licensing**
- Elasticsearch is open source again (AGPLv3, 2024) — https://www.elastic.co/blog/elasticsearch-is-open-source-again
- TimescaleDB 2.14 (multi-node removed) — https://github.com/timescale/timescaledb/releases/tag/2.14.0

**Context**
- Companion reference document with full landscape and history: `Time-Series-Databases-Guide.md`
