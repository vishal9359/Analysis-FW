# Analysis Framework — Module 1 (Offline Ingest, MVP)

Reads a completed `ProfileData-<tag>-<timestamp>` directory of protobuf profiling
data and loads it into ClickHouse. Implements `Analysis-FW-Module1-MVP-Design.md`.

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python -m analysis_fw <input_dir>
```

`<input_dir>` is the `ProfileData-*` directory. Config is bundled
(`analysis_fw/config.yaml`) and always loaded — the input path is the only
argument. Airflow invokes it the same way, templating the path per run.

```bash
# example
python -m analysis_fw /data/incoming/ProfileData-fio-4k-randread-20260627-144048
```

Override the ClickHouse endpoint per environment without editing the config:

```bash
CH_HOST=10.0.0.5 CH_PORT=8123 python -m analysis_fw <input_dir>
```

## Input contract

Each layer folder holds one or more **types**, each a `.proto` + its `.pb` data:

```
<layer>_<type>.proto        schema (see message shape below)
<layer>_<type>.pb           data, single file
<layer>_<type>.<NNN>.pb     data, split parts in order 000, 001, …
```

Each `.proto` follows the **generic_format convention**:

```proto
message GenericFormat { string timestamp; string hostname; uint32 component;
                        string tag; uint32 log_level; }         // the header
message <Payload>     { ... layer-specific scalar fields ... }  // the data
message <Record>      { GenericFormat generic_format = 1;       // one row
                        <Payload> <name> = 2; }
message <Wrapper>     { repeated <Record> <name> = 1; }         // a .pb file
```

- A `.pb` file **is one `<Wrapper>` message** holding a `repeated` list of records.
  Protobuf's repeated encoding delimits the records internally — **no framing /
  length prefix**. A big run is split into several `.pb` files, each a complete
  wrapper message.
- The loader finds the record/header/payload/wrapper **by structure** (the record
  is the message with a `GenericFormat` field), not by names — so a renamed
  payload field or a new layer needs no code change.
- **Table = file stem** (`linux_block_1_stats`). Each type → its own table.
- Each row is flattened: `run_id`, `ts` (parsed from `timestamp`), the generic
  fields, the payload fields, `_loaded_at`.
- `Config/` is ignored.

## What it does

`discover → build schema → create tables → load (parallel) → reconcile → JSON report`

- **Parallel:** one process per `.pb` unit (protobuf decode is CPU-bound; the GIL
  makes threads pointless). Configurable pool size.
- **Reconcile:** per unit, `read == inserted`; a mismatch fails the load.
- **Exit codes** tell Airflow the failure class: `0` ok · `2` config · `3` input ·
  `4` schema · `5` database (retryable) · `6` integrity.

## MVP scope

In: discover, runtime schema (generic detection), parallel read, batched insert,
per-unit reconcile, one database per producer.

**Not yet (deferred, see design):** clean reload — **re-running a load appends
(duplicates)**; the table's `PARTITION BY run_id` is already set up for the future
coordinator to drop-and-replace. Also deferred: crash-recovery manifest,
auto-migrate (adding a field to an existing table needs a one-time
`ALTER TABLE ADD COLUMN`), quarantine, streaming.

## Troubleshooting decode errors

If a `.pb` fails to parse, the loader reports it as an input error (exit 3) with
the file, the expected message type, and the first bytes — for example:

```
linux_block_1_stats.pb: could not parse the file as BlockDeviceStatLog (...).
  => the .pb likely does not match its .proto (wrong schema/version), or the
     file is truncated/corrupt. Confirm the .proto with Profile FW.
```

Because the format is now one self-delimited wrapper message per file, a decode
failure means the `.pb` doesn't match its `.proto` (schema/version drift) or the
file is corrupt — not a framing question.

## Test

```bash
python -m pytest tests/ -v
```

Tests run the whole pipeline against an in-memory store, so no database is
needed. `tests/make_fixture.py` generates sample data in the input format.
The real ClickHouse insert is validated on a server (see below).

## First run against a real ClickHouse

```bash
python tests/make_fixture.py /tmp/fix
python -m analysis_fw /tmp/fix/ProfileData-fixture-20260627-144048
# then, in clickhouse-client:
#   SELECT count() FROM profile_fw.block_type1;   -- expect 1000
```

## Layout

```
analysis_fw/
  config.yaml       bundled config (always loaded)
  cli.py            entry point, exit codes, JSON report
  config.py         load + validate; host/port from env
  discover.py       tree walk, stem grouping, .proto/.pb pairing
  registry.py       proto compile, table + column derivation, DDL
  framing.py        varint de-framing
  worker.py         decode one unit -> rows
  runner.py         orchestration: sequential or process pool
  store/
    base.py         Store interface
    clickhouse.py   DDL, batch insert
    memory.py       in-memory store for tests
```
