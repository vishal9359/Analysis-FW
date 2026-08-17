# Analysis Framework — Module 1 (MVP) — High-Level Design

**Date:** 2026-07-21 · **Implements:** `Analysis-FW-Module1-MVP-Requirements.md` · **Go 1.18+, ClickHouse**

> This is the MVP design as agreed, kept as the reasoning record. The **current**
> architecture is [architecture.md](architecture.md); where they differ, that
> document and the [ADRs](decisions/) win. Notably: the implementation is Go
> ([ADR-0009](decisions/0009-go-implementation.md)), the store is a full seam
> ([ADR-0008](decisions/0008-database-seam.md)), and the partition key is
> `run_id` alone (no `sut_id` column exists).

---

## 1. Shape

```
   Discover ──▶ SchemaRegistry ──▶ [ Worker pool ] ──▶ ClickHouse
   (walk tree)  (compile protos)    read→decode→insert   (Store adapter)
```

Four parts, thin boundaries:
- **Discover** — walk the tree, group files into `(stem)` units, pair each `.pb` with its `.proto`.
- **SchemaRegistry** — compile each `.proto` once, derive table + columns.
- **Worker** — one `.pb` file: read → decode → batch → insert into its table. Independent, no shared state.
- **Store** — the only place SQL/ClickHouse lives (keeps "swappable DB" true for ~free).

*Coordinator (reload/drop, cross-worker reconcile, dispatch orchestration) is intentionally out of MVP — added later.*

## 2. Flow

```
1  load bundled config (inside the module); take input path from the CLI arg
2  discover units: each <layer>_<type> stem -> its .proto + ordered .pb parts
3  registry: compile each .proto -> table name (= stem), columns, message class
4  connect; create DB + tables if missing (from derived schema)
5  run units, optionally across a process pool:
       each unit = one .pb file -> read -> decode -> batch -> insert into the stem's table
6  aggregate per-unit counts -> JSON summary -> exit code by outcome
```

No reload, no partition drop, no DB-side reconcile in MVP — those come with the coordinator.

## 3. Parallelism  *(decision to confirm)*

- **Unit of work = one `.pb` file** (a split part, or a whole single file). Handles "one type is huge, split into N" — every part is an independent job.
- **A bounded goroutine pool.** Protobuf decode is CPU-bound; Go has no GIL, so goroutines give real parallel decode in one process — the schema is compiled once and shared, and one pooled connection serves every worker. *(The original Python design needed a process pool for the same parallelism; see [ADR-0009](decisions/0009-go-implementation.md).)*
- **Workers are fully independent** — each inserts its own rows and returns only small counts. No shared state, no ordering constraint (append-only), so no coordination is needed to run them in parallel.
- Pool size is configurable (`store.workers`); concurrent inserts to the same table are fine in ClickHouse.

## 4. Schema → table

- Each `.proto` = one message: contract header (fields 1–5) + payload (field 6, scalar fields). *(Assumption, per format spec §4 — payload nested in field 6; flatten its scalars to columns.)*
- **Table name = file stem** (`block_type1`), so multiple types per layer land in separate tables automatically.
- Columns = fixed header columns + one column per payload scalar (name preserved, protobuf type → ClickHouse type).
- Each `.proto` compiled into its **own descriptor pool** — isolates any package/message-name reuse across types.
- `ENGINE = MergeTree PARTITION BY run_id ORDER BY (run_id, hostname, ts)`.
  *(The `run_id` partition is already what the future coordinator will drop for clean reload — nothing is lost by deferring it.)*

## 5. Integrity (MVP scope)

- `run_id` = directory name; the SUT is identified by the header's `hostname` column.
- `async_insert` off, so post-load counts are truthful.
- Each worker reports rows read vs inserted; the summary aggregates them. A per-worker read≠inserted is a failure.
- Any worker failure → whole load fails (non-zero exit).
- **MVP is append-only: re-running a load duplicates rows.** Clean reload (drop + replace) arrives with the coordinator; the table's partition key is already set up for it.

## 6. Config (editable, at the repo root)

The config file lives at **`config/config.yaml`** (repo root), separate from the code so an operator can edit it in place. It is resolved relative to the source tree, so it loads from any working directory — same for CLI and Airflow. Only the input path is passed per run; `--config` points at an alternate file.

```yaml
# config/config.yaml  (repo root, editable)
producer: { name: profile_fw, database: profile_fw }
store:    { kind: clickhouse, host: localhost, port: 9000,
            batch_size: 100000, workers: 4,
            options: { async_insert: false } }
logging:  { level: INFO, format: json }
# DB creds NOT here — the target uses the default user
```

- **Secrets stay out of the file** — credentials come from env, so the config carries no passwords.
- **`store.host` / `store.port`** may be overridden by env (`CH_HOST` / `CH_PORT`) so the same config works on the dev box and the office server without editing.
- **One config = one producer/database.** Fine for MVP (Profile FW only). When UVP / TraceVision arrive, either deploy the module again with its own config, or add config-selection then — a small, deferred change.

See §8 for what each key means.

## 7. Layout

```
config/
  config.yaml       editable config (repo root; loaded on every run)
cmd/
  analysis-fw/      input-path arg, exit codes, JSON summary, fan-out to the pool
  mkfixture/        simulated-data generator
internal/
  config/           load config + validate; host from env, -config override
  discover/         tree walk, stem grouping, .proto/.pb pairing
  registry/         pure-Go proto compile, table + neutral-column derivation
  reader/           parse a .pb wrapper, yield records
  worker/           one .pb: read -> decode -> batch -> insert
  runner/           run one directory (units in parallel) and batches of runs
  store/            the database seam
    store.go        Store interface + neutral ColumnType
    factory/        the one place an adapter is named
    clickhouse/     DDL (create db/table), batch insert
    memory/         in-memory Store (tests)
docs/               design, block-metrics, timestamp-format
testdata/protos/    bundled sample .proto files
```

No coordinator package. `cmd/analysis-fw` just fans units out to the pool and collects their returned counts — no reload/drop/reconcile logic.

## 8. Invocation & config

**One argument — the input path. The config loads from `config/config.yaml`.**

```
analysis-fw <input_dir>          # same for CLI and Airflow
```

- **`<input_dir>`** — the `ProfileData-*` directory for *this* run. Airflow templates it each run (e.g. from the profiling task's XCom).
- **Config** — `config/config.yaml` at the repo root, loaded on every run (`-config` overrides). The input path is deliberately *not* in it, so the file never changes per run.

**Config keys:**

| Key | Purpose |
|---|---|
| `producer.name` | Label for the producer (Profile FW / UVP / TraceVision), used in logs/summary. |
| `producer.database` | Which ClickHouse database to write into — one per producer, so component ids never collide. |
| `store.kind` | Which store adapter to build (see `internal/store/factory`). |
| `store.host` / `store.port` | ClickHouse **native** endpoint (9000). Overridable by env (`CH_HOST` / `CH_PORT`). |
| `store.batch_size` | Rows per insert — bounds worker memory and tunes insert efficiency. |
| `store.workers` | How many units load concurrently (goroutines). |
| `store.options` | Adapter-specific settings; ClickHouse's `async_insert` must stay `false` so counts are truthful. |
| `logging.level` / `logging.format` | Log verbosity and JSON vs text. |

Not needed: proto location (protos ship in the tree) and any component→table mapping (table = file stem, derived).

## 9. Build order

1. discover + registry (table/column derivation) — no DB, unit-testable.
2. reader + worker decode path — verify against generated fixture data.
3. ClickHouse store — DDL, batch insert.
4. cli fan-out (pool) + config + JSON summary.
5. throughput measurement.

*(All five are done; see [architecture.md](architecture.md) for the built system.)*

## 10. Decisions to confirm in review

1. **Bounded goroutine pool, unit = one `.pb` file** (§3) — vs per-type workers.
2. **Payload is nested in field 6, scalars flattened to columns** (§4) — matches the spec; confirm the real type-protos follow it.
3. **MVP is append-only — no clean reload** (§5) — re-running duplicates; drop/replace deferred to the coordinator. Confirm that's acceptable for now.
```
