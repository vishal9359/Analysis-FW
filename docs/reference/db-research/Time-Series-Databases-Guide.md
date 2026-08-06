# Time-Series Databases — A Guide by World

*What each was built for, how each works, and how they compare — organized around the three workloads that matter.*
*Written to be readable by anyone. Technical claims re-checked July 2026.*

---

## Contents

1. [Introduction](#1-introduction)
2. [The three major worlds](#2-the-three-major-worlds)
3. [Categorization](#3-categorization)
4. [Analytics — evolution by problem](#4-analytics--evolution-by-problem)
5. [Analytics — database catalog](#5-analytics--database-catalog)
6. [Monitoring — evolution by problem](#6-monitoring--evolution-by-problem)
7. [Monitoring — database catalog](#7-monitoring--database-catalog)
8. [IoT — evolution by problem](#8-iot--evolution-by-problem)
9. [IoT — database catalog](#9-iot--database-catalog)
10. [Comparison matrix](#10-comparison-matrix)
11. [Appendix — ClickHouse OSS vs ClickHouse Cloud](#11-appendix--clickhouse-oss-vs-clickhouse-cloud)
12. [Why not the "faster" databases? — decision rationale](#12-why-not-the-faster-databases--decision-rationale-for-the-system-analysis-framework)
13. [References & sources](#13-references--sources)

---

## 1. Introduction

### What is time-series data?

Time-series data is any data where **every record carries a timestamp, and the timestamp is the main thing you organize by**: a CPU reading every second, a disk I/O event, a temperature sample, a stock price tick, a click on a website.

It behaves very differently from the data in a typical business application:

- **It never stops arriving** — thousands to millions of points per second, continuously, rather than a few records per user per day.
- **It's written once and almost never edited** — you don't go back and change what the CPU usage *was* at 10:03:07.
- **Recent data is hot; old data is cold** — nearly every question is about the last minutes or hours, and old data can usually be summarized or deleted.
- **Questions are about ranges and math, not single rows** — "average latency from 2pm to 3pm, grouped by service," not "fetch record #4711."

### Why traditional databases struggle

A traditional database (MySQL, Postgres, Oracle) is tuned for the *opposite* personality: moderate write rates, frequent edits, and lookups or changes to *individual rows*. Point one at a firehose of appends and range-scans over billions of rows and it slams into **three walls**:

1. **Writes slow down** — every insert has to keep indexes up to date, and that cost grows with the data.
2. **Storage explodes** — the row-by-row layout compresses time-series data poorly, so disks fill fast.
3. **Analysis crawls** — the engine reads whole rows off disk when your question only needs one column across a time range.

Every database in this guide exists because someone hit one of those three walls in a real situation and built a way around it.

---

## 2. The three major worlds

Underneath all the products, there are **three foundational workloads.** Get these three straight and everything else falls into place.

**Monitoring (metrics).** Numeric health readings — CPU, request rate, error count — each tagged with a few labels, sampled on a fixed schedule. The data is regular and moderate in variety; the questions are "graph or alert on metric X over the last window." This is the world of SRE dashboards and on-call alerts.

**IoT (device telemetry).** Millions of physical devices — meters, vehicles, machines — each sending the same handful of fields on a schedule. The data is extremely *regular*, often flows through weak edge hardware, and never stops, so compression and "what is this device's current value?" queries dominate. This is the world of factories, utilities, and connected cars.

**Analytics (events).** Billions of individual *event* records — a page view, an ad impression, an I/O operation — that people want to aggregate and drill into freely. The data is irregular and high-variety, full detail is kept, and the questions are open-ended and unknown in advance. This is the world of product analytics, clickstreams, and ad dashboards.

**A note on general-purpose databases.** A handful of databases (InfluxDB, TimescaleDB, QuestDB, CrateDB, and the newer unified engine GreptimeDB) are *general-purpose* — you can run them across more than one of these worlds. To keep this guide organized around the three worlds, each is placed under the world where it's most commonly deployed and flagged as general-purpose, rather than given a separate bucket.

### The three worlds by example

The distinction becomes concrete with one scenario per world.

**Monitoring — a DevOps team running a website.** They collect `CPU_usage%` from 500 servers every 10 seconds, each sample tagged `server=web-01, region=us-east`. A typical question: *"Show the average CPU of the web tier over the last 3 hours, and alert me if any server crosses 90%."* The data is a handful of numbers plus labels, arriving like clockwork; the questions are simple, about the recent past, and known in advance (dashboards and alerts). → **Prometheus, VictoriaMetrics.**

**IoT — an electric utility with 10 million smart meters.** Each meter reports voltage, current, and power every minute — the same three fields, forever. Typical questions: *"What is the current reading of meter #7,431,002?"* and *"Average power draw per district over the last day."* The data is millions of identical devices on a perfectly regular schedule; the challenge is sheer volume, extreme compression, and fast "current value per device" lookups. → **TDengine, Apache IoTDB.**

**Analytics — an e-commerce site logging every click.** Billions of page-view events, each with different attributes: `user_id, product, price, referrer, device, country`. A typical question: *"What is the conversion rate by traffic source, broken down by device type and hour, for products over $50, last quarter?"* — a question nobody planned for. The data is irregular and high-variety, full detail is kept, and the questions are open-ended and invented on the fly. → **ClickHouse, Druid, Pinot.**

The System Analysis Framework's trace data is Analytics-world: irregular per-IO events, high-cardinality attributes (PIDs, LBAs, queues), unknown queries, and correlation across layers — which is why an analytics engine (ClickHouse), not a monitoring or IoT database, is the fit.

### The three worlds compared

| | Monitoring | IoT | Analytics |
|---|---|---|---|
| What a "row" is | A metric sample | A device reading | An event |
| Ingestion rate | High | High | High *(all three ingest fast — not a differentiator)* |
| Source uniformity & cardinality | Uniform, low–moderate cardinality | Very uniform, low cardinality | Non-uniform, **high cardinality** |
| Variety of fields | Low (few metrics) | Low (fixed sensors) | High (many attributes) |
| Do you know the queries? | Yes (dashboards/alerts) | Mostly (per-device/aggregate) | No — ad-hoc |
| Typical question | "Alert if CPU > 90%" | "This meter's current value" | "Break down X by Y by Z" |
| Time focus | Recent | Recent + per-device | Any range, historical |
| JOINs across tables | No | Limited | Yes |

*A note on "regularity": throughout this guide, "regular" versus "irregular" refers to **data structure** — uniform sources with low cardinality versus varied events with high cardinality — **not** to how fast or steadily data arrives. All three worlds ingest at high, steady rates, so ingestion rate is not what separates them. What separates them is cardinality, field variety, whether the queries are known in advance, and whether cross-table JOINs are needed. This is why a high-rate, steadily-arriving stream of high-cardinality trace events (such as the System Analysis Framework's per-IO records) is an **Analytics** workload — not IoT or Monitoring — despite arriving very regularly.*

### Which world has the best read/write performance?

The honest answer is that the three cannot be ranked head-to-head, because they are not doing the same job — it is like asking whether a sports car, a cargo truck, or a race kart is "fastest." Each is fastest in its own lane and slow (or useless) outside it.

Within their intended workloads:

| | Write (ingest) | Read (query) |
|---|---|---|
| **Monitoring** (e.g. VictoriaMetrics) | Very high — metric samples are tiny and compressible | Fastest for simple, recent queries; cannot do complex analytics |
| **IoT** (e.g. TDengine) | Highest raw single-node ingest — uniform device data compresses best | Fast for per-device and simple queries; weak at complex joins |
| **Analytics** (e.g. ClickHouse) | High with batching (~900K–4M rows/s single node, scaling to 400M+/s on a cluster) | Fastest for complex aggregations over billions of rows; the most flexible |

The key insight: the specialized worlds (Monitoring and IoT) usually post the **highest raw ingest numbers**, because a stream of identical readings is far easier to swallow and compress than a stream of varied events. Analytics engines trade some of that raw speed for **flexibility** — full SQL, arbitrary queries, and joins. So:

- Highest raw write throughput → IoT / Monitoring engines, on their uniform data.
- Fastest simple, recent reads → Monitoring.
- Fastest complex, ad-hoc, historical reads → Analytics.

But "best performance" is the wrong lens for a design decision. If you ask a monitoring database to store per-IO events and JOIN across layers, it does not run slower — it simply cannot do the job. The right question is "best fit for the workload," which for high-cardinality events with ad-hoc joins and free scaling is the Analytics world.

---

## 3. Categorization

Before meeting the databases individually, here they are sliced two ways.

### By workload

| Workload | What it holds | Databases |
|---|---|---|
| **Analytics (events / OLAP)** | Billions of individual event records; ad-hoc questions | ClickHouse, Druid, Pinot, QuestDB\* |
| **Monitoring (metrics)** | Numbers with labels, sampled on a schedule | Prometheus, VictoriaMetrics, Thanos, Grafana Mimir, M3DB, InfluxDB\*, GreptimeDB\* |
| **IoT (device telemetry)** | Uniform readings from device fleets | TDengine, IoTDB, TimescaleDB\*, CrateDB\* |
| **Legacy** | Metrics, by the standards of their era | RRDtool, Graphite, OpenTSDB, KairosDB |

\* *General-purpose database, placed by primary use.*

### By storage engine

| Storage engine | How it stores data | Databases |
|---|---|---|
| **Immutable columnar parts, background-merged (MergeTree-style)** | Sorted, compressed columnar chunks written once, then merged in the background — LSM-*inspired*, but not a classic LSM tree | ClickHouse |
| **MergeTree-inspired metric store** | Per-series metric blocks merged in the background using ideas borrowed from MergeTree, specialized for metric samples — *not* a general columnar engine | VictoriaMetrics |
| **Time-partitioned columnar segments** | Sealed segments with bitmap/other indexes | Druid, Pinot |
| **Per-series LSM chunks** | One logical stream per label combination | Prometheus, InfluxDB v1 |
| **Row heap + time chunks** | Postgres rows partitioned by time, columnar for old data | TimescaleDB |
| **Time-sorted memory-mapped columns** | Column files kept physically ordered by time | QuestDB |
| **Per-device tables** | One physical table per device | TDengine |
| **Sensor-native file format (TsFile)** | Device/measurement tree on disk | IoTDB |
| **Parquet on object storage** | Open columnar files on S3 | GreptimeDB, InfluxDB 3 |
| **Lucene inverted index** | Every column indexed like a search engine | CrateDB |
| **On external key-value store** | Schema layered on HBase / Cassandra | OpenTSDB, KairosDB |
| **Round-robin fixed file** | Fixed-size ring that ages data out | RRDtool |

---

## 4. Analytics — evolution by problem

Not a timeline of years, but a chain of *problems*. **Rows run worst → best for open-ended, ad-hoc analytics (the workload this "world" is about); the last row is the most common default for that case.** "Best" is workload-dependent — Druid can win for streaming dashboards, Pinot for high-QPS user-facing analytics, QuestDB for finance.

| The problem someone had | The database born from it | How it solved the problem |
|---|---|---|
| Ingest financial tick data as fast as physically possible and run time-range / ASOF queries in SQL. | **QuestDB** | Data stored physically sorted by time in memory-mapped column files — the layout *is* the index; native ASOF JOIN. *(Powerful and general-purpose despite its finance roots, but single-node in the free tier.)* |
| Serve analytics to *millions of end users* at millisecond latency and huge query rates. | **Pinot** | Pre-computed star-tree indexes + partition-aware routing so each query touches minimal data and few servers. *(Fastest for **known** query shapes; weak at ad-hoc.)* |
| Show sub-second dashboards on ad events that are *still streaming in*. | **Druid** | Live in-memory ingestion + sealed, time-partitioned, bitmap-indexed segments; queries merge the live and historical lanes. *(More exploratory than Pinot, still rollup-oriented.)* |
| Run *any* interactive query over billions of events in under a second — you can't pre-aggregate questions you don't know yet. | **ClickHouse** | Columnar storage + vectorized (SIMD) execution + heavy compression — brute-force scans become cheap. *(Most flexible full-SQL all-rounder; scales for free.)* |

---

## 5. Analytics — database catalog

Template for each: **why it was developed · use cases · implementation (short) · strengths · weaknesses.**

### ClickHouse
- **Why it was developed:** Yandex.Metrica (Europe's answer to Google Analytics) had to let analysts run any SQL over billions of page-view events in under a second — and you can't pre-compute answers to questions you don't know yet.
- **Use cases:** The general-purpose analytics workhorse — event logs, clickstreams, traces, metrics, tick data; huge scans and ad-hoc SQL.
- **Implementation (short):** The **MergeTree** engine writes sorted, compressed, immutable columnar "parts" by pure append and merges them in the background; a sparse index skips irrelevant blocks; per-column codecs shrink data 10×+; **vectorized (SIMD)** execution runs across every core and node. Plus TTL retention, materialized views for rollups, and a native `lttb` downsampling function.
- **Strengths:** Blazing full-scan speed; excellent compression; full SQL; readers never block writers; scales out for free (Apache 2.0).
- **Weaknesses:** Single-row edits are expensive, and inserts must be **batched** — tiny per-row inserts are the anti-pattern. (Lightweight deletes now exist, but bulk data modification is still not its strength.)

### Apache Druid
- **Why it was developed:** Ad-tech dashboards needed events queryable *seconds* after they happen, with sub-second drill-downs, while data streams in nonstop.
- **Use cases:** Always-on streaming analytics dashboards fed by Kafka.
- **Implementation (short):** Events sit in memory on ingestion (instantly queryable) and are periodically sealed into time-partitioned, **bitmap-indexed** columnar **segments** in deep storage (S3/HDFS), served by historical nodes; a query transparently merges the live and historical lanes.
- **Strengths:** Sub-second aggregations at high concurrency; fresh data within seconds; durable and elastic via deep storage.
- **Weaknesses:** Roughly five or more service types plus ZooKeeper and a metadata DB to run (recent versions can reduce the ZooKeeper dependency via Kubernetes-based discovery); joins historically weak and SQL limited, though the newer multi-stage query engine is improving both; "rollup" culture can drop raw detail.

### Apache Pinot
- **Why it was developed:** Druid's problem pushed to *user-facing* scale — LinkedIn wanted to show *every member* "Who viewed your profile?", meaning millisecond answers at thousands of queries per second.
- **Use cases:** Analytics features embedded inside customer-facing apps.
- **Implementation (short):** The same segment shape as Druid, plus the richest index toolbox in the class — most famously the **star-tree index**, which pre-computes aggregations so a common query becomes a lookup; partition-aware routing sends each query to only the 1–2 servers holding the answer.
- **Strengths:** Unmatched concurrency and latency for known query shapes; tiny work per query.
- **Weaknesses:** You must *know* your queries to configure the indexes (the opposite of ad-hoc exploration); complex SQL is weaker (the multi-stage query engine is closing the join gap); heavy operations.

### QuestDB  *(general-purpose SQL)*
- **Why it was developed:** Financial markets — tick data at millions of rows per second with nanosecond timestamps, queried in SQL, without kdb+'s price tag.
- **Use cases:** Very fast single-node SQL ingest — finance tick data, sensor firehoses, application metrics, and other high-rate time-series.
- **Implementation (short):** Data is **always stored physically sorted by time** (the "designated timestamp") as memory-mapped, append-only column files, so time queries need no index — the storage layout *is* the index; a zero-GC Java core with SIMD aggregations, a native ASOF JOIN, plus recently added TTL and materialized views.
- **Strengths:** Among the fastest single-node ingest engines (it publishes very strong benchmarks); superb time-window scans; native ASOF JOIN; SQL.
- **Weaknesses:** Clustering and replication are enterprise-only (the project has stated no plans to open-source high availability), so both read and write capacity cap at one free node; compression is weaker than dedicated columnar OLAP engines.

---

## 6. Monitoring — evolution by problem

**Rows run worst → best for this world.** "Best" means the strongest, most-recommended choice for a demanding monitoring platform today; the legacy and declining systems sit first.

| The problem someone had | The database born from it | How it solved the problem |
|---|---|---|
| Graph one router's bandwidth on a tiny disk. | **RRDtool** | A fixed-size round-robin file that auto-averages old data into summaries and drops the oldest. *(Ancient; embedded-only today.)* |
| One dashboard for a whole farm of servers. | **Graphite** | A central daemon to receive metrics, plus a web API to graph any of them. *(Legacy; no compression, drowns at scale.)* |
| Store billions of metrics forever without building a storage engine. | **OpenTSDB / KairosDB** | A clever schema layered on **HBase / Cassandra** — reuse a cluster that already scales. *(Legacy scale-out; largely dormant.)* |
| Store one company's entire metric firehose with replication (Uber scale). | **M3DB** | A sharded, replicated custom store with its own compression and an aggregation tier. *(Capable, but community has cooled — rarely a new pick.)* |
| Give metrics a friendly, purpose-built home with easy ingest and retention. | **InfluxDB** | A batteries-included TSDB: line-protocol ingest, tags, retention, dashboards. *(Easy and popular, but the free tier is single-node.)* |
| Monitor infrastructure where servers appear and vanish every minute. | **Prometheus** | **Pull-based scraping** + service discovery + labels; new services are found and monitored automatically. *(The de-facto standard, but a single-server architecture with limited long-term retention.)* |
| Keep Prometheus, but get a global view and cheap years of history. | **Thanos** | Sidecars upload blocks to S3; a query layer fans out; a compactor tidies and downsamples. *(Adds scale to Prometheus as a bolt-on layer.)* |
| Offer "Prometheus as a service" to many teams, then scale to a billion series. | **Cortex → Grafana Mimir** | Multi-tenant microservices on object storage, each scaling independently. *(Very scalable and multi-tenant, but heavy to run.)* |
| Stop running three systems for metrics, logs, and traces on costly local disks. | **GreptimeDB** | An LSM engine that flushes and compacts data into Parquet on cheap object storage, queryable by both SQL and PromQL. *(Modern unified engine, but young.)* |
| Do all of this far more cheaply and simply than the heavy stacks. | **VictoriaMetrics** | A lean, ClickHouse-inspired rewrite with free clustering and much lower RAM/disk per sample. *(Efficient, scalable, and simple — one of the strongest all-round scalable metrics stores; Mimir/Thanos are often preferred for very large multi-tenant setups.)* |

---

## 7. Monitoring — database catalog

### Prometheus
- **Why it was developed:** The move to containers meant servers appeared and vanished every minute, and tools built for fixed server lists couldn't keep up. Ex-Google engineers recreated Google's internal monitoring for this new world.
- **Use cases:** Monitoring and alerting across a cluster or environment (one Prometheus server per environment) — the de-facto standard.
- **Implementation (short):** A single binary that **pulls** ("scrapes") metrics over HTTP, discovers targets automatically, keeps the most recent ~2 hours in an in-memory head block, then flushes and **compacts** older data into progressively larger immutable blocks with Gorilla delta-compression (typically ~1–2 bytes/sample, though it varies with cardinality and data patterns), and evaluates alert rules continuously via PromQL.
- **Strengths:** Auto-discovers new services; tiny storage footprint; huge ecosystem; dead-simple to run.
- **Weaknesses:** Single-node *by design* — no long-term retention, no clustering, and no raw events (metrics only).

### VictoriaMetrics
- **Why it was developed:** One engineer's conviction that the Prometheus-scaling stacks were too heavy and RAM-hungry.
- **Use cases:** A drop-in long-term and scale-out backend for Prometheus data — "the simplest thing that scales."
- **Implementation (short):** A from-scratch rewrite in Go whose storage borrows ideas from ClickHouse's MergeTree but is specialized for metric samples (not a general columnar engine); a cluster version with three simple roles (insert, store, select) that is fully open source.
- **Strengths:** Excellent compression and low resource use; free clustering; PromQL-compatible (MetricsQL).
- **Weaknesses:** Metrics only — no events, no SQL, no joins; automatic downsampling is an enterprise feature.

### Thanos
- **Why it was developed:** A company running many Prometheus servers wanted one global view and years of history *without replacing Prometheus*.
- **Use cases:** Federating existing Prometheus servers and archiving to object storage.
- **Implementation (short):** A sidecar attaches to each Prometheus and uploads finished blocks to S3; a query layer fans out across everything; a compactor tidies and downsamples old data.
- **Strengths:** Adds a layer instead of ripping anything out (incremental adoption); S3 makes history nearly free.
- **Weaknesses:** More moving parts than plain Prometheus; query latency across many stores can grow.

### Grafana Mimir (from Cortex)
- **Why it was developed:** Cortex was "Prometheus as a hosted, multi-tenant service"; Grafana Labs later rebuilt it as Mimir to handle a *billion* active series.
- **Use cases:** A central metrics platform for very large organizations — many teams, many clusters, strict per-team isolation.
- **Implementation (short):** A set of microservices (ingesters, queriers, compactors, ruler…) that each scale independently, storing blocks in object storage, with every tenant logically separated.
- **Strengths:** Scales each bottleneck independently; strong multi-tenancy; massive series counts.
- **Weaknesses:** Many services to operate; overkill for smaller teams.

### M3DB
- **Why it was developed:** Uber's metric volume — every car, every city, every service — crushed Graphite.
- **Use cases:** Uber-scale metric storage with replication and fault tolerance.
- **Implementation (short):** A sharded, replicated custom store with its own compression (M3TSZ) and an aggregation tier for rollups.
- **Strengths:** Built for one thing — sustained massive metric ingest that keeps working when nodes fail.
- **Weaknesses:** Community activity has cooled; complex to operate; rarely the right *new* choice today.

### InfluxDB  *(general-purpose SQL)*
- **Why it was developed:** To give time-series a purpose-built, batteries-included home instead of files or Hadoop — for years it was *the* default answer for metrics.
- **Use cases:** DevOps and IoT metrics on a single node; more recently, an edge/recent-data engine.
- **Implementation (short):** v1's engine stored data **per series** (per unique tag combination), which is great for metrics but blows up on high cardinality — the notorious "cardinality wall." v2 detoured into the Flux language (since deprecated in favor of InfluxQL/SQL); **v3 is a ground-up Rust rewrite** on Apache Arrow/Parquet over object storage — which fixes the cardinality wall by dropping the per-series index entirely and storing tags as ordinary columns in a columnar table.
- **Strengths:** Very easy to start; huge adoption and tooling; v3 fixes cardinality and adds SQL.
- **Weaknesses:** Free version is single-node. In v3 **Core** you can now write and query *any* time period, but a *single query* can only span a limited time range — roughly 72 hours by default, which is a configurable server limit on how many Parquet files one query plan may touch. Removing that per-query span limit requires Enterprise's compactor (which rewrites the small files into larger, sorted blocks). Clustering and the compactor are paid.

### GreptimeDB  *(general-purpose / unified)*
- **Why it was developed:** A team that had spent years building a proprietary TSDB — hitting trillions of points a day, runaway storage bills, and data fragmented across three siloed systems — set out to fix all of it at once.
- **Use cases:** Greenfield observability platforms wanting one store for metrics, logs, and events.
- **Implementation (short):** Written in Rust; an LSM engine writes **Parquet files to object storage**; an Apache Arrow + DataFusion query stack (vectorized/SIMD execution, like ClickHouse and QuestDB) speaks both SQL and PromQL; "flows" do continuous aggregation; distribution is in the open-source build. Its aim is to bring columnar-analytics performance to a unified, cloud-native design — though it is far younger and less battle-tested than ClickHouse.
- **Strengths:** Cheap, elastic storage via S3; one engine for three data types; open-source clustering.
- **Weaknesses:** Young — a few years old versus ClickHouse's decade-plus in the open (development since ~2009, open-sourced 2016) — so a smaller community and faster-moving ground.

### The legacy ancestors
- **RRDtool** — fixed-size round-robin files with automatic aging into summaries; perfect for its 1999 problem, still living inside old network tools.
- **Graphite** — centralized metrics for the first web farms; one file per metric (Whisper) drowns at scale and there's no compression.
- **OpenTSDB / KairosDB** — "let HBase/Cassandra do the scaling"; powerful in their day, but carrying a Hadoop cluster just for metrics is a price few still pay.

---

## 8. IoT — evolution by problem

**Rows run worst → best for this world** — best-fit for device-fleet telemetry last.

| The problem someone had | The database born from it | How it solved the problem |
|---|---|---|
| Keep sensor data in SQL, joined to device/customer tables, without Postgres collapsing at scale. | **TimescaleDB** | A Postgres extension that splits data into time "chunks" so hot indexes stay in RAM — full SQL survives. *(General-purpose SQL; no built-in multi-node write scale-out, and not IoT-specialized.)* |
| Query distributed machine data with SQL *and* full-text search across a cluster. | **CrateDB** | A shared-nothing SQL engine on Lucene — every column indexed, any node answers any query. *(Scales for free, but general SQL and heavier storage.)* |
| Run the database on weak edge hardware, survive messy data, and sync edge → cloud. | **IoTDB** | A sensor-native file format (TsFile) + one engine from gateway to cluster; edge files ship and merge centrally. *(Purpose-built for industrial and edge deployments.)* |
| Exploit that device fleets share a regular per-device schema (the same set of fields), whether readings are periodic or event-driven — and stop paying for clustering. | **TDengine** | One table per device; each device's data lands contiguously in time order and compresses hard; free clustering. *(Purpose-built for large device fleets — extreme ingest and compression.)* |

---

## 9. IoT — database catalog

### TDengine
- **Why it was developed:** General TSDBs waste IoT data's most valuable property — its *regularity* — and the popular option kept clustering behind a paywall.
- **Use cases:** Large fleets of similar devices: meters, vehicles, industrial sensors.
- **Implementation (short):** A **"one table per device, one super-table per device type"** model, so each device's uniform, in-order data lands contiguously and compresses exceptionally; a built-in last-value cache answers "current status" instantly, and built-in streaming (Streams) and pub-sub (TMQ) can replace *parts* of a Kafka+Redis+DB stack in some designs.
- **Strengths:** Extreme ingest and compression for device data; clustering included in the open-source build (AGPL); fewer surrounding systems to run.
- **Weaknesses:** The model *is* the optimization — irregular, high-cardinality event streams sit off its happy path.

### Apache IoTDB
- **Why it was developed:** Industrial projects (metro trains, factories) needed a database that runs *on the machine at the edge* — sometimes on very weak hardware — survives messy real-world data, and syncs upward to the cloud.
- **Use cases:** Edge-to-cloud industrial telemetry: trains, turbines, production lines.
- **Implementation (short):** A purpose-built columnar file format (**TsFile**) organizes data as a physical device tree (`root.factory.line1.sensor42`); the *same storage engine and file format* run from a single edge node up to a cluster, so edge files ship and merge centrally with no conversion. Ships an M4 operator for pixel-accurate visualization queries.
- **Strengths:** Same engine and file format from a tiny edge box to a full cluster; tolerant of out-of-order and gappy data; strong per-sensor compression.
- **Weaknesses:** Its own SQL dialect and device-tree model fit generic event data poorly; joins are weak.

### TimescaleDB (TigerData)  *(general-purpose SQL)*
- **Why it was developed:** An IoT startup realized their real product was "Postgres, but good at time-series."
- **Use cases:** Time-series living next to relational data — device registries, users, configs — queried with full SQL at moderate scale.
- **Implementation (short):** A **Postgres extension** that secretly splits your big table into time **chunks** so hot indexes stay in RAM, compresses old chunks into columnar form (~90% smaller), and maintains **continuous aggregates** (always-fresh rollups); offers `lttb()` for graph downsampling.
- **Strengths:** It *is* Postgres, so every tool, driver, and JOIN just works; chunk pruning keeps range queries fast.
- **Weaknesses:** Native distributed hypertables were **removed** (v2.14, 2024), so there's no built-in horizontal *write* scale-out — you scale up (one primary) plus read replicas, or shard at the application level; advanced features use a source-available (not OSI-open) license. (The company rebranded to **TigerData** in 2025, though the open-source extension keeps the **TimescaleDB** name.)

### CrateDB  *(general-purpose SQL)*
- **Why it was developed:** Industrial "machine data" needed both scale-out ingest *and* real SQL with joins and full-text search.
- **Use cases:** Sensor/machine data queried together with its business context (which machine, which customer, which site) in one distributed SQL cluster.
- **Implementation (short):** A shared-nothing distributed SQL engine whose storage layer is **Lucene** (the search library behind Elasticsearch), so every column is indexed by default and any node can answer any query.
- **Strengths:** Fast ad-hoc filtering with no planning; built-in full-text search; scales horizontally with SQL.
- **Weaknesses:** Heavier storage from indexing everything; no built-in continuous aggregation.

### (Niche IoT players)
- **openGemini** (Huawei) — clustered and InfluxQL-compatible; an exit ramp for teams that outgrew free InfluxDB.
- **HoraeDB** (Ant Group, formerly CeresDB) — built for metrics with *millions* of unique labels that choke classic designs.
- **SiriDB** — small, MIT-licensed, natively clustered; tiny community.
- **Warp 10** — time-series **plus location** (GPS tracks, fleets, ships), with a strong geo-temporal query language.

---

## 10. Comparison matrix

**Legend:** ✓ = yes / strong · ◐ = partial / moderate · ✗ = no.
**Downsampling column:** ✓ = native **LTTB** (or M4) function · ◐ = downsampling, but time-bucket/average (not LTTB) · ✗ = none.

| Database | Free / OSS license | Very high ingest | Fast time-range queries | Multi-node (free) | Retention policies | Continuous queries | Downsampling (LTTB) | Compression | Category |
|---|---|---|---|---|---|---|---|---|---|
| **⭐ ClickHouse — SELECTED** | **✓ Apache-2.0** | **✓ (batched)** | **✓** | **✓** | **✓ (TTL)** | **✓ (mat. views)** | **✓ (native `lttb`)** | **✓ strong** | **Analytics** |
| **Druid** | ✓ Apache-2.0 | ✓ (streaming) | ✓ | ✓ | ✓ | ◐ (ingest rollup) | ◐ | ◐ | Analytics |
| **Pinot** | ✓ Apache-2.0 | ✓ (streaming) | ✓ | ✓ | ✓ | ◐ (star-tree pre-agg) | ◐ | ◐ | Analytics |
| **QuestDB** | ✓ Apache-2.0 | ✓ (single-node) | ✓ | ✗ (paid) | ✓ (TTL) | ✓ (mat. views) | ◐ (SAMPLE BY) | ◐ (weaker than columnar OLAP) | Analytics |
| **Prometheus** | ✓ Apache-2.0 | ◐ (scrape-paced) | ◐ (recent) | ✗ (by design) | ✓ | ✓ (recording rules) | ✗ | ◐ (Gorilla) | Monitoring |
| **VictoriaMetrics** | ✓ Apache-2.0 | ✓ (metrics) | ✓ | ✓ | ✓ | ✓ (stream agg, free) | ◐ (time-bucket, Enterprise) | ✓ strong | Monitoring |
| **Thanos** | ✓ Apache-2.0 | ◐ (via Prometheus) | ✓ | ✓ | ✓ | ✓ (ruler) | ◐ (5m/1h rollups) | ◐ | Monitoring |
| **Grafana Mimir** | ✓ AGPL-3.0 | ✓ | ✓ | ✓ | ✓ | ✓ (ruler) | ✗ | ◐ | Monitoring |
| **M3DB** | ✓ Apache-2.0 | ✓ | ✓ | ✓ | ✓ | ◐ (agg tier) | ◐ (rollup rules) | ◐ (M3TSZ) | Monitoring |
| **InfluxDB 3 (Core)** | ✓ MIT/Apache-2.0 (Core) | ✓ | ◐ (single-query span ≈72h by default, configurable) | ✗ (paid) | ✓ | ✓ (processing engine) | ◐ | ◐ | Monitoring |
| **GreptimeDB** | ✓ Apache-2.0 | ✓ | ✓ | ✓ | ✓ (TTL) | ✓ (flows) | ◐ | ◐ (Parquet) | Monitoring |
| **TDengine** | ✓ AGPL-3.0 | ✓ (device data) | ✓ | ✓ | ✓ (KEEP) | ✓ (streams) | ◐ (interval) | ✓ strong | IoT |
| **IoTDB** | ✓ Apache-2.0 | ✓ (sensor data) | ✓ | ✓ | ✓ (TTL) | ✓ (CQ) | ✓ (M4) | ◐ | IoT |
| **TimescaleDB** | ✓ Apache-2.0 + TSL | ◐ (~100Ks rows/s) | ✓ | ✗ (removed) | ✓ (retention policies) | ✓ (cont. aggregates) | ✓ (`lttb` toolkit) | ✓ (high, workload-dependent) | IoT |
| **CrateDB** | ✓ Apache-2.0 | ◐ (scale-out) | ✓ | ✓ | ✓ (partition drop) | ✗ | ✗ | ◐ (heavier) | IoT |

*Licenses and free-tier limits change often — confirm anything load-bearing against the vendor's current docs. The InfluxDB row reflects the current **v3 Core** release; older v1/v2 behaved differently (v1 had the cardinality wall, v1 used Continuous Queries, v2 used Tasks). "Category" reflects each database's primary world; the general-purpose ones (InfluxDB, TimescaleDB, QuestDB, CrateDB, GreptimeDB) are used across worlds. On the Compression column: the Parquet-based engines (GreptimeDB, InfluxDB 3) still achieve strong general columnar compression via ZSTD and dictionary encoding — they are marked ◐ only relative to the specialized time-series codecs (Delta, DoubleDelta, Gorilla) in ClickHouse, VictoriaMetrics, and TDengine, not because their compression is weak.*

---

## 11. Appendix — ClickHouse OSS vs ClickHouse Cloud

The single most important point: **it is the same database engine in both.** Cloud does not run different SQL or query faster per core — the differences are in storage architecture, operations, managed tooling, and compliance, not in what the database fundamentally does. (Drawn from official ClickHouse documentation; see sources below.)

| Area | ClickHouse OSS (self-hosted) | ClickHouse Cloud (managed, paid) |
|---|---|---|
| **License / cost** | Apache 2.0, free. You pay only for your own infra + ops labor | Consumption-based (compute per-second + object storage); tiers: Basic / Scale / Enterprise |
| **Core engine** | Full columnar engine, MergeTree family, vectorized execution, full SQL, materialized views, TTL, dictionaries | **Identical engine** — same SQL, same functions, same performance per core |
| **Storage / replication engine** | **ReplicatedMergeTree** — each node stores its own full copy on local disk; coordinated by ClickHouse Keeper | **SharedMergeTree** (proprietary, Cloud-only) — all data in object storage (S3/GCS/Blob), stateless compute |
| **Scaling model** | Manual: you design sharding (Distributed tables) + replication; resharding is a project | Automatic: compute scales independently of storage, so adding compute replicas generally needs no resharding of data; hundreds of replicas per table; autoscaling + idle-to-zero |
| **Compute / storage separation** | Coupled (compute + data on same nodes)\* | Fully separated; also compute-compute separation (isolate reads from writes on shared storage) |
| **Operations** | You run backups, upgrades, monitoring, HA, tuning | Fully managed — setup, backups, upgrades, monitoring, billing |
| **Managed ingestion** | Built-in **Kafka table engine**, S3/URL functions, Postgres/MySQL integration engines — but you wire them up | **ClickPipes** (managed pipelines from Kafka, Kinesis, S3, Postgres CDC). *The Kafka table engine is not supported in Cloud (per official docs) — ClickPipes is the recommended path* |
| **Management UI** | None official; use CLI / third-party (Grafana, DBeaver) | Web SQL console, dashboards, service management, Cloud API |
| **Access / auth** | RBAC, password policies, certificate/SSH auth, **LDAP & Kerberos** — all included, but self-managed | Same database RBAC **plus** managed console-level SAML SSO, API keys, cloud identity integration |
| **Compliance certs** | None inherent — *you* self-certify your deployment | SOC 2 Type II, ISO 27001, GDPR, CCPA, US DPF (all plans); HIPAA, PCI DSS (Enterprise) |
| **Support / SLA** | Community; or a third-party vendor (e.g. Altinity) | SLA-backed uptime + vendor support |
| **Deployment targets** | Bare metal, VM, Docker, Kubernetes, any cloud, **on-prem, air-gapped** | AWS / GCP / Azure managed; BYOC (runs in your own AWS account, Enterprise) |
| **Versioning** | You pick versions; LTS releases available; upgrade on your schedule | Managed upgrades performed by ClickHouse on a maintenance schedule; no version management for you |

\* *OSS can tier cold data to S3 via disk configuration, but compute and the authoritative data are not decoupled the way SharedMergeTree does it.*

**Cloud-only capabilities (proprietary, no self-host path):** SharedMergeTree, SharedCatalog (DDL/metadata replication across stateless replicas), compute-compute separation, autoscaling/idling, and ClickPipes.

**OSS-only advantages (OSS is not a subset of Cloud):** full clustering/sharding/replication for free under Apache 2.0 (via the official Kubernetes operator), the built-in Kafka table engine, full control over merge/concurrency/tuning settings, and on-prem / air-gapped deployment.

*Sources (official ClickHouse unless noted):* [Cloud architecture](https://clickhouse.com/docs/cloud/reference/architecture) · [SharedMergeTree](https://clickhouse.com/docs/cloud/reference/shared-merge-tree) · [SharedCatalog](https://clickhouse.com/docs/cloud/reference/shared-catalog) · [Compliance overview](https://clickhouse.com/docs/cloud/security/compliance-overview) · [ClickPipes](https://clickhouse.com/blog/clickhouse-announces-clickpipes) · [ClickHouse GitHub (Apache 2.0)](https://github.com/ClickHouse/ClickHouse) · [Cloud vs OSS analysis (OneUptime)](https://oneuptime.com/blog/post/2026-03-31-clickhouse-cloud-vs-open-source-comparison/view)

---

## 12. Why not the "faster" databases? — decision rationale for the System Analysis Framework

### 12.1 The benchmark paradox

When you benchmark raw read/write speed (TSBS, ClickBench, and the various vendor blogs), several databases beat ClickHouse on a *single* axis:

- **QuestDB** and **TDengine** ingest faster on a single node (QuestDB ~1.4M rows/s vs ClickHouse ~900K in the same TSBS test).
- **Pinot** and **Druid** answer certain queries faster (StarTree's benchmark claims Pinot is ~4× ClickHouse for its target queries).
- **GreptimeDB** claims faster writes and roughly 2× better compression in its own log benchmark.

A natural question follows: *if they are faster, why did we choose ClickHouse?*

The answer is that **raw ingest/query speed is not our binding constraint.** Our data is high-cardinality per-IO trace events (from eBPF/blktrace) that we must correlate across stack layers using unpredictable, ad-hoc queries — and we must scale out for free across three projects. A faster engine is worthless if it cannot *do that job at all*. Every "faster" database fails at least one of the four requirements below, and the one that doesn't fail them (GreptimeDB) loses on maturity rather than capability. In short: they win a drag race on an axis that is not our bottleneck, and lose on the axes that are.

### 12.2 Our four binding requirements (from the Profile & Analysis frameworks)

These come straight from the framework's data and goals ([Frameworks.txt](Frameworks.txt) §2.2, §137, §157):

1. **High-cardinality trace ingestion.** eBPF/blktrace events carry PIDs, LBAs, queue IDs, and offsets — millions of unique values. The store must swallow this without hitting a "cardinality wall."
2. **Multi-layer correlative queries (JOINs).** We correlate behaviour across syscall ↔ VFS ↔ filesystem ↔ block ↔ NVMe ↔ SSD-telemetry layers — that means JOINs across a table per layer, plus joins to per-run metadata.
3. **Ad-hoc debugging flexibility (no pre-indexing).** Defining the analysis queries is itself an open P1 task ([Frameworks.txt](Frameworks.txt) §157). We cannot pre-declare indexes or rollups for questions we have not invented yet.
4. **Completely free multi-node scaling.** As TraceVision and UVP Control grow the data volume, we must scale horizontally with no paid tier and no licence trap.

### 12.3 Requirement matrix

**Legend:** ✓ = strong fit · ◐ = partial / caveats · ✗ = does not meet.

| Requirement | ClickHouse | Monitoring TSDBs (Prometheus, VictoriaMetrics, InfluxDB, Mimir) | IoT DBs (TDengine, IoTDB) | Pinot / Druid | QuestDB / TimescaleDB | GreptimeDB |
|---|---|---|---|---|---|---|
| **1. High-cardinality trace ingestion** | ✓ Native | ✗ Metrics only; cardinality wall | ◐ Rigid device schema | ✓ Supported | ◐ High RAM overhead | ✓ Supported |
| **2. Multi-layer correlative JOINs** | ✓ Excellent | ✗ Unsupported (PromQL) | ✗ Restricted | ✗ Limited | ✓ Supported | ✓ Supported |
| **3. Ad-hoc flexibility (no pre-indexing)** | ✓ High | ◐ Metrics domain only | ✗ Rigid schema | ✗ Requires pre-index / rollup | ✓ High | ✓ High |
| **4. Completely free multi-node scaling** | ✓ Apache-2.0 | ✓ (VM / Mimir / Thanos) | ✓ (AGPL / Apache) | ◐ Free but heavy ops | ✗ Paid / removed | ✓ Apache-2.0 |
| **Verdict for our use case** | **Chosen** | Data-model mismatch | Schema mismatch | No ad-hoc; heavy ops | No free scale-out | Viable — set aside on maturity |

### 12.4 Database-by-database: why each is set aside

**QuestDB — faster single-node writes, but capped and finance-shaped.**
QuestDB is the single-node ingest champion, and for pure write speed on one machine it beats ClickHouse. But its clustering and replication are Enterprise-only, and the project has stated it has no plans to open-source high availability — so that speed is permanently **capped at one machine** (fails requirement 4). A free ClickHouse cluster reaches tens of millions of rows/s, far past any single QuestDB node. QuestDB's compression is also weaker, and its engine is tuned for regular financial tick streams rather than irregular, high-cardinality, correlated trace data. It is fast in a straight line but cannot add lanes.

**TDengine — faster single-node IoT writes, but the wrong data shape.**
TDengine out-ingests ClickHouse on uniform device data precisely because its "one table per device" model exploits regularity. Our trace data is the opposite of regular — irregular, high-cardinality events spanning many layers — which works *against* that core optimization (weakens requirement 1 and fails 3), and its joins are restricted, so multi-layer correlation (requirement 2) is hard. The moment the data stops looking like a tidy fleet of identical sensors, its advantage evaporates. It is also AGPL-licensed.

**GreptimeDB — genuinely close; set aside on maturity, not capability.**
In honesty, GreptimeDB is the strongest alternative to ClickHouse for this workload. Its own benchmarks show faster writes and about 2× better compression, it supports SQL joins, it handles high cardinality, and it scales for free under Apache-2.0 — so it **passes all four requirements on paper.** We set it aside for engineering-risk reasons, not architecture: it is only a few years old versus ClickHouse's decade-plus in the open, with a far smaller community, less mature tooling, and very few public production references for a workload like ours. For a foundational component that three projects will depend on, choosing the mature, heavily battle-tested engine is the conservative and correct call. GreptimeDB is the database to re-evaluate in two to three years.

**Pinot — faster reads, but only for questions you already know.**
Pinot's speed comes from pre-computing answers with star-tree indexes: you configure the indexes for known query shapes *in advance*. That is the exact opposite of requirement 3 — we do not know our queries yet. Pinot also has limited join support (requirement 2) and heavy operations (ZooKeeper plus multiple services). It is a go-kart that is unbeatable on a track it has already memorized, and useless for exploring roads it has never seen.

**Druid — faster fresh-streaming reads, but rollup-oriented and join-weak.**
Druid excels at sub-second dashboards over still-streaming data, but it leans on ingestion-time rollup — which can discard the raw per-event detail we need for debugging — has weak joins (requirement 2), needs up-front data modelling (requirement 3), and carries heavy operational weight. Excellent for fixed operational dashboards; wrong for ad-hoc forensic analysis of storage behaviour.

**Monitoring TSDBs (Prometheus, VictoriaMetrics, InfluxDB, Grafana Mimir) — cannot store the data at all.**
These are the fastest systems at what they do, but they store *metrics* (numbers plus labels, sampled on a schedule), not *events*. Our per-IO trace records are events, so this is a data-model mismatch that fails requirement 1 before speed is even relevant; the classic ones (Prometheus, InfluxDB v1) additionally hit the cardinality wall, and none support JOINs (requirement 2). Note: we may still run **VictoriaMetrics alongside ClickHouse** for the periodic CPU/GPU/memory metric streams — but not for the trace events themselves.

**TimescaleDB — full SQL, but no free scale-out and cardinality-bound.**
TimescaleDB gives us Postgres-grade JOINs and ad-hoc SQL (passes requirements 2 and 3), which is attractive. But native multi-node was removed in 2024, so there is no free horizontal write scale-out (fails requirement 4), and high-cardinality trace volumes pressure its memory (weakens requirement 1). It is a fine choice at moderate scale, but not for our event firehose.

**CrateDB and IoTDB — not actually faster, and each fails a requirement.**
Neither outperforms ClickHouse on our workload, so they never entered the "faster" list. CrateDB indexes every column (heavy storage, slower ingest); IoTDB's rigid device-tree model and weak joins fail requirements 2 and 3.

### 12.5 The one-line rationale

We are not buying the fastest engine on any single axis. We are buying the **one engine that satisfies all four binding requirements at once** — high-cardinality event ingestion, multi-layer JOINs, ad-hoc queries with no pre-indexing, and completely free horizontal scaling — while being battle-tested at very large scale. Each "faster" database wins on an axis that is not our bottleneck and loses on one that is; the only database that meets every requirement (GreptimeDB) is simply too young to bet a three-project foundation on today.

**Decision: ClickHouse is selected as the data store for the System Analysis Framework's Analysis FW.** It will be deployed as **single-node, open-source (Apache-2.0)** to begin with, and scaled out horizontally — for free — as TraceVision and UVP Control grow the data volume. GreptimeDB is recorded as the primary candidate to re-evaluate in roughly two years, once its maturity and ecosystem catch up.

---

## 13. References & sources

*Primary sources used in this document. Benchmark links are grouped separately so the team can extend them.*

### ClickHouse — official documentation
- [ClickHouse GitHub (Apache-2.0)](https://github.com/ClickHouse/ClickHouse)
- [ClickHouse Cloud architecture](https://clickhouse.com/docs/cloud/reference/architecture)
- [SharedMergeTree](https://clickhouse.com/docs/cloud/reference/shared-merge-tree)
- [SharedCatalog](https://clickhouse.com/docs/cloud/reference/shared-catalog)
- [Cloud security & compliance](https://clickhouse.com/docs/cloud/security/compliance-overview)
- [ClickPipes (managed ingestion)](https://clickhouse.com/blog/clickhouse-announces-clickpipes)
- [Kafka table engine (not supported in Cloud)](https://clickhouse.com/docs/engines/table-engines/integrations/kafka)
- [Inserting data / batching guidance](https://clickhouse.com/docs/guides/inserting-data)
- [Common getting-started mistakes to avoid](https://clickhouse.com/blog/common-getting-started-issues-with-clickhouse)

### ClickHouse — adopters & case studies
- [Adopters list](https://clickhouse.com/docs/about-us/adopters)
- [User stories](https://clickhouse.com/user-stories)
- [Use cases](https://clickhouse.com/use-cases)
- [How Anthropic uses ClickHouse for observability](https://clickhouse.com/blog/how-anthropic-is-using-clickhouse-to-scale-observability-for-ai-era)
- [LogHouse — scaling to a quadrillion rows](https://clickhouse.com/blog/a-quadrillion-rows-across-the-three-cloud-scaling-loghouse)

### Benchmarks & performance *(team to extend)*
- [ClickBench — dashboard](https://benchmark.clickhouse.com/)
- [ClickBench — source & methodology](https://github.com/ClickHouse/ClickBench)
- [TSBS — Time Series Benchmark Suite (Timescale)](https://github.com/timescale/tsbs)
- [TSBS — TDengine fork](https://github.com/taosdata/tsbs)
- [QuestDB vs ClickHouse (TSBS)](https://questdb.com/blog/clickhouse-vs-questdb-comparison/)
- [TDengine TSBS IoT report](https://tdengine.com/tsbs-iot-performance-report-tdengine-influxdb-and-timescaledb/)
- [KX — KDB-X vs QuestDB / ClickHouse / TimescaleDB / InfluxDB (TSBS)](https://kx.com/blog/benchmarking-kdb-x-vs-questdb-clickhouse-timescaledb-and-influxdb-with-tsbs/)
- [GreptimeDB vs ClickHouse vs Elasticsearch (log benchmark)](https://greptime.com/blogs/2024-08-22-log-benchmark)
- [GreptimeDB JSONBench (1B documents)](https://greptime.com/blogs/2025-03-18-jsonbench-greptimedb-performance)
- [StarTree — Pinot vs Druid vs ClickHouse](https://startree.ai/resources/a-tale-of-three-real-time-olap-databases/)
- [Independent OLAP comparison (Leventov)](https://leventov.medium.com/comparison-of-the-open-source-olap-systems-for-big-data-clickhouse-druid-and-pinot-8e042a5ed1c7)
- [timestored — "Every Time-Series DB Benchmark Ever"](https://www.timestored.com/data/time-series-database-benchmarks)
- [Ingesting 1B rows/sec in ClickHouse (Tinybird)](https://www.tinybird.co/blog/1b-rows-per-second-clickhouse)
- [ClickHouse vs TimescaleDB vs InfluxDB (sanj.dev)](https://sanj.dev/post/clickhouse-timescaledb-influxdb-time-series-comparison/)

### Limitations & OSS-vs-Cloud analysis
- [When NOT to use ClickHouse (ChistaDATA)](https://chistadata.com/when-not-to-use-clickhouse/)
- [ClickHouse JOIN limitations (GlassFlow)](https://www.glassflow.dev/blog/clickhouse-limitations-joins)
- [Schema-design limits (Altinity KB)](https://kb.altinity.com/altinity-kb-schema-design/how-much-is-too-much/)
- [Cloud vs open-source comparison (OneUptime)](https://oneuptime.com/blog/post/2026-03-31-clickhouse-cloud-vs-open-source-comparison/view)

### Other databases referenced
- GreptimeDB — [GitHub](https://github.com/GreptimeTeam/greptimedb) · [site](https://greptime.com/)
- QuestDB — [GitHub](https://github.com/questdb/questdb) · [high-availability docs](https://questdb.com/docs/high-availability/overview/)
- [TDengine](https://tdengine.com/) · [Apache Druid](https://druid.apache.org/) · [Apache Pinot](https://pinot.apache.org/) · [Apache IoTDB](https://iotdb.apache.org/)
- [TimescaleDB / TigerData](https://www.tigerdata.com/) · [v2.14 release — multi-node removed](https://github.com/timescale/timescaledb/releases/tag/2.14.0)
- [InfluxDB](https://www.influxdata.com/) · [InfluxDB 3 Core 72-hour limitation explained](https://www.influxdata.com/blog/influxdb3-open-source-public-alpha-jan-27/)
- [VictoriaMetrics](https://victoriametrics.com/) · [Prometheus](https://prometheus.io/) · [CrateDB](https://cratedb.com/)
