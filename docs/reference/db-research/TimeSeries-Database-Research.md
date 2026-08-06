# Time-Series Databases — The Full Story

*Research documentation for the System Analysis Framework (Analysis FW)*
*Prepared: July 2026 · Audience: everyone on the team, no database background required*

---

## 1. Why this document exists

Our System Analysis Framework will collect huge amounts of time-stamped data from AI systems under test — per-IO traces from eBPF/blktrace, CPU/GPU/memory utilization, NVMe queue stats, SSD telemetry — and the Analysis FW must store and analyze it. We evaluated the time-series database (TSDB) landscape and chose **ClickHouse**.

This document explains the *full picture* behind that choice: why time-series databases exist at all, what real-world problem gave birth to each one, how each works inside, and how they all fit into categories. By the end, the reasoning for our choice should feel obvious rather than arbitrary.

---

## 2. What is time-series data, and why normal databases struggle with it

Time-series data is any data where **every record has a timestamp and the timestamp is the main thing you organize by**: a CPU reading every second, a disk IO event, a temperature sample, a stock price tick.

It has a very particular personality:

- **It arrives relentlessly.** Not one order per customer per day — thousands to millions of points per second, forever.
- **It is written once and almost never changed.** You don't go back and "edit" what the CPU usage was at 10:03:07.
- **Recent data is hot, old data is cold.** Everyone asks about the last hour; almost nobody asks about a random Tuesday two years ago — and old data can usually be summarized or deleted.
- **Queries are about ranges and math, not single records.** "Average latency between 2pm and 3pm, grouped by process" — not "fetch record #4711".

A traditional database (MySQL, Postgres, Oracle) is built for the opposite personality: moderate write rates, frequent updates, and queries that fetch or change *individual rows*. Point one of them at millions of appends per second and range-scans over billions of rows, and three things break: inserts get slow (index maintenance), storage explodes (row format compresses poorly), and analytical queries crawl (reading whole rows when you need one column).

Every database in this document exists because someone hit one of those three walls in a specific real-world situation. That's the story we'll follow.

---

## 3. The Evolution — six eras, six pains

```
1999          2006          2010            2013~15           2015~19            2019~now
 │             │             │                │                  │                  │
 RRDtool      Graphite     OpenTSDB        InfluxDB           M3, Cortex        TDengine
 (fixed-size  (web-scale   KairosDB        Prometheus         Thanos, VM        QuestDB
  graphs for   metrics      Druid           Atlas              TimescaleDB       IoTDB
  routers)     for web      (Big-Data                          ClickHouse OSS    GreptimeDB
               companies)    era)                              Pinot, CrateDB    Mimir ...
 "graph it"   "centralize  "store it       "purpose-built     "scale it out,    "specialize:
               it"          all forever"    for time-series"   or keep SQL"      IoT, finance,
                                                                                  cloud-native"
```

### Era 1 (1999) — "Just graph my router" → RRDtool

The internet was growing, and network administrators needed one simple thing: a graph of bandwidth on a router over time. Disks were tiny, so storing everything forever was unthinkable.

**RRDtool** (Round-Robin Database) solved this with a clever trick: a **fixed-size file** that never grows. Recent data is kept detailed; as data ages, it is automatically averaged into coarser summaries (per-hour, per-day) — the original **downsampling**. When the file is "full", the oldest data falls off the end, like a conveyor belt.

*The pain it left behind:* one rigid file per metric, no way to ask ad-hoc questions, no central server. Fine for 50 routers; hopeless for a web company.

### Era 2 (2006) — "We have thousands of servers now" → Graphite

Web companies (Graphite was born at the travel site Orbitz) suddenly ran *thousands* of servers and wanted every team to push metrics to **one central place** and build dashboards freely.

**Graphite** kept RRDtool's file-per-metric idea (its storage engine is called Whisper) but added a network daemon to receive metrics and a web API to graph anything. It became the standard for a decade.

*The pain it left behind:* millions of metric files = the disk drowns in small random writes; scaling beyond one machine meant manual, fragile sharding; no data compression.

### Era 3 (2010–2013) — "Store everything forever" → OpenTSDB, KairosDB, and Druid

The Big-Data / Hadoop era arrived. Companies now *had* distributed storage clusters and a new attitude: never throw data away, never lose precision through downsampling.

- **OpenTSDB** (born at StumbleUpon, 2010) said: don't build storage — put billions of metric points into **HBase** (Hadoop's database), which already scales across machines.
- **KairosDB** did the same thing on top of **Cassandra**.
- Meanwhile in ad-tech, **Druid** (Metamarkets, 2011) faced a different flavor of the problem: advertising dashboards needed **interactive slice-and-dice over billions of events as they streamed in**. Relational databases were too slow, HBase too inflexible — so they built a new engine around time-partitioned, indexed, columnar segments.

*The pain it left behind:* running Hadoop/HBase just to store metrics is a huge operational tax, and these systems had no analytics language to speak of.

### Era 4 (2013–2015) — "Time-series deserves its own database" → InfluxDB, Prometheus

Two teams, two very different motivations, one conclusion: stop bolting time-series onto other systems and design a database for it from scratch.

- **InfluxDB** (2013): a startup building a monitoring product realized the *storage engine itself* was the product. They built a batteries-included TSDB — easy to install, SQL-like query language, tags on every point, retention and continuous queries built in. It became the most popular TSDB by adoption.
- **Prometheus** (started 2012 at SoundCloud, released 2015): ex-Google engineers missed Google's internal monitoring (Borgmon). The world was moving to **containers and microservices** — servers appearing and disappearing every minute — and tools built for static server lists couldn't cope. Prometheus flipped the model: it *pulls* metrics from your services, attaches **labels** (key=value dimensions) to every series, and ships a query language (PromQL) and alerting designed for operations. It became the standard monitoring system of the Kubernetes era.
- **Netflix Atlas** (2014) deserves a footnote: Netflix needed near-instant dashboards over operations metrics and built an in-memory dimensional store — fast, but memory-expensive, and few outside Netflix run it.

*The pain it left behind:* InfluxDB's free version stayed single-node (clustering became the paid product), and it struggled when metrics had huge numbers of unique tag values (**high cardinality**). Prometheus was *deliberately* single-node — great for one cluster, but no long-term storage, no global view across data centers.

### Era 5 (2015–2019) — Two parallel revolutions: "scale it out" and "keep SQL"

**Revolution A — scaling the Prometheus world.** Everybody loved Prometheus's model but needed years of retention, multiple data centers, and multi-team isolation. A whole family appeared whose job is "Prometheus, but bigger":

- **M3DB** (Uber, open-sourced 2018): Uber's Graphite setup collapsed under billions of metrics from every city; they built their own replicated, sharded metric store.
- **Cortex** (2016): Weaveworks wanted to sell *hosted* Prometheus, so they built a multi-tenant, horizontally scalable backend that many Prometheus servers push into.
- **Thanos** (2018): the gaming company Improbable ran many Prometheus servers and wanted a **global query view and cheap long-term storage in S3** *without replacing anything* — Thanos wraps existing Prometheus servers.
- **VictoriaMetrics** (2018): one engineer's conviction that all of the above were too complex and too hungry — a from-scratch rewrite obsessed with compression and simplicity that is faster and lighter, and whose clustered version is fully open source.

**Revolution B — the SQL renaissance.** A different group of people looked at InfluxDB's custom language and NoSQL-era designs and said: *we already know SQL, and our time-series data needs to join with our business data*.

- **TimescaleDB** (2017): a startup building an IoT platform realized their real product was "Postgres, but good at time-series." It's a Postgres **extension**: you keep full SQL, joins, and the entire Postgres ecosystem; Timescale adds automatic time-partitioning, compression, and continuous aggregates.
- **CrateDB** (~2014): distributed SQL over "machine data" — sensor readings *plus* the registry/location/customer tables they relate to, in one cluster.
- **ClickHouse** (open-sourced 2016): built at Yandex since ~2009 to power Yandex.Metrica — Europe's competitor to Google Analytics — where analysts needed **arbitrary, interactive queries over billions of page-view events**. Not a TSDB by label, but a general **columnar analytics engine** so fast and so good at time-ordered event data that it became one of the most popular time-series backends in the world.
- **Apache Pinot** (LinkedIn, open-sourced 2015): LinkedIn wanted to show *every member* "Who viewed your profile" — analytics served not to ten analysts but to **hundreds of millions of end users**, demanding milliseconds per query at enormous query rates.

*The pain left behind:* still nothing great for the physical world — factories, vehicles, meters — where data comes from millions of identical devices, often through weak edge hardware.

### Era 6 (2019–now) — Specialization and the cloud-native rebuild

- **TDengine** (2019): built on the observation that IoT data is *extremely regular* — every device sends the same fields on a schedule. Exploiting that regularity (internally: one table per device) buys extreme ingest speed and compression. Its clustering is open source — a direct answer to InfluxDB's paid clustering.
- **Apache IoTDB** (Tsinghua University, Apache top-level 2020): born from industrial projects (metro trains, factories) where the database must run **on the edge device itself** — sometimes on very weak hardware — and sync upward to a cloud cluster. Its file format (TsFile) is designed for sensor data the way Parquet is for tables.
- **QuestDB** (2019–2020): from the world of **financial tick data** — nanosecond timestamps, millions of rows per second, SQL — with an ingestion path engineered like a trading system.
- **GreptimeDB** (2022): the cloud-native rethink — compute separated from storage (data lives on S3), one engine for metrics *and* logs *and* events, queryable by both SQL and PromQL.
- **Grafana Mimir** (2022): Grafana Labs took Cortex and rebuilt it to handle a **billion active series**, becoming the heavyweight of the Prometheus-scaling family.
- **openGemini** (Huawei), **HoraeDB** (Ant Group — built for *extreme* label cardinality), **SiriDB**, **Warp 10** (time-series + geolocation): regional and niche players filling specific gaps.

**The takeaway of the whole story:** nobody set out to build "a time-series database" in the abstract. Each system is a fossil of a specific pain: RRDtool of tiny disks, Graphite of the first web farms, OpenTSDB of the Hadoop era, Prometheus of the container revolution, TimescaleDB of SQL nostalgia (in the best sense), ClickHouse of web-scale analytics, Pinot of user-facing apps, TDengine/IoTDB of the physical world. **You choose a TSDB by matching your pain to theirs.**

---

## 4. Each database up close: the need, the use case, the design

*Grouped by family. For each: why it was born, what it's for, how it works inside (short), and why that design solves its problem.*

### 4.1 The Monitoring / Metrics family

*Their world: numeric health measurements (CPU, request rate, error count) with labels. Their queries: "graph/alert on metric X over time window Y". They store metrics ONLY — they cannot store event records like our per-IO traces.*

#### Prometheus
- **Born because:** container-era infrastructure changes every minute; monitoring needed service discovery, dimensions, and alerting as first-class citizens.
- **Use case:** monitoring one Kubernetes cluster / one environment; the de-facto standard.
- **Inside:** a single binary that *pulls* (scrapes) metrics over HTTP, stores them in 2-hour blocks with **Gorilla compression** (store only the tiny difference between consecutive values — a sample shrinks to ~1.4 bytes), indexes series by label, evaluates alert rules continuously.
- **Why it works:** pulling + service discovery means new pods are monitored automatically; the compression makes a laptop-sized machine hold a surprising amount of data. Deliberately single-node — simplicity over scale.

#### VictoriaMetrics
- **Born because:** the Prometheus-scaling options (Era 5) felt heavy and resource-hungry to one determined engineer.
- **Use case:** drop-in long-term/scale-out backend for Prometheus data; the "simplest thing that scales".
- **Inside:** a from-scratch rewrite in Go with its own storage format; better compression than Prometheus; a cluster version with three simple roles (insert, store, select) that is fully open source.
- **Why it works:** obsessive optimization — fewer moving parts, less RAM, less disk per sample. The catch we noted: **automatic downsampling is enterprise-only**.

#### Thanos
- **Born because:** a company with many Prometheus servers wanted one global search box and years of retention *without replacing Prometheus*.
- **Use case:** federate existing Prometheus servers; archive to object storage (S3).
- **Inside:** sidecar processes attach to each Prometheus, upload finished blocks to S3; a query layer fans out to everything; a compactor tidies old blocks and **automatically downsamples** (5-minute and 1-hour resolutions).
- **Why it works:** it adds a layer instead of replacing — adoption is incremental; S3 makes history nearly free.

#### Cortex → Grafana Mimir
- **Born because:** hosted, multi-tenant "Prometheus as a service" (Cortex, 2016); Grafana Labs later rebuilt it for a billion active series (Mimir, 2022).
- **Use case:** very large organizations — many teams, many clusters, one central metrics platform with per-team isolation.
- **Inside:** microservices (ingesters, queriers, compactors, ruler…) that scale horizontally, storing blocks in object storage; every tenant's data logically separated.
- **Why it works:** each bottleneck gets its own independently scalable service. The price: many moving parts to operate.

#### M3DB
- **Born because:** Uber's metric volume (every car, every city, every service) crushed Graphite.
- **Use case:** Uber-scale metric storage with replication.
- **Inside:** sharded, replicated custom store with its own compression (M3TSZ) and an aggregation tier for rollups.
- **Why it works:** built for exactly one thing — sustained massive metric ingest with fault tolerance. Community activity has declined; historically important, rarely the right new choice.

#### The legacy elders: RRDtool, Graphite, OpenTSDB, KairosDB
- **RRDtool:** fixed-size files, automatic aging of data into summaries. Perfect for its 1999 problem; today survives inside old network tools.
- **Graphite:** centralized metrics for the first web farms; file-per-metric storage that drowns at scale; no compression. Still met in older shops.
- **OpenTSDB / KairosDB:** "let HBase/Cassandra do the scaling." Powerful for their day; today you'd only pick them if you already operate those clusters.

### 4.2 The SQL / General-purpose family

*Their world: time-series that must remain queryable with full SQL, often joined against ordinary business tables.*

#### InfluxDB
- **Born because:** (2013) time-series deserved a purpose-built, easy, batteries-included database rather than files or Hadoop.
- **Use case:** the popular default for DevOps and IoT metrics on a single node; retention, continuous queries, dashboards — all built in.
- **Inside:** an LSM-style engine (write to memory + log, flush to sorted immutable files, merge in background) with time-organized shards; tags indexed for filtering; version 3 rebuilt the core in Rust around Apache Arrow/Parquet with SQL support.
- **Why it works:** the whole write path is append-friendly, and time-sharding makes retention (drop a whole shard) trivial. The catch: **free version is single-node**; clustering is the paid product — this shaped half of the later ecosystem.

#### TimescaleDB
- **Born because:** (2017) an IoT startup wanted time-series performance *without giving up Postgres and SQL joins*.
- **Use case:** time-series living next to relational data — device registries, users, configs — with full SQL; moderate scale.
- **Inside:** a Postgres extension that secretly splits your big table into small **time chunks** (partitions), compresses old chunks into a columnar format (90%+ typical), maintains **continuous aggregates** (always-up-to-date rollup tables), and offers `lttb()` for graph-friendly downsampling.
- **Why it works:** chunk pruning means a "last 24 hours" query never touches last year's data; and it *is* Postgres, so every tool, driver, and JOIN just works. The catches: multi-node was **removed** (2024), and the advanced features use Timescale's own license (free to self-host, not fully open source).

#### QuestDB
- **Born because:** (2019) financial markets — tick data at millions of rows/second with nanosecond timestamps, queried in SQL.
- **Use case:** the fastest single-node SQL ingest in the open-source TSDB world; finance, sensor firehoses.
- **Inside:** columnar storage, time-partitioned, with a zero-copy ingestion path engineered like a trading system; `SAMPLE BY` for time bucketing; recently added TTL and materialized views.
- **Why it works:** it removes every layer between the network and the disk. The catch: open-source version is single-node (replication is enterprise).

#### CrateDB
- **Born because:** (~2014) industrial "machine data" needed both scale-out ingest *and* real SQL with joins and full-text search.
- **Use case:** sensor/machine data that must be queried together with its business context (which machine, which customer, which site) in one distributed SQL cluster.
- **Inside:** a shared-nothing SQL engine whose storage layer is Lucene (the search library behind Elasticsearch) — every column indexed by default; any node can take any query.
- **Why it works:** indexes-everywhere makes ad-hoc filtering fast without planning; the trade is heavier storage and no built-in continuous aggregation.

### 4.3 The IoT family

*Their world: millions of physical devices sending the same few fields on a schedule, often via weak edge hardware; extreme compression matters because the data never stops.*

#### TDengine
- **Born because:** (2019) general TSDBs wasted the most valuable property of IoT data — its *regularity* — and the popular option (InfluxDB) kept clustering behind a paywall.
- **Use case:** large fleets of similar devices: meters, vehicles, industrial sensors. Clustering included in the open-source build.
- **Inside:** "one table per device, one super-table per device type" — since each device's data is uniform and arrives in time order, it is stored contiguously and compresses exceptionally; built-in streams provide continuous computation and alerts.
- **Why it works:** the schema *is* the optimization: no tag-index explosions, no scattered writes. The trade: it's shaped for uniform sensor fleets, not free-form event analytics. License: AGPL.

#### Apache IoTDB
- **Born because:** industrial projects needed a database that runs *on* the machine (edge), survives factory-grade messiness (out-of-order, gaps), and syncs to a cloud cluster.
- **Use case:** edge-to-cloud industrial telemetry: trains, turbines, production lines.
- **Inside:** a purpose-built columnar file format (TsFile) with per-sensor encodings; a tree-style data model matching physical hierarchies (plant → line → machine → sensor); cluster mode fully open source; even ships an LTTB downsampling function.
- **Why it works:** the same small engine scales from a gateway box to a cluster, so data flows upward without format conversions.

#### GreptimeDB
- **Born because:** (2022) in the cloud era, storing observability data on local disks — and running three systems for metrics, logs, and events — looks wasteful.
- **Use case:** greenfield observability platforms; unified store queryable in SQL *and* PromQL, with data on object storage.
- **Inside:** compute/storage separation — stateless query nodes over data in S3 (Parquet-based); distributed mode in the open-source build; "flows" for continuous aggregation.
- **Why it works:** S3 makes retention nearly free and scaling elastic. The trade: it's young — smaller community, faster-moving ground.

#### The niche corner
- **openGemini** (Huawei): clustered, InfluxQL-compatible — an exit ramp for teams that outgrew free InfluxDB.
- **HoraeDB** (Ant Group → Apache incubator): built for metrics with *millions of unique label values* (per-user, per-order labels) that choke classic TSDBs.
- **SiriDB:** small, MIT-licensed, natively clustered; tiny community.
- **Warp 10:** time-series *plus location* — GPS tracks, fleets, ships; strong geo-temporal query language, heavy Hadoop-era scaling.

### 4.4 The Real-Time OLAP family

*Their world: not "metrics" but* events *— billions of individual records (a page view, an ad impression, an IO operation) that people want to slice, dice, and aggregate interactively. This is our framework's world.*

#### ClickHouse — our choice
- **Born because:** Yandex.Metrica had to let analysts run *any* query over billions of page-view events and get answers in under a second — pre-computing every possible answer was impossible because the questions weren't known in advance. (Note the exact parallel to our situation.)
- **Use case:** the general-purpose analytics workhorse: event logs, clickstreams, traces, metrics, tick data — huge scans, ad-hoc SQL, few concurrent users.
- **Inside (the four tricks):**
  1. **Columnar storage** — each column lives in its own file; a query reads only the 3 columns it needs out of 50, not whole rows.
  2. **MergeTree** — data lands as sorted, immutable chunks merged in the background; writing is pure sequential append (fast), and nothing is ever edited in place.
  3. **Aggressive compression** — similar values sit next to each other in a column, so data shrinks 10× or more, with special codecs for timestamps and gauges; less disk read = faster queries.
  4. **Vectorized execution** — queries process values in big batches using SIMD (one CPU instruction operating on many values at once), across all cores, across all nodes.
  Plus: sparse indexes that *skip* irrelevant data blocks, table-level **TTL** for retention (old data can even auto-collapse into aggregates), **materialized views** for continuous aggregation, and a native **LTTB** function for graph-friendly downsampling.
- **Why it works:** every layer is designed to make *full scans absurdly cheap*, which is exactly what "we don't know the queries yet" requires. The trades: single-row updates are expensive (irrelevant for us — we never edit a trace), and inserts must be **batched** (fits our log-then-ingest pipeline perfectly).

#### Apache Druid
- **Born because:** ad-tech dashboards needed events queryable *seconds after they happen*, with sub-second drill-downs, while data streams in nonstop.
- **Use case:** always-on streaming analytics dashboards fed by Kafka.
- **Inside:** events sit in memory on ingestion nodes (instantly queryable) and are periodically sealed into time-partitioned, bitmap-indexed columnar **segments** stored in S3 and served by historical nodes; a query transparently merges the live and historical lanes. A coordinator auto-balances segments across nodes.
- **Why it works:** the two-lane design buys freshness; time partitioning and bitmap indexes buy speed. The trades: ~six different service types plus ZooKeeper and a metadata DB to operate; joins are weak; SQL is limited.

#### Apache Pinot
- **Born because:** LinkedIn wanted to put analytics in front of *every member* ("Who viewed your profile?") — meaning millisecond answers at thousands of queries per second, a load no analyst-oriented engine could take.
- **Use case:** analytics features inside customer-facing apps.
- **Inside:** same broad shape as Druid (streaming + batch into columnar segments, brokers routing queries), but with a **toolbox of per-column indexes** — most famously the **star-tree index**, which pre-computes aggregations so a common query becomes a lookup instead of a scan; partition-aware routing sends each query to only the 1–2 servers that hold the answer.
- **Why it works:** tiny work per query × minimal cross-node chatter = enormous concurrency. The trades: you must know your queries to configure those indexes (the opposite of our situation), complex SQL is weak, and operations are heavy.

---

## 5. Putting it all together — the categories

### 5.1 By family (what kind of data, what kind of questions)

| Family | Members | Data they hold | Typical question |
|---|---|---|---|
| **Monitoring / metrics** | Prometheus, VictoriaMetrics, Thanos, Cortex, Mimir, M3DB, Atlas | Numbers with labels, sampled on a schedule | "Graph/alert on CPU for service X" |
| **SQL / general-purpose TSDB** | InfluxDB, TimescaleDB, QuestDB, CrateDB | Measurements *and* related business tables | "Join sensor data with the device registry" |
| **IoT / sensor** | TDengine, IoTDB, GreptimeDB, openGemini, HoraeDB, SiriDB, Warp 10 | Uniform readings from device fleets | "Average vibration per pump per hour" |
| **Real-time OLAP (event analytics)** | **ClickHouse**, Druid, Pinot | Billions of individual event records | "Any aggregation over any slice, fast" |
| **Legacy elders** | RRDtool, Graphite, OpenTSDB, KairosDB | Metrics, by the standards of their era | (maintenance mode) |

### 5.2 By workload category (from our earlier discussion)

| Workload | Meaning | Databases |
|---|---|---|
| **Real-time monitoring analytics** | Constant ingest, dashboard/alert reads | Prometheus family, VictoriaMetrics, Mimir, Thanos, M3, Graphite, OpenTSDB |
| **Real-time OLAP** | Streaming ingest + interactive slicing of events | ClickHouse, Druid, Pinot |
| **HTAP-leaning** | Time-series + transactional/relational in one | TimescaleDB, CrateDB |
| **High-ingest specialist** | Engineered around maximum write speed | QuestDB, TDengine, IoTDB |
| **Embedded / fixed-size** | Runs inside another tool | RRDtool |

### 5.3 By license and free clustering (the practical filter)

| Fully free **and** multi-node in the free version | Free but **single-node only** (clustering paid/removed) | Free with caveats |
|---|---|---|
| ClickHouse, Druid, Pinot, VictoriaMetrics cluster, Thanos, Cortex, Mimir (AGPL), M3DB, TDengine (AGPL), IoTDB, GreptimeDB, CrateDB, openGemini | InfluxDB, QuestDB, TimescaleDB (multi-node removed), Prometheus (by design) | TimescaleDB's compression/continuous-aggregates use Timescale's own license (free to self-host, not OSI open source) |

### 5.4 The one-line map

> **Metrics from servers** → Prometheus + VictoriaMetrics/Mimir · **Sensors and devices** → TDengine / IoTDB · **Time-series next to business SQL** → TimescaleDB / CrateDB · **Billions of events, unknown questions** → **ClickHouse** · **Streaming dashboards** → Druid · **Analytics inside your app** → Pinot

---

## 6. What this means for our System Analysis Framework

Our situation, restated in this document's language:

1. **Our data is events, not just metrics.** The dominant volume is per-IO trace records (timestamp, PID, LBA, size, latency, queue, layer) from eBPF/blktrace — potentially millions per second during a run — plus periodic CPU/GPU/memory/SSD metrics. The entire monitoring family is disqualified by data model alone.
2. **We don't know our queries yet.** The analyses we *do* know (latency distributions, LBA hotness, burst detection, queue-depth stats, cross-layer correlation) are all aggregations and joins over raw events — and more will be invented as we explore. That rules out engines that must be tuned to known queries (Druid's rollups, Pinot's star-trees) and demands full SQL over raw data.
3. **Our ingest is batch-shaped.** Profiling must not disturb the system under test, so we log locally during a run and ingest afterwards — which neutralizes ClickHouse's only real weakness (it dislikes tiny per-row inserts) and plays to its strength (bulk loads).
4. **We are a small team.** A single-binary engine we can run on one box today and shard later beats a six-service cluster with ZooKeeper.
5. **Our safety net:** the Profile FW already mandates a defined raw-log folder hierarchy. Those files remain the source of truth; the database is a *rebuildable index* over them. If the future proves us wrong, we re-ingest into something else — this decision is not a one-way door.

ClickHouse was born from exactly our problem — *"analysts must be able to ask anything over billions of events and get answers interactively"* — and it checks every box in our capability matrix: Apache 2.0 license, free multi-node, extreme batched ingest, columnar compression, TTL-based retention, materialized views for continuous aggregation, native LTTB downsampling, full SQL, raw event support, and high-cardinality tolerance.

**Decision: ClickHouse, starting single-node, with Grafana for the UI and the raw log hierarchy as our reversibility guarantee.**

---

## 7. ClickHouse deep dive — who uses it, where it struggles, and how to benchmark it

*Added after finalizing ClickHouse for the Analysis FW. This section answers three practical questions: who trusts it in production and for what, what it is genuinely bad at, and how to measure it ourselves.*

### 7.1 Who uses ClickHouse in production, and for what

ClickHouse reports ~900+ verified companies in production. What matters for us: the single biggest category is **observability / telemetry over machine-generated events** — which is exactly what our framework does. The marquee names below are running the same workload shape we're building.

**Observability / logging / monitoring** *(our category — most relevant)*

| Company | Use case | Scale mentioned |
|---|---|---|
| **Anthropic** | Observability for AI infrastructure; credited as "instrumental" in shipping Claude 4, run by a 3-person team on an air-gapped in-house ClickHouse | — |
| **Cloudflare** | DNS analytics & automated monitoring | ~7M rows/sec on just 24 servers; original 2017 system did 1M+ DNS queries/sec |
| **Uber** | Central logging platform | billions of events/sec |
| **Didi** | Observability platform | petabytes/day, 40 GB/s ingest |
| **eBay** | Logs, metrics, and events platform | — |
| **Sentry** | Error/exception tracking (their core product) | — |
| **Shopify**, **Character.AI** | Bespoke observability platforms | — |

**Product & web analytics**

| Company | Use case |
|---|---|
| **PostHog** | Product analytics — ClickHouse *is* their core engine |
| **Plausible** | Privacy-friendly web analytics (their core engine) |
| **Contentsquare**, **Yandex Metrica** | Web analytics (Metrica is where ClickHouse was born) |
| **Instacart** | Retailer/ads dashboards, A/B testing, ML signals |
| **Spotify** | Experimentation (A/B test) platform |
| **Segment** | Data processing (9 nodes, 7.5 TB SSDs) |

**Ad tech & finance** (speed-critical)

| Company | Use case | Scale |
|---|---|---|
| **Traffic Stars** | Ad network | 1.8 PiB, ~300 servers |
| **LifeStreet** | Ad network | 5.27 PiB, 75 servers |
| **Criteo**, **Geniee**, **MGID** | Ad/retail analytics | — |
| **Bloomberg**, **Deutsche Bank**, **Amadeus** | Financial monitoring / high-frequency data | — |

**High-volume analytics at scale**

| Company | Use case | Scale |
|---|---|---|
| **Ahrefs** | SEO/backlink analytics | 100k+ CPU cores, ~1 EB uncompressed |
| **Disney+** | Video streaming analytics | 395 TiB |
| **Twilio SendGrid** | Email analytics | 10B events/day |
| **DigiCert** | DNS platform | 35B events/day |
| **Roblox** | Gaming safety/trust operations | 100M events/day |
| **Lyft**, **DeepL**, **SEMrush** | Real-time dashboards / ML data / marketing analytics | — |

**Why this matters for us:** ClickHouse is increasingly the default "real-time data platform behind AI" (OpenAI, Anthropic, Tesla, Meta, Cloudflare are cited users). Our workload — high-volume machine-generated event/trace telemetry, analyzed with ad-hoc SQL — is the exact center of gravity of its adopter base, not an edge case. The Anthropic and Uber/Cloudflare observability stories are the closest public analogues to our Analysis FW.

**Reference links to keep:**
- Official adopters list (companies + scale) — https://clickhouse.com/docs/about-us/adopters
- User stories / case studies index — https://clickhouse.com/user-stories
- Use-cases overview — https://clickhouse.com/use-cases
- Anthropic observability case study — https://clickhouse.com/blog/how-anthropic-is-using-clickhouse-to-scale-observability-for-ai-era
- Cloudflare's classic "HTTP analytics at scale" / DNS at 1M+ qps writeup — see the Cloudflare engineering blog linked from the adopters page
- 2025 feature roundup — https://clickhouse.com/blog/clickhouse-2025-roundup
- ClickStack (their observability stack) review — https://clickhouse.com/blog/clickstack-a-year-in-review-2025

### 7.2 Known issues — what ClickHouse is NOT good at

This is the honest "cons" list. Most of these do **not** affect our framework (we do batched, append-only, read-mostly analytics), but the team should know them so we don't misuse it.

| # | Weakness | Why it happens (implementation) | Affects us? |
|---|---|---|---|
| 1 | **Single-row / small inserts are bad** | Every insert creates a new "part" that must be merged in the background; thousands of tiny inserts overwhelm the merge machinery | **No** — we log to files during a run and bulk-load afterward (batched inserts are ideal). *Rule: buffer to ≥1,000-row batches.* |
| 2 | **Updates & deletes are slow** | They run as asynchronous background "mutations" that rewrite whole parts; on big tables a mutation can run for hours/days | **No** — trace data is write-once; we never edit an IO record |
| 3 | **No real transactions** | No multi-statement ACID transactions, rollbacks, or updatable views | **No** — we're not doing transactional writes |
| 4 | **Point lookups / "fetch one row" are inefficient** | Columnar layout means one row is scattered across many column files; row stores (Postgres) win here | **No** — we ask aggregate questions, not "fetch record #4711" |
| 5 | **Large table↔table JOINs can be slow or OOM** | Rule-based planner (not cost-based); the right table is loaded into memory for hash joins | **Partly** — keep dimension/metadata tables small; denormalize where possible. Our cross-layer joins should be event-table ⋈ small-lookup, which is fine |
| 6 | **No memory limits by default → OOM kills** | Big aggregations/joins buffer in RAM; ~60% of deployments reportedly run with no limits set | **Yes (ops)** — set `max_memory_usage` and per-query limits from day one |
| 7 | **Schema changes on huge tables are heavy** | ALTER can rewrite data; hours on very large tables | **Minor** — design the schema deliberately up front; add columns rarely |
| 8 | **Not a stream processor** | No built-in stateful windowing/sessionization/CEP; needs Flink/Kafka-side logic for that | **No** — we ingest batches, not live streams |
| 9 | **HA/clustering needs expertise** | Replication needs ClickHouse Keeper (or ZooKeeper) and careful setup | **Later** — irrelevant single-node; revisit if we shard |
| 10 | **Not a full data-warehouse suite / docs gaps** | No bundled ETL/orchestration; docs can be thin on advanced topics | **Minor** — we bring our own ingest scripts anyway |

**One-line summary of "when NOT to use ClickHouse":** transactional apps (OLTP), key-value/point-lookup workloads, frequent single-row updates/deletes, tiny datasets, and stateful stream processing. **None of these describe our Analysis FW** — which is precisely why it fits.

**Reference links to keep:**
- "When NOT to use ClickHouse" (ChistaDATA) — https://chistadata.com/when-not-to-use-clickhouse/
- Official "13 getting-started mistakes and how to avoid them" — https://clickhouse.com/blog/common-getting-started-issues-with-clickhouse
- JOIN limitations explained (GlassFlow) — https://www.glassflow.dev/blog/clickhouse-limitations-joins
- Altinity KB — schema-design limits ("how much is too much") — https://kb.altinity.com/altinity-kb-schema-design/how-much-is-too-much/
- Lightweight-delete known issues (GitHub #39870) — https://github.com/ClickHouse/ClickHouse/issues/39870
- Balanced practitioner review ("The Good, The Bad, and The Ugly") — https://dev.to/lindesvard/clickhouse-the-good-the-bad-and-the-ugly-2pi7

### 7.3 Benchmarking resources

Two benchmarks matter for us: **ClickBench** (general analytics, ClickHouse's own but run fairly across ~60 systems) and **TSBS** (time-series-specific, vendor-neutral). Use both to validate on *our* data before committing hardware.

**ClickBench — analytical DBMS benchmark**
- Live dashboard comparing 60+ systems — https://benchmark.clickhouse.com/
- Source & methodology (GitHub) — https://github.com/ClickHouse/ClickBench
- **What it is:** 43 analytical queries (full scans, filtered scans, aggregations) over a single flat table of ~100M rows of real web-analytics data on a standard AWS `c6a.4xlarge`. Reports cold-run and hot-run times, load time, and on-disk size. ClickHouse did no query-specific tuning, so cross-system comparison is reasonably fair.
- **Why relevant:** its workload — "clickstream, machine-generated data, structured logs, events" — is essentially our workload.

**TSBS (Time Series Benchmark Suite) — time-series-specific**
- Original (Timescale) — https://github.com/timescale/tsbs
- Fork with more DBs (TDengine) — https://github.com/taosdata/tsbs
- **What it is:** the standard cross-vendor TSDB benchmark (used by InfluxDB, TimescaleDB, QuestDB, ClickHouse). Measures ingest rate and a suite of time-range/aggregation queries.
- **Published findings worth citing:** ClickHouse loaded ~4M metrics/sec (~400K rows/sec) in Altinity's TSBS run — ~3× faster ingest than TimescaleDB and InfluxDB — and matched or beat them on query latency, pulling far ahead on I/O-intensive queries. Independent 2026 comparisons put ClickHouse time-series compression around 15–30× vs ~10–15× for TimescaleDB.

**Other benchmark references:**
- Altinity — "ClickHouse for Time Series" / scalability — https://altinity.com/blog/clickhouse-for-time-series
- KX TSBS comparison (KDB-X vs QuestDB/ClickHouse/TimescaleDB/InfluxDB) — https://kx.com/blog/benchmarking-kdb-x-vs-questdb-clickhouse-timescaledb-and-influxdb-with-tsbs/
- Independent ClickHouse vs TimescaleDB vs InfluxDB (2026) — https://sanj.dev/post/clickhouse-timescaledb-influxdb-time-series-comparison/
- `clickhouse-benchmark` built-in load-testing tool — https://clickhouse.com/docs/operations/utilities/clickhouse-benchmark

> **Caveat on all benchmarks:** every vendor benchmark flatters its own product, and results swing hard with hardware, schema, and query mix. Treat published numbers as directional. The only number that decides our design is a **ClickBench-style run on our own trace/telemetry data on our own analysis box** — which we should do during the Q3 Analysis FW design phase, using `clickhouse-benchmark` with the ~10 queries from Frameworks.txt §137 as the query set.

---

*Appendix — sources of truth for this document: team discussion notes (July 2026) comparing capability matrices across InfluxDB, TimescaleDB, QuestDB, TDengine, GreptimeDB, IoTDB, CrateDB, Prometheus, VictoriaMetrics, Mimir, Thanos, Cortex, M3DB, ClickHouse, Druid, Pinot, openGemini, and legacy systems. Licensing and feature claims (e.g., VictoriaMetrics enterprise-only downsampling, TimescaleDB multi-node removal, InfluxDB/QuestDB single-node OSS) reflect the state as of mid-2026 and should be re-verified before any future re-evaluation, as vendors change licenses frequently.*
