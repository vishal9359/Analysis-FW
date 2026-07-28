# Analysis Framework — Module 1 (MVP) — High-Level Design

**Date:** 2026-07-21 · **Implements:** `Analysis-FW-Module1-MVP-Requirements.md` · **Python 3.12+, ClickHouse**

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
- **Process pool, not threads.** Protobuf decode is CPU-bound; Python's GIL makes threads serialize it. Processes give real parallel decode. *(Same CPU-bound reason behind the "Go later" plan.)*
- **Workers are fully independent** — each opens its own ClickHouse connection, inserts its own rows, returns only small counts. No shared state, no ordering constraint (append-only), so no coordination is needed to run them in parallel.
- Pool size is configurable; concurrent inserts to the same table are fine in ClickHouse.

## 4. Schema → table

- Each `.proto` = one message: contract header (fields 1–5) + payload (field 6, scalar fields). *(Assumption, per format spec §4 — payload nested in field 6; flatten its scalars to columns.)*
- **Table name = file stem** (`block_type1`), so multiple types per layer land in separate tables automatically.
- Columns = fixed header columns + one column per payload scalar (name preserved, protobuf type → ClickHouse type).
- Each `.proto` compiled into its **own descriptor pool** — isolates any package/message-name reuse across types.
- `ENGINE = MergeTree PARTITION BY (run_id, sut_id) ORDER BY (run_id, sut_id, ts)`.
  *(The `(run_id, sut_id)` partition is already what the future coordinator will drop for clean reload — nothing is lost by deferring it.)*

## 5. Integrity (MVP scope)

- `run_id` = directory name; `sut_id` = header hostname; both stored as columns.
- `async_insert` off, so post-load counts are truthful.
- Each worker reports rows read vs inserted; the summary aggregates them. A per-worker read≠inserted is a failure.
- Any worker failure → whole load fails (non-zero exit).
- **MVP is append-only: re-running a load duplicates rows.** Clean reload (drop + replace) arrives with the coordinator; the table's partition key is already set up for it.

## 6. Config (bundled with the module)

The config file ships **inside the module** and is always loaded — same for CLI and Airflow. Only the input path is passed per run.

```yaml
# analysis_fw/config.yaml  (bundled, always loaded)
producer: { name: profile_fw, database: profile_fw }
store:    { host: localhost, port: 8123,
            batch_size: 100000, workers: 6, async_insert: false }
logging:  { level: INFO, format: json }
# DB creds NOT here — from env (CH_USER / CH_PASSWORD)
```

- **Secrets stay out of the file** — credentials come from env, so the bundled config carries no passwords.
- **`store.host` / `store.port`** may be overridden by env (`CH_HOST` / `CH_PORT`) so the same bundled config works on the dev box and the office server without editing.
- **One bundled config = one producer/database.** Fine for MVP (Profile FW only). When UVP / TraceVision arrive, either deploy the module again with its own config, or add config-selection then — a small, deferred change.

See §8 for what each key means.

## 7. Layout

```
analysis_fw/
  config.yaml       bundled default config (always loaded)
  cli.py            input-path arg, exit codes, JSON summary, fan-out to the pool
  config.py         load bundled config + validate; creds/host from env
  discover.py       tree walk, stem grouping, .proto/.pb pairing
  registry.py       proto compile, table + column derivation
  framing.py        varint de-framing
  worker.py         one .pb: read -> decode -> batch -> insert
  store/
    base.py         Store interface
    clickhouse.py   DDL (create db/table), batch insert, count
tests/
```

No `coordinator.py`. `cli.py` just fans units out to the pool and collects their returned counts — no reload/drop/reconcile logic.

## 8. Invocation & config

**One argument — the input path. The config is bundled and always loaded.**

```
analysis_fw <input_dir>          # same for CLI and Airflow
```

- **`<input_dir>`** — the `ProfileData-*` directory for *this* run. Airflow templates it each run (e.g. from the profiling task's XCom).
- **Config** — `analysis_fw/config.yaml` inside the module, loaded on every run. The input path is deliberately *not* in it, so the file never changes per run.

**Config keys:**

| Key | Purpose |
|---|---|
| `producer.name` | Label for the producer (Profile FW / UVP / TraceVision), used in logs/summary. |
| `producer.database` | Which ClickHouse database to write into — one per producer, so component ids never collide. |
| `store.host` / `store.port` | ClickHouse HTTP endpoint. Overridable by env (`CH_HOST` / `CH_PORT`). |
| `store.batch_size` | Rows per insert — bounds worker memory and tunes insert efficiency. |
| `store.workers` | Process-pool size (how many `.pb` files load at once). |
| `store.async_insert` | Must be `false` so counts are truthful. |
| `logging.level` / `logging.format` | Log verbosity and JSON vs text. |
| `CH_USER` / `CH_PASSWORD` *(env, not in file)* | DB credentials — kept out of the config file. |

Not needed: proto location (protos ship in the tree) and any component→table mapping (table = file stem, derived).

## 9. Build order

1. discover + registry (table/column derivation) — no DB, unit-testable.
2. framing + worker decode path — verify against POC-generated data.
3. ClickHouse store — DDL, batch insert, count.
4. cli fan-out (pool) + config + JSON summary.
5. throughput measurement.

## 10. Decisions to confirm in review

1. **Process pool, unit = one `.pb` file** (§3) — vs threads, or per-type workers.
2. **Payload is nested in field 6, scalars flattened to columns** (§4) — matches the spec; confirm the real type-protos follow it.
3. **MVP is append-only — no clean reload** (§5) — re-running duplicates; drop/replace deferred to the coordinator. Confirm that's acceptable for now.
```
