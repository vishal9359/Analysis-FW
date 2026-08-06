# POC 1 — Analysis Framework: Detailed Activities

**Derived from the four high-level POC 1 activities:**
1. Support database service with schema definition to store data
2. Input data support — offline data files
3. Support for time-series data analysis
4. Support for defined UI screens for system analysis of block layer and SSD

**Duration:** ~4 weeks | **Input mode:** offline data files only (live streaming deferred to POC 2)
**Input sources:** sample files from Profile FW engineer's tool exploration + self-generated files (fio + existing eBPF tools), so we are never blocked.

---

## Activity 1 — Database service with schema definition

*Objective: study and select the best time-series DB for our use case, deploy it as a service, define the schema.*

- **1.1** Write DB selection criteria from our use case: very high event-volume ingest, fast time-range queries, strong compression, free/open-source license, Grafana support, growth path to multi-node for 6–12 SUTs.
- **1.2** Paper study of candidates — ClickHouse, TimescaleDB, QuestDB, InfluxDB 3.x, VictoriaMetrics (plus Parquet+DuckDB as an offline baseline; Prometheus documented as ruled out for event data). Output: comparison matrix, shortlist of 2 for hands-on testing.
- **1.3** Define the benchmark yardstick before testing: 6–8 representative time-range queries (latency percentiles in a window, IOPS timeline, queue depth over time, IO burst inspection, SSD health trend) and target dataset sizes.
- **1.4** Hands-on benchmark of the 2 shortlisted DBs using generated + real captured data: sustained ingest rate, disk bytes per million events (compression), query latency on billion-row data, query speed while loading, retention/expiry support.
- **1.5** Write the **DB decision memo**: chosen DB, measured numbers, how it scales to multi-node.
- **1.6** Deploy the chosen DB as a proper service on the central node: scripted/containerized install, start/stop, config, data directory management.
- **1.7** Define schema v0 with documentation: block-layer IO event table, SSD telemetry table (SMART/log-page samples), run metadata table (run_id, SUT, workload, timestamps), aggregate tables/rollups (per-second IOPS, throughput, latency percentiles).
- **1.8** Set initial retention policy: raw events short-term, aggregates long-term (TTL/tiering).

**Deliverables:** comparison matrix, decision memo, running DB service, documented schema.

## Activity 2 — Input data support: offline data files

*Objective: reliably load completed profiling data files into the DB.*

- **2.1** Set up the data-generation environment (Linux box with NVMe, fio, BCC/bpftrace, nvme-cli) and produce our own sample files; in parallel, collect whatever sample files the Profile FW engineer's exploration produces.
- **2.2** Document every input file type we receive: producing tool, format, fields, typical size and event rate. Output: **supported input formats list v0**.
- **2.3** Define the parsed record format per data type (block-layer IO event, SSD telemetry sample, system stats) and the mapping from each tool's output to the DB schema.
- **2.4** Build the **offline file loader v0** (CLI): parse → validate → batch-insert into DB, tagged with run_id/SUT; bad lines counted and saved aside, not silently dropped; re-loading the same run is safe (skip or replace).
- **2.5** Build a **synthetic file generator**: produces large realistic data files at controllable event rates and durations — used for DB benchmarking (1.4) and volume tests.
- **2.6** Measure loader performance: rows/sec, time to load a 1-hour run's files; document.
- **2.7** Agree the log folder hierarchy convention with the team (run_id/SUT/layer file naming) so loading can be automated later.

**Deliverables:** formats list, loader CLI, file generator, loader performance numbers, folder convention.

## Activity 3 — Time-series data analysis support

*Objective: prove time-based analysis works on the loaded data.*

- **3.1** Implement the query catalog v0 (versioned SQL, the 6–8 queries from 1.3): latency percentiles over a time window, IOPS/throughput timeline, queue depth over time, IO size distribution, sequential-vs-random ratio, first-cut IO burst detection, per-process IO attribution, SSD health trend across a run.
- **3.2** Implement aggregates/rollups in the DB and verify correctness by cross-checking against fio's own end-of-run summary numbers.
- **3.3** Support window/run comparison: run the same query over two time ranges or two run_ids side by side.
- **3.4** Measure query performance on a billion-row dataset (from the generator); document the numbers.
- **3.5** Write a short ad-hoc analysis guide: how an engineer runs their own SQL against the data.

**Deliverables:** query catalog, verified aggregates, comparison capability, performance numbers, how-to guide.

## Activity 4 — UI screens for block layer and SSD analysis

*Objective: view and analyze the data through Grafana using its default capabilities — no custom screen design in POC 1.*

- **4.1** Install Grafana (OSS) on the central node and connect the chosen DB with its official data-source plugin.
- **4.2** Verify block-layer analysis through Grafana's built-in Explore: each catalog query runnable with the standard time-range picker.
- **4.3** Same for SSD data: SMART/telemetry trends over a run's duration.
- **4.4** Save two minimal dashboards using default panels only (saved views of catalog queries): one block-layer, one SSD.
- **4.5** Write a 1-page user guide: open Grafana, pick run and time range, view results.

**Deliverables:** working Grafana connected to DB, two saved dashboards, user guide.

## Activity 5 — Streaming approach study for POC 2 (decision only)

*Objective: choose the approach for continuous (streaming) data collection so POC 2 starts building immediately. Study and decide only — no pipeline is built in POC 1.*
*(Team-added activity, beyond the four high-level items — prepares POC 2.)*

- **5.1** Write the streaming requirements: target event rates and file volumes (real numbers from 2.5/2.6), never-block rule, tolerated lag per data type (aggregates within seconds, raw events within ~a minute), outage tolerance (e.g. 10-minute central-node outage with zero loss), 6–12 SUTs, free/open-source licenses only.
- **5.2** Paper study of candidate approaches against those requirements:
  - file-tailing shipper agent (Vector, Fluent Bit)
  - message broker in the middle (Kafka, NATS JetStream)
  - direct batch inserts from SUT to DB
  - fully custom agent + ingest service
  Output: comparison table with pros/cons and an initial pick.
- **5.3** Time-boxed hands-on trial (2–3 days max) of the initial pick: run it with generator-produced files at target rate into the chosen DB; measure sustained throughput, CPU cost on the SUT, behavior during a forced outage, catch-up time after recovery. Just enough evidence to trust the decision — not a build-out.
- **5.4** Decide the two-lane design as part of the recommendation: small per-second aggregates streamed fast for live dashboards; bulk raw event files trailing behind — so "live view" never depends on moving the full firehose in real time.
- **5.5** Write the **streaming approach decision memo**: recommended approach, measured evidence, rejected options and why. This memo is the first input to POC 2 planning.

**Deliverables:** requirements note, comparison table, trial measurements, decision memo.

---

## Week-by-week plan

| Week | Activities |
|------|-----------|
| 1 | 1.1–1.3 (criteria, paper study, yardstick) · 2.1–2.2 (environment, sample files, formats list) · 2.5 (generator, first version) |
| 2 | 1.4–1.5 (benchmark, decision memo) · 2.3–2.4 (record mapping, loader v0) |
| 3 | 1.6–1.8 (DB service, schema, retention) · 2.6–2.7 · 3.1–3.3 (queries, aggregates, comparison) · 5.1–5.2 (streaming requirements + paper study) |
| 4 | 3.4–3.5 (scale test, guide) · 4.1–4.5 (Grafana) · 5.3–5.5 (streaming trial + decision memo) · findings + demo |

## Success criteria (demo on last day)

1. Take offline data files from a real fio profiling run (block layer + SSD logs), load them with one loader command, and the data appears in the DB.
2. Run the time-range query catalog on that run; results in seconds even on billion-row test data.
3. Open Grafana, pick the run's time range, and analyze block-layer and SSD behavior on the two saved dashboards.
4. DB decision memo with measured evidence is written and reviewed.
5. Loader and query performance numbers documented.
6. Streaming approach decision memo (Activity 5) written and reviewed — POC 2 can start building on day one.

## Deferred to POC 2 (consciously out of scope)

- Live/streaming ingestion **implementation** (the approach is *decided* in POC 1 Activity 5; the build happens in POC 2)
- Custom-designed UI screens
- Multi-node DB deployment (POC 1 delivers the growth plan on paper)
- Integration with Use-case Run & Deploy FW automation

## Dependencies

- Sample data files from Profile FW engineer — helpful but not blocking (we self-generate in 2.1/2.5).
- One Linux machine with a local NVMe SSD (weeks 1–3) and DGX Spark access if available in week 4 (nice-to-have for POC 1, required in POC 2).
