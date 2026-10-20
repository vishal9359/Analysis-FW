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
- 30-test suite against an in-memory store; realistic time-series fixture generator.
- `src/` layout, editable `config/config.yaml`, self-contained docs.

Run/verify: see the [README](../README.md). Carries forward into production as the
**replay / backfill / repair** path, not the primary path.

## Now — Phase 1: plain-language request → Grafana graph

**The status board for Phase 1 — keep it current.** What it must do:
[goals-and-requirements.md](goals-and-requirements.md) (Phase 1). How:
[phase1-design.md](phase1-design.md). Work is on branch **`phase1`** (pushed
2026-10-16), not yet merged into `main`.

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
- **Measurement Catalog** for `linux_block_1_stats` (18 columns) and
  `linux_block_2_misc` (196 columns), with the team's answers of 2026-10-12 and
  2026-10-15: [knowledge/catalog/](../knowledge/catalog/), checked by
  `python tools/catalog.py check`. The block_2 proto was fixed (`BiOpFlags`) and gained
  the latency thresholds `lat_option_us_1` … `_16`.

### Built, tested on simulated runs (2026-10-15; on a real ClickHouse 2026-10-16)

- **`analysis/`** — step 0 (find the run's database, compare its columns with the
  catalog), per-run views, per-run WrenAI data models, check C1 on WrenAI's SQL.
  Commands: `python -m analysis check-run | prepare | plan | plan-file` (README,
  "Phase 1").
- **`tests/test_analysis.py`** — 16 tests on a fake database; no ClickHouse.
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
  function. Nothing needed fixing. Not yet run on a real run (step 2 below).

### Next, in order

1. ~~**On a machine with ClickHouse** (WSL2; README, "Phase 1"): load a simulated run
   into a database of its own; run `check-run`, `prepare` and `plan … --execute`.~~
   **Done (2026-10-16)** — see above.
2. **On the office box**, with a real FIO run captured with the new protos: the same
   commands; compare `plan --execute`'s numbers with the existing Grafana panel; run
   `plan-file` under `strace -f -e trace=connect` — no connections expected.
3. **Build step 4, the request runner** ([phase1-design.md](phase1-design.md) §7):
   `request.yaml`; steps 1, 3 and 7 with our LLM (a new `analysis/llm.py`, built like
   `tools/llm_config.py`); step 2's yes/no; step 5 with check C2; step 6, the Grafana
   writer; check C3; a record per request in `out/<run>/`.
4. **Test set and scoring:** 10 block-layer trial requests, answers from the exported
   dashboards (below); then the trial decision: keep WrenAI, switch to Cube Core, or
   plan ourselves.
5. **Then:** catalog the NVMe, syscall and SSD tables (`tools/catalog.py skeleton`);
   grow to the 30-request acceptance set.

### Waiting on the team

- **Catalog Q2** (`linux_block_2_misc`): are bio sizes counted before or after
  splitting? The team is checking.
- **E2:** the existing Grafana dashboards exported as JSON — the answer key.
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
  `sut_id`, `CH_USER`/`CH_PASSWORD` and one job per `.pb` file. The code and the README
  are right.
- On `main`, `ClickHouseStore.count()` is dead code with a `sut_id` column that does not
  exist (removed on the `query` branch).
- Loading a run into a table made from an older proto fails as a database error
  (exit 5), though the cause is the schema. One database per run avoids it.
- `tests/make_fixture.py` fills every number as a growing running total, so in a
  simulated run the settings (`lat_option_us_*`) and gauges (`in_flight_io_count`) grow
  too. Fine for testing the views; not for testing latency bucket labels, or checks on
  gauges (C2).
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
