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

## Done — the MVP (Module 1 offline loader)

**Status: built and in good shape** (this repo). Delivered:

- Runtime schema derivation from `.proto` (no code change for new fields/layers/producers).
- Wrapper + `generic_format`/`payload` format; repeated sub-messages → child tables.
- Batch loading of many runs; fail-fast / `--continue-on-error`; typed exit codes.
- Multi-format timestamp parsing → RFC 3339 UTC; per-run reconciliation.
- Run-level config sidecars (2026-10-12): the latency bucket thresholds in
  `latency_bucket_options.yaml` become a `latency_buckets` table, one row per bucket
  with a legend-ready `range` ([ADR-0011](decisions/0011-run-level-config-sidecars.md)).
- Loader test suite (46 tests) against an in-memory store; realistic time-series
  fixture generator.
- `src/` layout, editable `config/config.yaml`, self-contained docs. The README is
  the user guide; [development.md](development.md) the developer guide (2026-10-14).

Run/verify: see the [README](../README.md). Carries forward into production as the
**replay / backfill / repair** path, not the primary path.

## Now — Phase 1: plain-language request → Grafana graph

**The status board for Phase 1 — keep it current.** What it must do:
[goals-and-requirements.md](goals-and-requirements.md) (Phase 1). How:
[phase1-design.md](phase1-design.md). Work is on branch **`phase1`**, rebased onto
`main` on 2026-10-20 (so it has `main`'s `latency_buckets` loader); not yet merged.

### Done

- **Requirements agreed** (2026-10-09): Goals A, B and C; pass mark 27 of 30 requests.
- **Tools chosen** ([phase1-tool-research.md](phase1-tool-research.md)): WrenAI's
  open-source core for the trial (Cube Core as the fallback); Grafana draws.
- **Only our LLM** (decided 2026-10-10): the company LLM API, or an open-source model
  run in the office. WrenAI contains no AI and sends no usage statistics — checked in
  its source (wrenai 0.15.0, planner wren-core-py 0.8.0 and 0.8.1).
- **Office box ready** (2026-10-10): LLM API with tool calling, ClickHouse, Grafana —
  `tools/check_phase1_env.py`, `tools/check_llm.py`; results in
  [phase1-tool-research.md](phase1-tool-research.md) §7.
- **Design written:** [phase1-design.md](phase1-design.md) — who does what (ours ·
  WrenAI · our LLM), request steps 0–7, per-run views, checks C1–C3, build order.
- **Each run gets its own database** (2026-10-12) while Profile FW's protos keep
  changing; the run's views live in it (2026-10-15). The team's `deploy.py` (outside
  this repo) loads a run with `python -m src <run> --config <temp config>`.
- **Block layer first** (decided 2026-10-20): the whole of Phase 1 — request to
  Grafana graph — is built for the two block tables, the user reviews it, and only then
  are the other layers added.
- **Measurement Catalog** for `linux_block_1_stats` (18 columns) and
  `linux_block_2_misc` (180 columns), with the team's answers of 2026-10-12 and
  2026-10-15: [knowledge/catalog/](../knowledge/catalog/), checked by
  `python tools/catalog.py check`. The block_2 proto was fixed (`BiOpFlags`). Its latency
  bucket thresholds are not in the rows: the profiler writes them to
  `latency_bucket_options.yaml`, and the loader stores them in `latency_buckets`
  ([ADR-0011](decisions/0011-run-level-config-sidecars.md)). Corrected on 2026-10-20:
  the catalog had described them as 16 columns of every row.

### Built, tested on simulated runs and one real run (2026-10-15 to 2026-10-20)

- **`analysis/`** — step 0 (find the run's database, compare its columns with the
  catalog), per-run views, per-run WrenAI data models, check C1 on WrenAI's SQL.
  Commands: `python -m analysis check-run | prepare | plan | plan-file`
  ([development.md](development.md), "Phase 1").
- **`tests/test_analysis.py`** — tests on a fake database; no ClickHouse.
- **What testing WrenAI showed** ([phase1-design.md](phase1-design.md) §6): strict mode
  let a database-qualified name reach a raw table, so C1 checks every table of the
  final SQL; `ref_sql` was dropped (WrenAI rewrote our ClickHouse SQL); WrenAI rewrites
  some functions (`toStartOfMinute` → `dateTrunc('MINUTE', …)`); on ClickHouse 25.6 the
  two give the same result.
- **Run on a real ClickHouse (2026-10-16):** ClickHouse 25.6.13.41 in WSL2 on a dev
  machine, with the simulated run from `tests/make_fixture.py` loaded into a database of
  its own (`profile_fw_sim`, 30,514 rows). `check-run` found it; `prepare` made both views
  and reported "sum of changes = last minus first" for all 16 + 180 counters;
  `plan … --execute` ran WrenAI's SQL, and its IOPS, MiB/s and latency equalled a
  calculation on the raw table on all 999 rows. C1 refused a raw table and a table
  function. Nothing needed fixing.
- **Run on a real run on the office box (2026-10-20):** a direct-I/O random-mix FIO run
  with latency buckets, in a database of its own. `check-run` found it (given the run
  folder's path, so `--db` and path support were added); `block_1`: all 18 columns
  usable; `prepare`: both views, "sum of changes = last minus first" for 16 + 180
  counters; `plan … --execute`: C1 passed and the queries ran, 220 samples. It showed two
  catalog errors, both corrected: the latency thresholds are rows of `latency_buckets`,
  not 16 columns of `block_2` (`block_2` has 180 usable columns, not 196); and
  `block_1`'s `device` holds the namespace, `nvme1n1`, not the controller name.
- **Numbers checked on that run (2026-10-20):** one sample per second for 220 s, no
  gaps; every per-second IOPS, MiB/s and latency in the views equals a calculation on the
  raw table (219 of 219 rows); the run's protos match the catalog exactly. In `block_2`,
  Q1, Q3 and Q15 hold exactly: every bio was 4 KiB, and the size buckets add up to
  `read_bios` and `write_bios`. The latency buckets add up to the bio counts in both
  directions, and the run's 15 thresholds gave 16 rows in `latency_buckets`. `block_1`
  and `block_2` count the same 73,263,082 I/Os and the same bytes; how they split
  between reads and writes differs by 36 (block_1 catalog Q4). `plan-file` under
  `strace` opened no network connection.

### Built 2026-10-21 — the whole request, for the block layer; run on a simulated run only

- **Step 4, the request runner:** `python -m analysis ask <request.yaml>` runs steps 0–7
  and leaves a record per request in `out/<run>/` ([phase1-design.md](phase1-design.md)
  §7.1). `--yes` takes the meaning as confirmed (for scoring), `--no-grafana` stops
  before drawing. New in `analysis/`: `runner.py`; `llm.py`, the only code that calls our
  LLM, built on `tools/llm_config.py`; `steps.py`, the prompts and the checks on each
  answer; `grafana.py`, a folder per run, a dashboard per request, a panel from a
  template. `checks.py` gained C1's time filter, C2 and C3.
- **Latency histograms** ([phase1-design.md](phase1-design.md) §5.2): `latency_buckets`
  is described (`knowledge/catalog/latency_buckets.yaml`, a run-level table: no `ts`, no
  view). `block_2`'s four latency groups say `labels: latency_buckets`, so each run gets
  a histogram view, `linux_block_2_misc_histograms`, with every bucket's range; the 68
  raw bucket columns are no longer offered to our LLM. `prepare` makes the view and
  checks that every I/O is in a labelled bucket; `tools/catalog.py check` checks the new
  catalog keys.
- **Example requests** in `requests/`: the five of the requirements, and a latency
  distribution.
- **The LLM log (2026-10-22):** beside each record, `<same name>.llm.txt` holds every
  request sent to our LLM and its response in full — header names only, never their
  values — so what our LLM is asked and answers can be read directly.
- **The runbook (2026-10-22):** [phase1-runbook.md](phase1-runbook.md) — running Phase 1
  on the office machines, every command, what each does inside, and the errors seen
  setting it up: a Windows machine needs letting in to the server's ClickHouse; a run
  is named by its folder, not its database.
- **Tests:** 96 in all — 46 for the loader, 30 in `tests/test_analysis.py`, 20 in
  `tests/test_runner.py` (a scripted LLM and a fake Grafana); no ClickHouse, LLM or
  Grafana needed.
- **Run end to end on a dev machine:** ClickHouse 25.6 in WSL2 with the simulated run,
  Grafana 13.2.4 with the ClickHouse plugin 4.23.0, and a stand-in for our LLM — scripted
  answers, and also a fake OpenAI-compatible server, so `python -m analysis ask` ran its
  real HTTP path with the custom headers, and no header value reached the record. Drawn,
  checked and looked at in a browser: read and write IOPS over time; data read in the
  last 2 minutes (a single number); the read latency distribution (a bar chart with
  each bucket's range); slow reads over time; one line per bucket over time. A query
  that read the raw table was refused by C1 and corrected on the next try, and asking
  again replaced the same dashboard. Fixed on the way: a dashboard now shows the
  result's own time range, as one table can end before the run does.
- **Not yet run with our LLM.** The prompts of steps 1, 3 and 7 meet the real model only
  on the office box (next, below).

### Next, in order

1. ~~**On a machine with ClickHouse** (WSL2; README, "Phase 1"): load a simulated run
   into a database of its own; run `check-run`, `prepare` and `plan … --execute`.~~
   **Done (2026-10-16)** — see above.
2. ~~**On the office box**, with a real FIO run: the same commands, then check the
   numbers and run `plan-file` under `strace`.~~ **Done (2026-10-20)** — see above.
   Still open: compare the run's totals with FIO's own summary.
3. ~~**Describe `latency_buckets`**, so Phase 1 can label latency histograms.~~
   **Done (2026-10-21)** — see above.
4. ~~**Build step 4, the request runner.**~~ **Built (2026-10-21)** — see above.
5. **On the office box, with our LLM and the real run:** set `grafana.token` in
   `tools/config.json`; run `prepare <run>` again (it now makes the histogram view, and
   on a real run should find no I/O in an unused bucket column — catalog Q16); run
   `python -m analysis ask <file> --run <run>` on each file in `requests/`. The printed
   output and the records in `out/<run>/` show how our LLM reads the prompts: tune them
   from there. Then the user reviews the block layer.
6. **Test set and scoring:** 10 block-layer trial requests, answers from the exported
   dashboards (below); then the trial decision: keep WrenAI, switch to Cube Core, or
   plan ourselves.
7. **After the block-layer review:** catalog the other tables the real run has (`tools/catalog.py skeleton`,
   after copying their `.proto` files into `tests/sample_protos/`): NVMe, syscall and
   SSD, and the newer ones — `linux_block_3_sync`, the filesystem tables
   (`linux_ext4fs_1_iostats`, `_2_sync`, `_3_buffered`, `linux_fs_1_readahead`,
   `linux_fs_2_writepages`) and `linux_platform_1_stats` (CPU, GPU, NIC, RDMA, process).
   With the filesystem tables, requests such as "latency for filesystem and block" become
   answerable. Then grow to the 30-request acceptance set.

### Waiting on the team

- **E2:** the existing Grafana dashboards exported as JSON — the answer key.
- **A Grafana service-account token** (Editor role), in `tools/config.json` as
  `grafana.token`, for the Grafana writer (step 6).
- *Answered on 2026-10-20:* catalog Q2 (sizes and bytes are counted when a bio is
  submitted, before any split) and Q4 (a few I/Os may differ between collectors; accepted).
  Every catalog question is answered. A run with large bios (FIO `bs=4M`) would still
  settle Q17 fully.
- **Optional:** review the draft catalog entries and set `status: confirmed`.
- Real profile data stays on the office box: anything that needs it runs there, and
  only the commands' output comes back.

### Known issues and debts

- Installing `wrenai` raises `urllib3` to ≥ 2.7, which clashes with a `kubernetes`
  Python package if one is installed (seen on the first dev machine). Use a separate
  virtual environment there.
- Profile FW still counts a bio under 4 KiB twice (`read_lt_4kb` and
  `read_lt_4kb_range`); they will remove one. The catalog explains how to avoid it.
- [design.md](design.md) (the MVP loader design) is partly out of date: it mentions
  `sut_id`, `CH_USER`/`CH_PASSWORD` and one job per `.pb` file. The code and
  [development.md](development.md) are right.
- On `main`, `ClickHouseStore.count()` is dead code with a `sut_id` column that does not
  exist (removed on the `query` branch).
- Loading a run into a table made from an older proto fails as a database error
  (exit 5), though the cause is the schema. One database per run avoids it.
- `tests/make_fixture.py` fills every number as a growing running total, so in a
  simulated run the gauges (`in_flight_io_count`) grow too. Fine for testing the views;
  not for testing checks on gauges (C2). It also fills all 17 latency-bucket columns,
  though its file sets 4 thresholds (5 buckets), so `prepare` warns that I/Os are in
  unused columns — expected on a simulated run only. Its tables also differ in length
  (`block_1` 1,000 samples, `block_2` 400).
- The prompts of steps 1, 3 and 7 are not yet tried with our LLM (office box only).
- **Profile FW still writes timestamps as local time + `Z` + offset**
  (`2026-10-15T08:27:30.929Z00:00`, seen 2026-10-20), which is not valid RFC 3339. The
  loader drops what follows `Z` and reads the time as UTC, so `ts` is right only when the
  SUT's clock is set to UTC, as it was for that run. A SUT on IST would be stored
  5 h 30 min off. The fix is on the producer side:
  [timestamp-format.md](timestamp-format.md).
- Branches `query` and `go_version` stopped in August 2026 and lack the
  repeated-inside-repeated support (ADR-0010).

## Later — Phase 2: automated analysis reports

Starts with automated FIO validation. Goals: [goals-and-requirements.md](goals-and-requirements.md)
(Phase 2 and later); draft architecture: [analysis-architecture.md](analysis-architecture.md).
It reuses Phase 1's catalog.

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

### Data-contract questions to settle with Profile FW

Surfaced during the dummy-data work (details:
[reference/context/fake-profile-data-notes.md](reference/context/fake-profile-data-notes.md)):

- **Epoch vs monotonic clock (highest risk).** Real eBPF `bpf_ktime_get_ns()` is
  **boot-relative, not epoch**. If Profile FW ships monotonic ns without a boot-time
  offset, `ts` silently becomes "1970 + uptime" and time queries break silently.
  Settle: are payload `*_ns` fields epoch, or monotonic + a boot offset in run config?
- **Header timestamp is 1-second resolution** — cannot order events within a second,
  yet it's the intended `ORDER BY` key. A `uint64 timestamp_ns` in the header is
  wire-compatible to add later; cheaper to decide now.
- **Record size ~179 B measured** (block 167 / nvme 140 / syscall 171 / filesystem
  211), not the assumed ~80 B — moves the storage/retention math by ~2×. Re-measure
  once the real payloads (spec §3.6–§3.11) exist.
- **`run_config.log` contents** — a `seed` + `config_sha256` would make a run
  reproducible from the log folder alone; `events_emitted`/`events_dropped` feed the
  loss reconciliation. Currently empty by design.
