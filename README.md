# Analysis Framework — Module 1 (Offline Ingest)

Loads Profile FW protobuf profiling data into ClickHouse. Given a
`ProfileData-<tag>-<timestamp>` run directory, it decodes every record and
writes it to typed ClickHouse tables — completely, correctly, and repeatably.

The schema is derived from the producer's `.proto` at runtime, so new fields,
new layers, and new producers need **no code change**.

> **Project context** — this README is the developer quickstart. For the full
> picture (why the project exists, where it's going, and why it's built this way)
> start at [CLAUDE.md](CLAUDE.md), then [docs/overview.md](docs/overview.md),
> [docs/architecture.md](docs/architecture.md), [docs/roadmap.md](docs/roadmap.md),
> and the decision records in [docs/decisions/](docs/decisions/). Imported
> requirements, planning context, and DB research are under
> [docs/reference/](docs/reference/).

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
pip install -r requirements.txt      # protobuf, grpcio-tools, PyYAML, clickhouse-connect
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
  [docs/timestamp-format.md](docs/timestamp-format.md).

## What lands in ClickHouse

- **One database per producer** (`producer.database` in config), so component
  ids never collide across producers.
- **One table per `<layer>_<type>`** (the file stem is the table name).
- Each record is flattened to one row: `run_id`, `ts` (parsed from `timestamp`),
  the generic header fields, the payload scalar fields, `_loaded_at`.
- `ENGINE = MergeTree`, `PARTITION BY run_id`, `ORDER BY (run_id, hostname, ts)`.
  `run_id` is the run directory name.

**Repeated sub-messages → child tables.** If a `Payload` has `repeated <Message>`
fields (e.g. NVMe `per_queue`, `per_core`), each becomes its own child table
`<stem>_<field>`:

- One record → **1 main row + N + M child rows** (additive, never N×M).
- A `record_id` (UInt64) links them: `main JOIN child USING (run_id, record_id)`.
- Child tables carry the header + `record_id` + their own fields, ordered by
  their key dimension (`queue_id` / `cpu_id`) for fast filtering.
- Scalar-only payloads (e.g. block) get no `record_id` and no child tables.

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

Deriving per-second IOPS, bandwidth, and latency from the loaded counters — with
ready ClickHouse and Grafana queries — is in
[docs/block-metrics.md](docs/block-metrics.md).

## Scope & limitations (MVP)

- **Append-only.** Re-loading the same run **duplicates** rows. The table's
  `PARTITION BY run_id` is ready for a future coordinator to drop-and-replace.
- **Single machine.** Batch is a bounded per-run loop; fleet fan-out (Airflow /
  the streaming re-architecture) is out of scope.
- **Adding a field to an existing table** needs a one-time `ALTER TABLE ADD
  COLUMN` (auto-migrate is deferred).
- `record_id` and `run_id` are single-node schemes; multi-node-safe versions are
  deferred to the re-architecture.

See [docs/design.md](docs/design.md) for the full design.

## Troubleshooting

A `.pb` that fails to parse is reported as an input error (exit 3) with the file,
the expected message type, and the first bytes — it means the `.pb` does not
match its `.proto` (schema/version drift) or is truncated/corrupt.

If `ts` shows `1970-01-01`, the `timestamp` string couldn't be parsed (the raw
string is still kept in the `timestamp` column). See
[docs/timestamp-format.md](docs/timestamp-format.md) for the expected format.

## Test

```bash
python -m pytest tests/ -v
```

Tests run the whole pipeline against an in-memory store — no database needed.
`tests/make_fixture.py` generates sample runs from `tests/sample_protos/`; pass
`--protos <dir>` to generate from the real protos instead.
