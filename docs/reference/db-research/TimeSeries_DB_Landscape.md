# Time-Series Database Landscape — Why Each Exists and How Each Works

*Companion to [DB_Study_TimeSeries_Comparison.md](DB_Study_TimeSeries_Comparison.md). That doc answers "which one for us"; this one answers "why do so many exist, what was each built for, and why are they built differently."*

---

## 1. The key insight: every database fossilizes the workload it was born from

Nobody builds a time-series database in the abstract. Each was built because a specific company hit a specific wall with a specific data shape — and the architecture that got them past that wall is frozen into the product forever. That's why there are "so many": there are several fundamentally different time-shaped workloads, and each spawned its own family:

| Family | Birth workload | Data shape | Members |
|---|---|---|---|
| **Event analytics (OLAP)** | Web/ad analytics dashboards | Irregular, high-cardinality events; ad-hoc queries | ClickHouse, Druid, Pinot |
| **Metrics monitoring** | DevOps/infrastructure monitoring | Regular numeric samples, few labels, alerting | Prometheus, VictoriaMetrics, OpenTSDB, InfluxDB v1 |
| **SQL time-series** | "We refuse to leave Postgres/SQL" | Relational data with a time axis | TimescaleDB, QuestDB |
| **IoT / device telemetry** | Industrial sensors, connected cars | Millions of devices × regular structured samples | TDengine, Apache IoTDB |
| **Cloud-native unified observability** | Cost + cardinality crisis of the metrics family | Metrics+logs+traces as one "wide event" model on object storage | GreptimeDB, InfluxDB 3 |

(Ancestors for context: **RRDtool/Graphite** — fixed-size ring files for server graphs, the 1999–2008 era; **kdb+** — proprietary columnar tick-data engine from finance, the spiritual ancestor of QuestDB; Facebook's **Gorilla** paper (2015) — the compression algorithms everyone now uses.)

---

## 2. Database by database

### ClickHouse — "scan everything, absurdly fast"
- **Born:** Yandex, ~2009 internal, open-sourced 2016. Built for **Yandex.Metrica**, Europe's largest web-analytics service.
- **Problem it solved:** analysts needed *arbitrary* ad-hoc SQL over trillions of page-view events. Arbitrary means you can't pre-aggregate — so the only way is to make brute-force scanning insanely fast.
- **Implementation & why:** MergeTree engine — every insert writes an immutable, sorted, compressed columnar "part" (LSM-style, sequential-only disk writes); background merges; sparse index (one mark per ~8192 rows) instead of per-row indexes; vectorized SIMD execution across all cores; per-column compression codecs. Everything serves one goal: maximize bytes-scanned-per-second while never blocking writes.
- **Write:** millions of rows/s/node — *if batched* (small inserts are the anti-pattern). **Read:** billions of rows/s scan rates; readers never contend with writers (immutable parts). **Weak:** point lookups, row updates, unbatched inserts.

### Apache Druid — "sub-second dashboards on streaming events"
- **Born:** Metamarkets (ad-tech), 2011; Apache top-level later.
- **Problem it solved:** interactive slice-and-dice dashboards over live ad-auction streams — sub-second answers, data visible seconds after the event.
- **Implementation & why:** immutable time-partitioned **segments**; columnar with bitmap inverted indexes (fast arbitrary filtering); **deep storage** (S3/HDFS) holds the truth while "historical" nodes cache segments locally — durability and elasticity come free; optional ingest-time rollup trades raw detail for speed; many specialized node roles. Built for known-shape dashboard queries at high concurrency, not free-form SQL.
- **Write:** high, Kafka-native, exactly-once. **Read:** sub-second aggregations, high QPS. **Weak:** joins/ad-hoc SQL, raw-detail retention (rollup culture), heavy ops (5+ roles, ZooKeeper, metadata DB).

### Apache Pinot — "analytics served to end users"
- **Born:** LinkedIn ~2013–14 ("Who viewed my profile"), Apache top-level later.
- **Problem it solved:** Druid's problem, but pushed to *user-facing* scale: thousands of concurrent queries from end users with p99 latencies in tens of milliseconds.
- **Implementation & why:** segment-based columnar like Druid, plus the richest index toolbox in the class (inverted, sorted, range, JSON, text, and the signature **star-tree index** — configurable pre-aggregation that bounds worst-case latency). Broker scatter-gather fan-out. Everything optimizes templated queries at extreme QPS.
- **Write:** high, Kafka realtime. **Read:** unmatched QPS/latency for known query shapes. **Weak:** ad-hoc SQL (multi-stage engine is newer), ops weight similar to Druid.

### TDengine — "one table per machine"
- **Born:** TAOS Data, Beijing, 2017 (founder Jeff Tao, telecom/IoT background); cluster open-sourced with 3.0 (AGPL).
- **Problem it solved:** industrial/vehicle IoT — millions of devices each emitting *regular, structured* telemetry; and replacing the whole Kafka+Redis+DB pile with one system (built-in cache, stream processing, pub-sub).
- **Implementation & why:** the "super table" model — one physical table per device, sharing a schema template with static tags. Because each device's data arrives roughly in time order, per-device tables make every write a near-sequential append and compression extremely effective; a built-in last-value cache serves the #1 IoT query ("current status") instantly.
- **Write:** very high *for device-shaped data*. **Read:** fast per-device and cross-device rollups. **Weak:** the model *is* the optimization — irregular, high-cardinality event streams (our shape) sit off its happy path.

### Apache IoTDB — "the sensor-native file format"
- **Born:** Tsinghua University research; Apache incubator 2018, top-level 2020.
- **Problem it solved:** factory/edge industrial IoT: high-frequency sensor writes on weak edge hardware, out-of-order arrival, and edge→cloud data shipping.
- **Implementation & why:** **TsFile** — a purpose-built columnar file format for sensor series organized as a device/measurement tree (`root.factory.line1.sensor42`); LSM-style seq/unseq files handle out-of-order data; files written at the edge can be shipped and merged centrally. Native M4-LSM operator for pixel-perfect visualization queries (published research).
- **Write:** very high sample ingest, tolerant of disorder. **Read:** strong per-series aggregation. **Weak:** own SQL dialect, weak joins, device-tree model mismatches generic event data.

### TimescaleDB — "time-series is just a Postgres problem"
- **Born:** Timescale Inc (pivot from IoT startup iobeam), 2017; now TigerData.
- **Problem it solved:** teams that need time-series scale but *refuse to give up* full SQL, joins, transactions, and the Postgres ecosystem.
- **Implementation & why:** a Postgres **extension** — hypertables transparently partition data into time "chunks" so the hot B-tree indexes always fit in RAM (this fixes vanilla Postgres's insert-rate collapse on big tables); columnar compression for old chunks and polished continuous aggregates came later. The bet: developer familiarity beats specialized performance for moderate scale.
- **Write:** ~100Ks rows/s — the row-oriented heap + B-tree/WAL path is an order of magnitude below columnar LSMs. **Read:** excellent for indexed/relational queries; full PG ecosystem. **Weak:** raw scan throughput; multi-node deprecated — the scaling story ends at one big box.

### QuestDB — "open-source kdb+ for tick data"
- **Born:** started ~2014 by Vlad Ilyushchenko (finance background; frustration with kdb+ licensing costs); company formed 2019.
- **Problem it solved:** financial market tick data — brutal ingest rates and time-window queries, with SQL, without kdb+'s price tag.
- **Implementation & why:** the boldest simplification in the list — **data is stored physically sorted by time, always** (the "designated timestamp"), as memory-mapped append-only column files. No LSM merge overhead, no index needed for time queries: the storage *is* the index. Zero-GC Java core, SIMD aggregations; out-of-order arrivals handled by copy-merge commits.
- **Write:** single-node champion class (its ILP ingest benchmarks are the fastest around). **Read:** superb time-window scans, native ASOF JOIN. **Weak:** clustering is Enterprise-only, so both read and write capacity cap at one free node; limited native compression.

### InfluxDB (v1 → 3 Core) — "the purpose-built metrics product" (and a cautionary tale)
- **Born:** InfluxData (Paul Dix), 2013, from the Errplane monitoring product.
- **Problem it solved:** a developer-friendly, purpose-built home for DevOps metrics — trivial ingestion (line protocol), schema-on-write, built-in retention. For years the default answer to "time-series database."
- **Implementation & why:** v1's TSM engine (an LSM variant) organized storage *per series key* (measurement+tags) — brilliant for metrics compression, but every new label combination = a new series, so **high cardinality blows it up**: the famous InfluxDB cardinality wall. v2 detoured into the Flux language (since deprecated). v3 is a *ground-up rewrite* in Rust on Arrow + DataFusion + Parquet over object storage to fix cardinality and cost — but the free Core is positioned as a single-node, recent-data (~72h query window) edge engine; distributed and long-range are paid.
- **Write:** good. **Read:** fast on recent data; long ranges are the paywalled part. **Lesson:** the per-series data model choice in 2013 forced two painful reinventions.

### VictoriaMetrics — "Prometheus storage, but cheap and simple"
- **Born:** ~2018, Aliaksandr Valialkin (ad-tech infrastructure background).
- **Problem it solved:** Prometheus deployments drowning in long-term storage costs and the complexity of Thanos/Cortex scale-out stacks.
- **Implementation & why:** a MergeTree-*inspired* (explicitly ClickHouse-influenced) columnar store specialized for metric samples, single-binary ops, PromQL-compatible MetricsQL, free clustering. Deliberately narrow: samples + labels, nothing else.
- **Write/Read:** excellent — *for metrics*. **Weak:** it is a metrics store, period; per-event high-cardinality data is the one documented workload it rejects; no SQL, no joins; historical downsampling is Enterprise.

### Prometheus — "a monitoring system, not a database"
- **Born:** SoundCloud, 2012 (ex-Google engineers recreating Borgmon); CNCF's second project ever after Kubernetes.
- **Problem it solved:** monitoring and *alerting* on dynamic cloud-native infrastructure — where targets (containers) appear and vanish constantly, and the monitor must keep working when everything else is on fire.
- **Implementation & why:** **pull-based scraping** with service discovery (the monitor finds ephemeral targets; nothing must be configured to push); **deliberately local-only storage** (a monitoring system that depends on a distributed system fails exactly when you need it); TSDB v2 with in-memory head block + immutable 2-hour blocks, Gorilla delta-of-delta/XOR compression; PromQL built for alert expressions.
- **Write:** per-scrape sample appends — perfect for monitoring, irrelevant for event firehoses. **Read:** label-selected series over recent windows. **Weak — by explicit design:** no events, no long retention, no clustering (that's what Thanos/Mimir/VictoriaMetrics bolt on).

### OpenTSDB — "the first scale-out TSDB" (legacy)
- **Born:** StumbleUpon, ~2010 (Benoît Sigoure).
- **Problem it solved:** storing billions of infrastructure metrics *forever at full resolution* in an era when no open-source storage engine could — so it didn't build one.
- **Implementation & why:** a clever schema on top of **HBase** (row key = metric + time bucket + tags), piggybacking HBase's LSM scale-out instead of writing a storage engine. Pure 2010 pragmatism: reuse the Hadoop stack you already run.
- **Write:** scales with the HBase cluster. **Read:** per-series range scans fine; server-side analytics thin. **Today:** effectively dormant; carrying an HBase cluster for metrics is no longer a price anyone wants to pay. Historically important, not a candidate.

### GreptimeDB — "the metrics/logs/traces split was a mistake"
- **Born:** Greptime Inc, April 2022; open-sourced November 2022. The founding team spent ~4 years building a proprietary TSDB at a major tech company, hitting trillions of points/day, runaway storage bills, cardinality pain, and observability data fragmented across three siloed systems.
- **Problem it solved:** "Observability 2.0" — one engine where metrics, logs, and traces are a single wide-event model; compute separated from storage so the bulk of data lives on cheap object storage (S3).
- **Implementation & why:** Rust; LSM engine writing **Parquet files to object storage**; Apache Arrow + DataFusion query stack; SQL and PromQL; Flow engine for continuous aggregation; region-based distribution fully in the open-source build. The design is essentially "ClickHouse-class columnar analytics, rebuilt cloud-native from day one."
- **Write/Read:** designed for exactly our class of problem; the open question is not architecture but **maturity** — four years old versus ClickHouse's seventeen.

---

## 3. Read/write performance summary

| DB | Write throughput class | Read strength | Sweet spot | Falls over when |
|---|---|---|---|---|
| ClickHouse | ★★★★★ (batched) | Massive scans, ad-hoc SQL | Event analytics at any scale | Small inserts, point updates |
| Druid | ★★★★★ (streaming) | Sub-second dashboard aggs | Known-shape dashboards on streams | Ad-hoc SQL, joins, small team ops |
| Pinot | ★★★★★ (streaming) | Extreme QPS, ms latency | User-facing analytics | Ad-hoc exploration, ops weight |
| TDengine | ★★★★★ (device data) | Per-device + rollups | Regular machine telemetry | Irregular high-cardinality events |
| IoTDB | ★★★★★ (sensor data) | Per-series aggregation | Industrial/edge sensors | Generic events, joins |
| QuestDB | ★★★★★ (single node) | Time-window scans, ASOF | Tick data on one box | Need to scale past one free node |
| GreptimeDB | ★★★★ (designed) | Columnar scans, SQL+PromQL | Cloud-native observability | Maturity risk under sustained load |
| VictoriaMetrics | ★★★★ (metrics) | PromQL over series | Prometheus at scale | Anything that isn't metrics |
| InfluxDB 3 Core | ★★★ | Recent-data queries | Edge/last-72h monitoring | Long ranges, clustering (paid) |
| TimescaleDB | ★★★ | Indexed relational + time | SQL-first moderate scale | Raw ingest volume, multi-node |
| Prometheus | ★★ (scrapes) | Alerting queries | Infrastructure monitoring | Events, retention, scale-out |
| OpenTSDB | ★★★ (via HBase) | Series range scans | (historical) | Modern expectations, ops cost |

## 4. Why the implementations differ — the four levers

1. **Storage layout.** Row-oriented heap (TimescaleDB) → cheap single-row ops, expensive scans. Columnar parts/segments (ClickHouse, Druid, Pinot, InfluxDB 3, GreptimeDB) → expensive point ops, phenomenal scans. Per-series chunks (Prometheus, VictoriaMetrics, InfluxDB v1) → perfect for "one metric over time," collapses on high cardinality. Per-device tables (TDengine, IoTDB) → exploits device regularity. Schema-on-KV (OpenTSDB) → borrows someone else's engine.
2. **Write path.** In-place B-tree updates (Postgres family) hit a wall when indexes outgrow RAM. LSM immutable parts (most modern engines) turn every write into a sequential append at the cost of background merges. Time-ordered direct append (QuestDB) removes even the merge cost — as long as data arrives near time order.
3. **Query model.** Full SQL (ClickHouse, Timescale, QuestDB, Greptime) for ad-hoc analysis; PromQL/MetricsQL for alert expressions; restricted aggregation APIs (Druid/Pinot native, OpenTSDB) that trade expressiveness for guaranteed latency.
4. **Distribution model.** Single node + "scale by buying a bigger box" (QuestDB/Timescale free tiers); shared-nothing clusters (ClickHouse, Druid, Pinot, TDengine, VM); compute/storage separation on object storage (GreptimeDB, InfluxDB 3's paid tiers) — the newest generation, betting that S3 economics beat local disks at scale.

## 5. What this means for us (tie-back)

Our data — an irregular firehose of high-cardinality per-IO events needing ad-hoc time-range SQL and event↔metric joins — is *precisely the web-analytics shape*, not the metrics shape and not the IoT shape. That's why the study shortlists from the event-analytics family (ClickHouse) with the cloud-native unifier (GreptimeDB) as challenger, and why metrics stores (Prometheus/VictoriaMetrics/InfluxDB) and device stores (TDengine/IoTDB) — however impressive their headline numbers — are optimized for walls we'll never hit while missing the one we will.

## Sources
- GreptimeDB origins and motivation: [About Greptime](https://greptime.com/about), [Four Years of GreptimeDB retrospective](https://www.greptime.com/blogs/2026-04-21-greptimedb-four-years-retrospective), [GreptimeDB GitHub](https://github.com/GreptimeTeam/greptimedb)
- Licensing/feature verifications: see the sources section of [DB_Study_TimeSeries_Comparison.md](DB_Study_TimeSeries_Comparison.md)
