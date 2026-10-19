# Analysis Framework — Profiling Data Loader

Load Profile FW profiling data into ClickHouse, so you can query and chart it.

Point the loader at a profiling run, and it creates the database, creates the tables
and inserts the rows. You don't define any schema: the tables are built from the
`.proto` files that ship inside the run, so when the profiler adds a field or a new
layer, the loader picks it up with no change and no configuration.

**Version 0.1.0.**

---

## Contents

- [How it fits together](#how-it-fits-together)
- [Prerequisites](#prerequisites)
- [Install](#install)
- [Configure](#configure)
- [Prepare your data](#prepare-your-data)
- [Run the loader](#run-the-loader)
- [What you get](#what-you-get)
- [Latency buckets](#latency-buckets)
- [Checking that a load worked](#checking-that-a-load-worked)
- [Troubleshooting](#troubleshooting)
- [Known limits in this release](#known-limits-in-this-release)

---

## How it fits together

```text
                        ┌───────────────────────────────────────────────────┐
   .pb files            │ Analysis FW                                       │
   (a ProfileData-*     │                                                   │
    run directory)      │  ┌────────┐     ┌─────────┐     ┌───────┐         │
     ──────────────────►│  │ loader │ ──► │ workers │ ──► │ store │         │
                        │  └────────┘     └─────────┘     └───┬───┘         │
                        │                                     │             │
                        │                                     ▼             │
                        │                            ,─────────────────.    │
                        │                           (     ClickHouse    )   │
                        │                            `─────────────────'    │
                        │                                     ▲             │
                        │                                     │ SQL         │
                        │  ┌ ─ ─ ─ ─ ─ ┐          ┌───────────┴────────┐    │
                        │     deploy *   ────────►│   Grafana server   │◄────── Grafana UI
                        │                         │                    │──────►
                        │  └ ─ ─ ─ ─ ─ ┘          └────────────────────┘    │
                        └───────────────────────────────────────────────────┘
```

The paired arrows on the right are one conversation: the browser asks, the Grafana
server answers.

`* deploy` installs the Grafana dashboards. It ships separately and is **not** part
of this repository.

| | |
|---|---|
| **loader** | reads a run directory and works out the tables from the `.proto` files in it |
| **workers** | decode the `.pb` data files, several at a time (`store.workers`) |
| **store** | creates any missing tables and inserts the rows in batches |
| **ClickHouse** | where your data lands, one database per producer |
| **Grafana server** | queries ClickHouse with SQL to draw the dashboards |

You run the **loader**. Everything from ClickHouse rightwards is how you read the
results back.

---

## Prerequisites

| | Requirement |
|---|---|
| **Python** | 3.10 or newer (tested on 3.12) |
| **ClickHouse** | A running server reachable over its **HTTP port** (default `8123`). Tested against ClickHouse 25.6. |
| **Data** | One or more `ProfileData-*` run directories produced by Profile FW |
| **Disk** | Only for the input data — the loader streams it and keeps nothing |

**ClickHouse must accept the default user with no password.** This release does not
send credentials, so a server that requires a username and password will not work.
See [Known limits](#known-limits-in-this-release).

You do **not** need to create the database or any tables by hand. The loader creates
whatever is missing.

### Quick check that ClickHouse is reachable

```bash
curl http://localhost:8123/ping          # expect: Ok.
```

---

## Install

The loader runs from its source directory — there is nothing to install into your
Python environment besides the dependencies.

```bash
git clone https://github.com/vishal9359/Analysis-FW.git
cd Analysis-FW

python -m venv .venv
source .venv/bin/activate                # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### Dependencies

Installed by the command above:

| Package | Why it's needed |
|---|---|
| `protobuf` | reads the profiling data |
| `grpcio-tools` | bundles `protoc`, used to compile the run's `.proto` files |
| `PyYAML` | reads the config file |
| `clickhouse-connect` | talks to ClickHouse |
| `pytest` | only for the self-check below |

### Verify the install

```bash
python -m src --version                  # prints the version
python -m pytest tests/ -q               # end-to-end self-check
```

The self-check runs the whole pipeline against an in-memory store, so it needs **no
database**. If it passes, the installation is sound and any remaining problem is
configuration, connectivity or input data.

> Run every command **from the `Analysis-FW` directory** (`python -m src` needs it).

---

## Configure

Settings live in **`config/config.yaml`**, next to the code, so you can edit it in
place. The defaults work against a local ClickHouse.

```yaml
producer:
  name: profile_fw          # label shown in logs and the summary
  database: profile_fw      # ClickHouse database to write into

store:
  host: localhost           # ClickHouse host
  port: 8123                # ClickHouse HTTP port
  batch_size: 100000        # rows per insert
  workers: 4                # how many data files load at once
  async_insert: false       # leave false, or reported row counts become unreliable

logging:
  level: INFO               # DEBUG | INFO | WARNING | ERROR
  format: json              # json | text
```

| Setting | What to change it for |
|---|---|
| `producer.database` | Write a different producer's data into its own database |
| `store.host` / `store.port` | Point at a remote ClickHouse |
| `store.batch_size` | Lower it if inserts are rejected as too large; raise it for throughput |
| `store.workers` | Match your CPU count. `1` loads serially, which gives the clearest logs when diagnosing a problem |
| `logging.format` | `text` is easier to read by eye; `json` suits log collectors |

### Overriding without editing the file

```bash
CH_HOST=db-host CH_PORT=8123 python -m src /data/ProfileData-baseline-20261012-1230

python -m src /data/ProfileData-baseline-20261012-1230 --config /etc/analysis-fw.yaml
```

`CH_HOST` and `CH_PORT` take precedence over the file. `--config` replaces the file
entirely.

---

## Prepare your data

A **run** is one `ProfileData-<tag>-<timestamp>` directory, exactly as Profile FW
writes it. Keep it intact — the loader needs the `.proto` files that sit beside the
data.

```
ProfileData-baseline-20261012-1230/
├── Config/
│   ├── run_config.log
│   └── …
├── latency_bucket_options.yaml          (only if -lat-buckets was used)
├── Linux/
│   ├── Block/
│   │   ├── linux_block_1_stats.proto    schema
│   │   ├── linux_block_1_stats.000.pb   data, part 1
│   │   ├── linux_block_1_stats.001.pb   data, part 2
│   │   ├── linux_block_2_misc.proto
│   │   └── linux_block_2_misc.pb        data, single file
│   ├── NVMe/
│   └── Syscall/
└── SSD/
```

Rules the loader relies on:

- Every data file has a **matching `.proto` in the same folder**. `foo.pb` needs
  `foo.proto`.
- A large data set is split into numbered parts — `foo.000.pb`, `foo.001.pb` — and
  they must be **complete and consecutive**. A missing part is reported rather than
  silently skipped.
- Folder names below the run directory are free: the loader searches the whole tree.
- **`Config/` is ignored.**

The run directory name becomes the `run_id` on every row, so keep it meaningful — it
is how you select one profiling session later.

---

## Run the loader

### One run

```bash
python -m src /data/ProfileData-baseline-20261012-1230
```

### Several runs

Point at the **parent** directory and every `ProfileData-*` inside it is loaded, one
after another. This is detected automatically:

```bash
python -m src /data
```

By default the batch **stops at the first run that fails**. To load everything and
collect the failures instead:

```bash
python -m src /data --continue-on-error
```

### All options

| Option | Effect |
|---|---|
| *(none)* | Load the given run, or every `ProfileData-*` under it |
| `--batch` | Force batch mode (normally detected) |
| `--continue-on-error` | Batch: keep going after a run fails |
| `--config PATH` | Use a different config file |
| `--version` | Print the version |
| `-h`, `--help` | Show usage |

A machine-readable summary is printed to **stdout**; progress and errors go to
**stderr**. So this keeps the summary clean:

```bash
python -m src /data/ProfileData-baseline-20261012-1230 > summary.json
```

---

## What you get

One **database** (`producer.database`, default `profile_fw`) containing one **table per
data file**, named after the file:

```
linux_block_1_stats.pb   →  table  linux_block_1_stats
linux_block_2_misc.pb    →  table  linux_block_2_misc
```

Every table carries the same leading and trailing columns:

| Column | Meaning |
|---|---|
| `run_id` | the run directory name — **use this to select one profiling session** |
| `ts` | the record's timestamp, as a ClickHouse `DateTime` (UTC) |
| `timestamp` | the original timestamp string, exactly as profiled |
| `hostname`, `component`, `tag`, `log_level` | the record header |
| …the profiled fields… | one column per field, named as in the `.proto` |
| `_loaded_at` | when this row was inserted |

Tables are `MergeTree`, partitioned by `run_id` and sorted by
`(run_id, hostname, ts)`.

### Per-queue, per-core and similar tables

Where the profiler reports a list per record — per queue, per CPU core, per latency
window — those rows go into a **companion table** named after the main table and the
field:

```
linux_nvme_1_stats                                   one row per record
linux_nvme_1_stats_per_queue                         one row per queue, per record
linux_nvme_1_stats_per_core                          one row per core, per record
linux_nvme_2_lbarandomness_per_core_seq_random_entries
```

A `record_id` column links them back to the main row:

```sql
SELECT *
FROM linux_nvme_1_stats       AS m
JOIN linux_nvme_1_stats_per_queue AS q USING (run_id, record_id)
WHERE m.run_id = 'ProfileData-baseline-20261012-1230';
```

Tables with no such lists have no `record_id` — they don't need one.

### Starter queries

```sql
-- which tables were created
SHOW TABLES FROM profile_fw;

-- which runs have been loaded
SELECT run_id, min(ts) AS started, max(ts) AS ended, count() AS rows
FROM profile_fw.linux_block_1_stats
GROUP BY run_id ORDER BY started DESC;

-- one run's data, in time order
SELECT * FROM profile_fw.linux_block_1_stats
WHERE run_id = 'ProfileData-baseline-20261012-1230'
ORDER BY ts
LIMIT 20;
```

Ready-made IOPS, bandwidth and latency queries — including Grafana-friendly versions
— are in [docs/block-metrics.md](docs/block-metrics.md).

---

## Latency buckets

If the profiler was run with `-lat-buckets`, it records how many IOs fell into each
latency range:

```bash
./bin/profile_fw -tag baseline -i 1 -d 1500 -dev nvme1 -lat-buckets 1,10,100,1000
```

The **counts** land in the layer's own table, but those columns only say *which*
bucket — not what range it covers, because you choose that per run. So the ranges are
written to a **`latency_buckets`** table, one row per bucket:

| run_id | option_index | range | threshold_us |
|---|---|---|---|
| ProfileData-… | 0 | `< 1 µs` | 0 |
| ProfileData-… | 1 | `1-10 µs` | 1 |
| ProfileData-… | 2 | `10-100 µs` | 10 |
| ProfileData-… | 3 | `100 µs - 1 ms` | 100 |
| ProfileData-… | 4 | `> 1 ms` | 0 |

- **You can pass any number of thresholds.** *n* thresholds describe *n+1* ranges —
  the two open-ended ones at each end included.
- **`range` is ready to use as a chart label.** Values are shown in the largest
  sensible unit, so you get `50 s` rather than `50000000`.
- `option_index` is the bucket's position, `0` being the fastest.
- `threshold_us` is the threshold that opens the bucket, and `0` on the first and last
  rows, which have no threshold on their open side.

This table only appears if the run contains `latency_bucket_options.yaml` —
`-lat-buckets` is optional, and a run profiled without it simply has no such table.

How to join the ranges to the counts: [docs/block-metrics.md](docs/block-metrics.md).

---

## Checking that a load worked

The loader prints a summary and **verifies its own work**: for every table it compares
the records it read against the rows it inserted. A mismatch fails the run.

```json
{
  "status": "complete",
  "run_id": "ProfileData-baseline-20261012-1230",
  "producer": "profile_fw",
  "database": "profile_fw",
  "totals": { "records": 2500, "rows": 30514, "units": 7, "latency_buckets": 5 },
  "units": [
    { "stem": "linux_block_1_stats", "files": 2, "records": 1000,
      "tables": { "linux_block_1_stats": 1000 }, "ok": true }
  ]
}
```

| Field | Meaning |
|---|---|
| `status` | `complete` or `failed` |
| `totals.records` | profiling records read |
| `totals.rows` | rows inserted |
| `totals.units` | data sets loaded (one per `.proto`) |
| `totals.latency_buckets` | latency range rows written, `0` if the run had none |
| `units[].ok` | `false` means that data set did not reconcile |

`rows` is normally larger than `records`, because per-queue and per-core lists add
rows of their own.

### Exit codes

Useful for scripts and schedulers — the code tells you whether retrying can help.

| Code | Meaning | Retry? |
|---|---|---|
| `0` | Success | — |
| `2` | Bad config | No — fix `config.yaml` |
| `3` | Input problem: missing path, unreadable or corrupt data | No — fix the data |
| `4` | A `.proto` would not compile, or doesn't match the expected shape | No — fix the schema |
| `5` | ClickHouse connect, insert or table-creation failure | **Often yes** — usually transient |
| `6` | Records read did not match rows inserted | No — investigate |
| `1` | Unexpected error | Report it |

---

## Troubleshooting

**`config error: config: config file not found at …`**
You are not in the `Analysis-FW` directory, or `config/config.yaml` was moved. `cd`
into the repo, or pass `--config PATH`.

**`cannot connect to ClickHouse at <host>:<port>` (exit 5)**
Check the server is up and the **HTTP** port is right (`8123`, not the native `9000`):
`curl http://<host>:8123/ping`. If your server requires a username and password, this
release cannot connect to it. The driver retries first, so expect a couple of
`Retrying…` / `Unexpected Http Driver Exception` warnings above the real error — the
last line is the one to read.

**`no .pb data files found under …` (exit 3)**
The path isn't a run directory and holds no `ProfileData-*` runs. Pass the
`ProfileData-*` directory itself, or its parent for batch mode.

**`unexpected .pb name (not <stem>[.NNN].pb) …` (exit 3)**
A data file has been renamed. Parts must be `<name>.pb` or `<name>.000.pb`.

**`<name>: found data files but no schema '<name>.proto' beside them` (exit 3)**
A data file has no schema next to it. Copy the whole run directory, don't cherry-pick
files.

**`<name>: split parts are not contiguous 000..00N (got […])` (exit 3)**
A numbered part is missing (`…000.pb`, `…002.pb`, but no `…001.pb`). The transfer was
incomplete — recopy the run.

**`protoc failed to compile` (exit 4)**
The run's `.proto` has an error in it, such as a message name that doesn't match where
it's used. The message names the file and line; it needs fixing on the profiler side.

**`<file>.pb: could not parse the file as …` (exit 3)**
The data doesn't match its `.proto` — usually the two came from different profiler
versions — or the file is truncated. The message includes the file size and its first
bytes, which is normally enough to tell a truncated file from a schema mismatch.

**`ts` shows `1970-01-01`**
The timestamp string could not be parsed. The original is always preserved in the
`timestamp` column, so nothing is lost. See
[docs/timestamp-format.md](docs/timestamp-format.md).

**`insert into latency_buckets failed: … range …` (exit 5)**
You have a `latency_buckets` table from an earlier version, which has no `range`
column. Existing tables are never altered automatically, so drop it and load again —
it is rebuilt:

```sql
DROP TABLE profile_fw.latency_buckets;
```

**Records read but no rows inserted (exit 6)**
Don't query the data. Check the log for the failing table and report it with the
summary JSON.

---

## Known limits in this release

- **Loading a run twice duplicates its rows.** There is no reload or de-duplication
  step yet. To reload a run, drop its partition first:
  ```sql
  ALTER TABLE profile_fw.linux_block_1_stats DROP PARTITION 'ProfileData-…';
  ```
  Do this for each affected table.
- **No authentication.** ClickHouse must allow the default user with no password.
- **New fields need a manual column.** If the profiler adds a field to a `.proto` you
  have already loaded, the existing table won't gain the column by itself — add it
  with `ALTER TABLE … ADD COLUMN`, or drop the table and reload.
- **One machine at a time.** Batch mode is a loop over runs on the host you run it on;
  there is no cluster-wide coordination.
- **Offline only.** Data is loaded from completed runs. Live streaming is planned.

---

## More

| | |
|---|---|
| IOPS / bandwidth / latency queries | [docs/block-metrics.md](docs/block-metrics.md) |
| Timestamp formats | [docs/timestamp-format.md](docs/timestamp-format.md) |
| Terminology | [docs/glossary.md](docs/glossary.md) |
| Contributing / internals | [docs/development.md](docs/development.md) |

Issues: https://github.com/vishal9359/Analysis-FW/issues
