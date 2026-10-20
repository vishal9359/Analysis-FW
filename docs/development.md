# Development guide — Module 1 (Offline Ingest)

> This is the **developer/internal** guide: architecture, the input contract, the
> derived schema, the fixture generator and the test suite. For the **user-facing**
> install-and-run documentation, see [../README.md](../README.md).

Loads Profile FW protobuf profiling data into ClickHouse. Given a
`ProfileData-<tag>-<timestamp>` run directory, it decodes every record and
writes it to typed ClickHouse tables — completely, correctly, and repeatably.

The schema is derived from the producer's `.proto` at runtime, so new fields,
new layers, and new producers need **no code change**.

> **Project context** — this README is the developer quickstart. For the full
> picture (why the project exists, where it's going, and why it's built this way)
> start at [CLAUDE.md](../CLAUDE.md), then [docs/overview.md](overview.md),
> [docs/architecture.md](architecture.md), [docs/roadmap.md](roadmap.md),
> and the decision records in [docs/decisions/](decisions/). Imported
> requirements, planning context, and DB research are under
> [docs/reference/](reference/).

## Architecture

```
 Profile FW (on each SUT)                    Analysis FW — Module 1 (this repo)
 ────────────────────────                    ──────────────────────────────────
 writes one run directory:                   $ python -m src <run_dir>
   ProfileData-<tag>-<ts>/                              │
     Config/run_config.log                              ▼
     Linux/<layer>/<layer>_<type>.proto     ┌───────────────────────────────────┐
     Linux/<layer>/<layer>_<type>.pb   ───► │ discover → registry → reader →     │
     SSD/ ...                                │ worker → store                     │
                                             └───────────────────────────────────┘
                                                         │
                                                         ▼
                                             ClickHouse — one DB per producer,
                                             one table per <layer>_<type>
```

Pipeline stages (each a module):

```
discover   walk the run dir; pair each <stem>.proto with its <stem>.pb part(s)
registry   compile the .proto at runtime → derive ClickHouse table(s) + columns
reader     parse each .pb (one wrapper message) and iterate its records
worker     flatten a record → row(s): scalars → main table, repeated → child tables
store      create tables if absent; batched INSERT   (ClickHouse; in-memory for tests)
```

| Path | Responsibility |
|---|---|
| `src/cli.py` | entry point; single-vs-batch dispatch; exit codes; JSON report |
| `src/config.py` + `config/config.yaml` | load/validate config; `CH_HOST`/`CH_PORT` env + `--config` overrides |
| `src/discover.py` | walk run dir, group `.proto`/`.pb` by stem, order split parts |
| `src/registry.py` | compile `.proto` → table schema (main + child tables) |
| `src/reader.py` | parse a `.pb` wrapper, yield records |
| `src/worker.py` | flatten records → rows; timestamp parsing |
| `src/runner.py` | run one directory (units in parallel) and batches of runs |
| `src/store/` | ClickHouse adapter + in-memory adapter (tests) |
| `config/`, `docs/`, `tests/` | editable config; docs (design, block-metrics, timestamp); test suite + fixtures |

## Install

```bash
pip install -r requirements.txt      # protobuf, grpcio-tools, PyYAML, clickhouse-connect,
                                     # wrenai (Phase 1)
```

## Usage

**One run** — from the repo root; the input path is the only argument (config
comes from `config/config.yaml`):

```bash
python -m src /data/incoming/ProfileData-fio-4k-randread-20260627-144048
```

**Many runs (batch)** — point at a parent directory of `ProfileData-*` runs; it
loads each sequentially (each run still parallelizes its own units):

```bash
python -m src /data/incoming              # auto-detected as batch
python -m src /data/incoming --batch      # force batch
python -m src /data/incoming --continue-on-error   # attempt every run
```

Default is **fail-fast**: the first failed run stops the batch and the rest are
marked `skipped`. `--continue-on-error` attempts all and collects failures.

Override the ClickHouse endpoint without editing the config:

```bash
CH_HOST=10.0.0.5 CH_PORT=8123 python -m src <dir>
```

**Exit codes** tell the caller the failure class: `0` ok · `2` config ·
`3` input · `4` schema · `5` database (retryable) · `6` integrity. In batch, the
code is the first failing run's class.

**Verify** what landed:

```sql
SELECT count() FROM profile_fw.linux_block_1_stats;
SELECT run_id, count() FROM profile_fw.linux_block_1_stats GROUP BY run_id;
```

## Input contract

Each layer folder holds one or more **types**, each a `.proto` + its `.pb` data:

```
<layer>_<type>.proto        schema
<layer>_<type>.pb           data (single file)
<layer>_<type>.<NNN>.pb     data (split parts, in order 000, 001, …)
```

Every `.proto` follows the agreed shape:

```proto
message GenericFormat { string timestamp; string hostname; uint32 component;
                        string tag; uint32 log_level; }        // the header
message Payload       { ... layer-specific scalar fields ... }    // the data
message StatLog       { GenericFormat generic_format = 1;       // one record
                        Payload payload = 2; }
message StatLogs      { repeated StatLog stat_logs = 1; }       // a .pb file
```

- A `.pb` file **is one `StatLogs` wrapper** holding a `repeated` list of
  records — protobuf's repeated encoding delimits them, so there is **no framing
  / length prefix**. A big run is split into several `.pb` files, each a complete
  wrapper.
- The loader finds record / header / payload / wrapper **by structure** (the
  record is the message with a `generic_format` field and a `payload` field), not
  by message name — so protos may reuse `StatLog`/`StatLogs`/`Payload` (each is
  compiled in its own descriptor pool; identical names never collide).
- `Config/` is ignored. Timestamps: RFC 3339 UTC — see
  [docs/timestamp-format.md](timestamp-format.md).

## What lands in ClickHouse

- **One database per producer** (`producer.database` in config), so component
  ids never collide across producers.
- **One table per `<layer>_<type>`** (the file stem is the table name).
- Each record is flattened to one row: `run_id`, `ts` (parsed from `timestamp`),
  the generic header fields, the payload scalar fields, `_loaded_at`.
- `ENGINE = MergeTree`, `PARTITION BY run_id`, `ORDER BY (run_id, hostname, ts)`.
  `run_id` is the run directory name.

**Payload fields map to tables by shape.** The loader never hardcodes field names —
it decides from each field's cardinality relative to the record:

| Payload field | Cardinality | Becomes |
|---|---|---|
| scalar | 1:1 | a column on the main row |
| singular message (`io_flags`) | 1:1 | flattened onto the main row: `io_flags_direct`, `io_flags_sync`, … |
| `repeated <Message>` (`per_queue`) | 1:N | child table `<stem>_per_queue` |
| repeated inside repeated (`per_core_seq_random[].entries[]`) | 1:N:M | leaf table `<stem>_per_core_seq_random_entries` |
| repeated scalar | — | rejected — the loader errors out |

- One record → **1 main row + N + M child rows** (additive, never N×M).
- A `record_id` (UInt64) links them: `main JOIN child USING (run_id, record_id)`.
- Child tables carry the header + `record_id` + their own fields, ordered by
  their key dimension (`queue_id` / `cpu_id`) for fast filtering.
- Scalar-only payloads (e.g. block) get no `record_id` and no child tables.

**Nested repeated fields get one table, not two.** A repeated message that itself holds
a repeated message is a *grouping level*, not a table. Its scalars are copied onto every
leaf row, so one measurement is one row and no JOIN is needed:

```
Payload
└── per_core_seq_random[]        cpu_id, dir          ← grouping level, no table
    └── entries[]                start_time, …        ← leaf, gets the table

linux_nvme_2_lbarandomness_per_core_seq_random_entries
  run_id  ts  record_id  <header>  cpu_id  dir  start_time  end_time
          accumulated_io_size  accumulated_io_count  is_sequential  _loaded_at
  ORDER BY (run_id, hostname, cpu_id, ts)
```

2 cores × {read, write} with 2/1/3/2 windows → **8 rows**, each carrying its own
`cpu_id` and `dir`. Per-group aggregates come from `GROUP BY cpu_id, dir`.

> ClickHouse does not preserve insertion order, so proto element order is **not**
> recoverable from the table — always `ORDER BY start_time` (add `record_id` to
> disambiguate across records). Producers must emit an ordering key on every leaf
> element. See [ADR-0010](decisions/0010-payload-field-shapes-to-tables.md).

## Run-level config sidecars

Some properties belong to the **run**, not to a record, and the profiler emits them as
a file next to the data rather than inside the `.pb`. The loader reads these once per
run and writes them to their own table, keyed by `run_id`.

**Latency buckets.** The operator chooses the thresholds at profiling time:

```bash
./bin/profile_fw -tag baseline -i 1 -d 1500 -dev nvme1 -lat-buckets 1,10,100,1000
```

and the profiler writes `latency_bucket_options.yaml` into the workload folder:

```yaml
latency_bucket_thresholds_us:
  lat_option_us_1: 1
  lat_option_us_2: 10
  lat_option_us_3: 100
  lat_option_us_4: 1000
```

If that file is present (in the run directory or its `Config/`), the loader creates
`latency_buckets` and writes **one row per bucket** — *n* thresholds give *n+1* rows:

| run_id | option_index | range | threshold_us | _loaded_at |
|---|---|---|---|---|
| ProfileData-… | 0 | `< 1 µs` | 0 | … |
| ProfileData-… | 1 | `1-10 µs` | 1 | … |
| ProfileData-… | 2 | `10-100 µs` | 10 | … |
| ProfileData-… | 3 | `100 µs - 1 ms` | 100 | … |
| ProfileData-… | 4 | `> 1 ms` | 0 | … |

`range` is rendered for a chart legend: a raw threshold like `50000000` says much less
at a glance than `50 s`. Values scale µs → ms → s → min → h, and a bucket whose bounds
share a unit writes it once (`1-10 ms`). The two open-ended buckets carry
`threshold_us = 0` — neither is named by a single threshold — and their bound is
readable in `range`. `option_index` is the bucket position, matching the proto's
positional fields (0 = `latency_lh_opt1`, *n* = `latency_overflow`).

**Why it has to be stored.** The measurements land in the layer table as counts —
`read_device_latency_latency_bw_opt2_and_opt3` means "IOs between the 2nd and 3rd
threshold", which is 10–100 µs *for this run only*. The thresholds are in neither the
`.pb` nor the `.proto`, so without this table the bucket columns can never be labelled,
and two runs profiled with different `-lat-buckets` would silently compare different
ranges. Labelling queries: [docs/block-metrics.md](block-metrics.md).

**n thresholds describe n+1 ranges** — `1,10,100,1000` gives `<1`, `1–10`, `10–100`,
`100–1000`, `>1000`. The table stores the 4 boundaries.

> **Upgrading an existing database:** `latency_buckets` gained the `range` column and
> changed from one row per threshold to one row per bucket. `ensure_table` issues
> `CREATE TABLE IF NOT EXISTS`, which does **not** add a column to a table that already
> exists, so a load against an older `latency_buckets` fails on the missing column. Drop
> it first — `DROP TABLE profile_fw.latency_buckets` — and the next load recreates it.
> Automatic schema migration is deferred debt
> ([ADR-0006](decisions/0006-mvp-append-only-ingest.md)).

`-lat-buckets` is optional, so **no file is normal** and simply produces no table. A
file that *is* present but malformed **fails the load** — storing counts that can never
be interpreted is worse than stopping. Checked: indices contiguous from 1, thresholds
strictly increasing, values whole and non-negative. See
[ADR-0011](decisions/0011-run-level-config-sidecars.md).

## Configuration

Config lives at `config/config.yaml` (repo root), separate from the code so an
operator can edit it in place. It is resolved relative to the source tree, so it
loads from any working directory. Key settings: `producer.database`,
`store.host`/`port` (also env-overridable — see Usage), `store.batch_size`,
`store.workers` (per-run process pool). Point at a different file with
`--config /path/to/config.yaml`. DB credentials are **not** in config (the
target uses the default user).

## Generate simulated data

`tests/make_fixture.py` writes a `ProfileData-*` run of realistic per-second
time-series data (increasing timestamps, monotonic counters), so the pipeline
and the metric queries can be exercised without waiting for real profiling:

```bash
python tests/make_fixture.py /tmp/fix                        # from tests/sample_protos/
python tests/make_fixture.py /tmp/fix --protos <proto_dir>   # from the real protos

python -m src /tmp/fix/ProfileData-fixture-20260727-180221
```

It generates from **any** `.proto` by structure — drop a new one in and it is
picked up, no code change. What it models beyond "fill every field":

- **Grouping levels fan out over their dimensions.** A repeated message with a
  key dimension *and* an enum (`NvmeCoreSeqRandom{cpu_id, dir, entries[]}`) gets
  every combination — 12 cores × {READ, WRITE} = 24 groups, each core appearing
  once per direction, which is what the profiler actually emits.
- **Leaf measurement windows are real intervals.** `start_time`/`end_time` tile
  the record's second per group — ordered, non-overlapping, `end > start` —
  rather than two independent counters. That makes `ORDER BY start_time` return
  the measurement order, the only ordering the database can give back.
- **Counters are phased per group**, so cores differ from one another instead of
  every group reporting identical numbers.
- **Enums cycle their declared values**, so a column like `dir` is never
  `UNSPECIFIED` on every row.

The test suite rebuilds the fixture whenever any `.proto` or `tests/make_fixture.py`
changes (it hashes them into `tests/_fixture/.protos.sha256`), so the fixture cannot
drift from the protos the loader is given.

Deriving per-second IOPS, bandwidth, and latency from the loaded counters — with
ready ClickHouse and Grafana queries — is in
[docs/block-metrics.md](block-metrics.md).

## Scope & limitations (MVP)

- **Append-only.** Re-loading the same run **duplicates** rows. The table's
  `PARTITION BY run_id` is ready for a future coordinator to drop-and-replace.
- **Single machine.** Batch is a bounded per-run loop; fleet fan-out (Airflow /
  the streaming re-architecture) is out of scope.
- **Adding a field to an existing table** needs a one-time `ALTER TABLE ADD
  COLUMN` (auto-migrate is deferred).
- `record_id` and `run_id` are single-node schemes; multi-node-safe versions are
  deferred to the re-architecture.

See [docs/design.md](design.md) for the full design.

## Troubleshooting

A `.pb` that fails to parse is reported as an input error (exit 3) with the file,
the expected message type, and the first bytes — it means the `.pb` does not
match its `.proto` (schema/version drift) or is truncated/corrupt.

If `ts` shows `1970-01-01`, the `timestamp` string couldn't be parsed (the raw
string is still kept in the `timestamp` column). See
[docs/timestamp-format.md](timestamp-format.md) for the expected format.

## Phase 1 — plain-language request to graph (in progress)

Code in `analysis/`; design in [phase1-design.md](phase1-design.md); status and next
steps in [roadmap.md](roadmap.md) ("Now — Phase 1"). Built so far: step 0 (find the
run's database, compare its columns with the catalog), the run's per-interval views,
the run's WrenAI data models, and check C1. `pip install -r requirements.txt` brings
WrenAI (pinned to 0.15.0; it contains no AI).

```bash
python -m analysis check-run <run>         # where the run is, what it has (read-only)
python -m analysis prepare <run>           # makes the run's views, checks they add up
python -m analysis plan <run> "<query>"    # WrenAI plans it, check C1; --execute runs it
python -m analysis plan-file out/<run>/data_models.json "<query>"   # no ClickHouse
```

`<run>` is the run folder's name, or its path: only the last part is used, because the
loader names each run after its folder. Each command searches every database for the
run; `--db <name>` looks in that one only. A query is written on the data models, for
example `SELECT linux_block_1_stats.ts, linux_block_1_stats.read_iops FROM
linux_block_1_stats WHERE linux_block_1_stats.run_id = '<run>'`. Only the ClickHouse
host and port come from `config/config.yaml`; its database name is not used here.

### Trying it on your own machine (ClickHouse in WSL2)

Real profile data stays on the office box, but a simulated run works anywhere:

```bash
# 1. ClickHouse in WSL2, the office box's version. The loader connects as the
#    default user with no password. WSL2 forwards localhost, so Windows sees :8123.
docker run -d --name clickhouse -p 8123:8123 --ulimit nofile=262144:262144 \
    -e CLICKHOUSE_SKIP_USER_SETUP=1 clickhouse/clickhouse-server:25.6

# 2. A simulated run, from tests/sample_protos/
python tests/make_fixture.py /tmp/sim        # -> /tmp/sim/ProfileData-fixture-20260727-180221

# 3. Load it into a database of its own, as deploy.py does on the office box:
#    copy config/config.yaml to sim.yaml and set  producer.database: profile_fw_sim
python -m src /tmp/sim/ProfileData-fixture-20260727-180221 --config sim.yaml

# 4. The Phase 1 steps
python -m analysis check-run ProfileData-fixture-20260727-180221
python -m analysis prepare   ProfileData-fixture-20260727-180221
python -m analysis plan ProfileData-fixture-20260727-180221 "SELECT linux_block_1_stats.ts, linux_block_1_stats.read_iops FROM linux_block_1_stats WHERE linux_block_1_stats.run_id = 'ProfileData-fixture-20260727-180221'" --execute
```

Two traps seen on a dev machine (2026-10-16):

- **WSL must be version 2.** Check with `wsl -l -v`. Under WSL1 every insert fails
  (`filesystem error: in rename: Permission denied`, exit code 5). If the WSL2 kernel is
  missing, run `wsl --update`, then `wsl --set-version Ubuntu-24.04 2`.
- **Not Ubuntu's own `clickhouse-server` package.** `apt install clickhouse-server`
  gives version 18.16 (2018), which has no window functions, so the views cannot be
  made. Use the Docker image above, or ClickHouse's own apt repository with version 25.6
  pinned. Installed with apt, start it with `sudo systemctl enable --now
  clickhouse-server`, and keep an Ubuntu terminal open: WSL stops Ubuntu, and ClickHouse
  with it, when the last one closes.

## Test

```bash
python -m pytest tests/ -v
```

Tests run the whole pipeline against an in-memory store — no database needed.
`tests/make_fixture.py` generates sample runs from `tests/sample_protos/`; pass
`--protos <dir>` to generate from the real protos instead. `tests/test_analysis.py`
covers Phase 1 on a fake database; the tests that need WrenAI are skipped when it is
not installed.
