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
<layer>_<type>.proto        schema — one message: contract header + payload (scalars)
<layer>_<type>.pb           data, single file
<layer>_<type>.<NNN>.pb     data, split parts in order 000, 001, …
```

- **Table = file stem** (`block_type1`). Each type lands in its own table.
- The schema is read from the `.proto` at runtime — new fields / types / producers
  need no code change.
- Framing is varint length-delimited. `Config/` is ignored.

## What it does

`discover → build schema → create tables → load (parallel) → reconcile → JSON report`

- **Parallel:** one process per `.pb` unit (protobuf decode is CPU-bound; the GIL
  makes threads pointless). Configurable pool size.
- **Reconcile:** per unit, `read == inserted`; a mismatch fails the load.
- **Exit codes** tell Airflow the failure class: `0` ok · `2` config · `3` input ·
  `4` schema · `5` database (retryable) · `6` integrity.

## MVP scope

In: discover, runtime schema, parallel read/decode, batched insert, per-unit
reconcile, one database per producer.

**Not yet (deferred, see design):** clean reload — **re-running a load appends
(duplicates)**; the table's `PARTITION BY (run_id, sut_id)` is already set up for
the future coordinator to drop-and-replace. Also deferred: crash-recovery
manifest, auto-migrate, unknown-field detection, quarantine, streaming.

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
