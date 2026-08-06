# Analysis Framework — Module 1 (Offline Ingest) Design

**Date:** 2026-07-21 · **Implements:** `Analysis-FW-Module1-Requirements.md` · **Language:** Python 3.12+

---

## 1. Design principles

1. **Ports and adapters.** The core knows nothing about files, protobuf, or ClickHouse. Each is an adapter behind an interface. This satisfies SRC-2, SCH-4 and STORE-2 structurally rather than by discipline.
2. **The schema is data, not code.** Tables, columns and types are derived from the producer's `.proto` at runtime. No generated classes are a build dependency.
3. **Everything is counted.** A load either reconciles exactly or fails. There is no "mostly loaded".
4. **Retry is a first-class path, not an error path.** Airflow will kill and re-run this. The happy path and the retry path are the same code.

## 2. Architecture

```
                       ┌──────────────── CORE (no I/O, no deps) ────────────────┐
   ┌──────────┐        │                                                        │        ┌──────────────┐
   │ Source   │─Raw───▶│  Decoder ──▶ Record ──▶ Validator ──▶ Batcher          │──Rows─▶│    Store     │
   │ (port)   │ Record │      ▲                                    │            │        │    (port)    │
   └──────────┘        │      │                                    ▼            │        └──────────────┘
        ▲              │  SchemaRegistry                      Reconciler        │               ▲
        │              └──────────▲─────────────────────────────────┬───────────┘               │
   ┌────┴──────────┐              │                                 │                  ┌────────┴────────┐
   │ LocalDirSource│         ┌────┴──────┐                    Load Report        ┌─────┴─────┐ ┌─────────┴───┐
   │ (StreamSource │         │ .proto    │                          │            │ClickHouse │ │ MemoryStore │
   │  later)       │         │ files     │                          ▼            │  Store    │ │  (tests)    │
   └───────────────┘         └───────────┘                    CLI / Airflow      └───────────┘ └─────────────┘
```

**Dependency rule:** arrows point inward. Adapters import the core; the core never imports an adapter. Enforced by a test that fails if `core/` imports `sources/` or `stores/`.

## 3. The four contracts

### 3.1 Source port

```python
class RawRecord(NamedTuple):
    payload_bytes: bytes        # one serialized protobuf message
    expected_component: int     # from the file's place in the hierarchy (a hint)
    source_ref: str             # file name — for errors and quarantine
    record_index: int           # position within that file
    byte_offset: int            # exact offset — for framing errors (SCH-7)

class RecordSource(Protocol):
    def open(self) -> SourceInfo: ...          # run_id, files found, layers found
    def records(self) -> Iterator[RawRecord]:  # MUST yield incrementally (SRC-3)
    def close(self) -> None: ...
```

`LocalDirSource` walks the mandated hierarchy, orders rotated files deterministically (`block.log`, `block.1.log`, `block.2.log`…), and de-frames varint-delimited records.

**`Config/` is excluded from record discovery.** `run_config.log` is not a data layer — it carries no component id and is currently empty by decision. It is recorded in `SourceInfo` as present/absent but never parsed as records. When Profile FW defines its contents, it becomes a separate, explicit step.

**Provenance travels with the record, not inside it** (REC-2): `RawRecord` carries file, index and byte offset so any failure can be traced exactly, while the canonical `Record` stays lean for the billions-of-rows path.

### 3.2 Schema registry (core)

Built once at startup from the producer's `.proto` directory:

```python
class ColumnSpec(NamedTuple):
    name: str; ch_type: str; codec: str

class ComponentSchema(NamedTuple):
    component_id: int
    table: str                   # from config (see §4); never guessed
    message_cls: type            # dynamic class from the descriptor pool
    columns: list[ColumnSpec]    # header columns + payload columns, in order
    field_names: list[str]       # payload field names, in column order
    known_field_numbers: set[int]  # for unknown-field detection (SCH-10)
```

Construction: compile `*.proto` → `FileDescriptorSet` → `DescriptorPool` → dynamic message classes. Column specs are derived from the descriptor (name, protobuf type).

**Schema version** = `sha256` of the serialized `FileDescriptorSet`. Recorded per run (SCH-8), so the exact schema that produced any stored data is always identifiable.

### 3.3 Record (core)

```python
class Record(NamedTuple):
    component: int
    ts: datetime                 # parsed from the header timestamp string
    sut_id: str                  # header hostname
    tag: int
    loglevel: int
    values: tuple                # payload scalars, in ComponentSchema column order
```

`values` is a tuple rather than a dict: field order is already fixed by the schema, and a tuple avoids per-record dict construction at billions of rows. Names remain recoverable via the `ComponentSchema`.

**No protobuf object and no SQL type ever appears in this struct** — that is REC-3, checkable by inspection.

### 3.4 Store port

```python
class Store(Protocol):
    def connect(self, cfg) -> None: ...
    def ensure_schema(self, schemas: list[ComponentSchema]) -> list[str]:
        """Create missing tables; ADD missing columns. Never drop or retype.
           Returns the DDL applied, for the load report."""
    def begin_run(self, run_id, sut_id, mode) -> RunDecision: ...   # see §5
    def write(self, schema: ComponentSchema, rows: list[Record]) -> None: ...
    def count(self, run_id, sut_id, component) -> int: ...
    def drop_run(self, run_id, sut_id) -> None: ...
    def finish_run(self, run_id, sut_id, tag, counts, status) -> None: ...
    def close(self) -> None: ...
```

`MemoryStore` implements the same protocol for tests — this proves the seam (STORE-4, QUAL-3).

## 4. Schema → table derivation

**Fixed header columns** (identical for every component, because the contract mandates the header):

| Column | Type | Source |
|---|---|---|
| `run_id` | `String` | run directory name |
| `sut_id` | `String` | header `hostname` |
| `ts` | `DateTime` | parsed from header `timestamp` |
| `tag` | `UInt32` | header `tag` |
| `loglevel` | `UInt8` | header `loglevel` |
| `component` | `UInt8` | header `component` |
| `_loaded_at` | `DateTime` | ingest time |

*`sut_id` is plain `String`, not `LowCardinality(String)`, because it appears in `PARTITION BY` — partitioning on a LowCardinality column is an unnecessary risk. Cardinality is low anyway (tens of SUTs), so compression barely differs.*

**Payload columns** — one per protobuf field, name preserved, type mapped:

| protobuf | ClickHouse |
|---|---|
| `string`, `bytes` | `String` |
| `bool` | `Bool` |
| `int32`, `sint32`, `sfixed32` | `Int32` |
| `int64`, `sint64`, `sfixed64` | `Int64` |
| `uint32`, `fixed32` | `UInt32` |
| `uint64`, `fixed64` | `UInt64` |
| `float` / `double` | `Float32` / `Float64` |
| `enum` | `Int32` |

**Table shape:**

```sql
ENGINE = MergeTree
PARTITION BY (run_id, sut_id)
ORDER BY (run_id, sut_id, ts)
```

Partitioning by `(run_id, sut_id)` makes the load unit and the delete unit identical — one directory is one SUT's run, so reload is a partition drop (RUN-4a).

*Known limit:* partition count grows as runs × SUTs. Fine at POC and early production scale; if run count grows large, retention must drop old partitions or the partition key needs revisiting. Flagged, not solved here.

**Codecs:** default `ZSTD(1)`, with `DoubleDelta` on `ts` and `_loaded_at`. Per-column overrides come from config, since the `.proto` carries no performance hints.

**Component → message type and table name** are the two things a `.proto` cannot state, so both live in config:

```yaml
components:
  1: { message: profilefw.LinuxBlock, table: block }
  2: { message: profilefw.LinuxNvme,  table: nvme  }
```

*(A custom protobuf option could later move this into the `.proto` itself, making producers fully self-describing. Not needed for v1.)*

## 5. Idempotency and retry — the manifest

Airflow retries. A killed load leaves a partial partition, and the loader must recover **without a human**. A small manifest table in the producer's database makes partial loads *detectable*:

```sql
CREATE TABLE _load_runs (
    run_id String, sut_id String,
    status Enum8('in_progress'=1, 'complete'=2, 'failed'=3),
    tag UInt32,
    started_at DateTime, updated_at DateTime64(3),
    counts String,                    -- JSON, per component
    source_path String, schema_version String, loader_version String,
    attempt UInt32
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (run_id, sut_id);
```

**Two details that matter for correctness:**
- The version column is **`updated_at`**, set fresh on every write — *not* `started_at`, which is identical across the `in_progress` and `complete` writes of one attempt and would make deduplication non-deterministic.
- Manifest reads use **`SELECT … FINAL`**. `ReplacingMergeTree` only collapses duplicates on merge, so a plain read can return a stale row and send the retry logic down the wrong branch.

**`begin_run` decision table:**

| Manifest state | Action |
|---|---|
| no entry | proceed; mark `in_progress` |
| `in_progress` | a previous attempt died → **drop partition, reload** (self-healing) |
| `failed` | drop partition, reload |
| `complete` + mode `fail` | exit with "already loaded" (default) |
| `complete` + mode `skip` | exit 0, load nothing |
| `complete` + mode `replace` | drop partition, reload |

Drop-then-reload is not atomic (ClickHouse has no multi-statement transaction here), but it is **self-correcting**: a crash anywhere leaves `in_progress`, and the next attempt drops and starts clean. That is what makes ORCH-4 true.

## 6. Load flow

```
1  parse args ──▶ load config ──▶ validate            → exit 2 on failure
2  compile .proto ──▶ SchemaRegistry ──▶ schema_version → exit 4 on failure
3  store.connect ──▶ ensure_schema (add-only)         → exit 5 on failure
4  source.open ──▶ discover files (Config/ excluded), derive run_id
5  read first record ──▶ header ──▶ sut_id
6  store.begin_run(run_id, sut_id, mode)              → §5 decision
7  for each RawRecord:
       conformance checks (see below)                 (violation → exit 3)
       decode ──▶ Record          (bad → quarantine, count, check threshold)
       append to that component's batch
       batch full ──▶ store.write
       every N records ──▶ progress log               (ORCH-5)
8  flush remaining batches
9  reconcile (see below)                              → exit 6 on mismatch
10 store.finish_run(status=complete, tag, counts)
11 emit JSON load report ──▶ stdout                   → exit 0
```

**Conformance checks per record (SCH-1):**
- The header parses and `component` is present and known to the registry.
- `header.component` matches the file's `expected_component`. A mismatch means a misplaced or mislabelled file. *Verified viable: the mandated header parses standalone on every layer, so a record identifies its own layer independently of where it sits.*
- `header.hostname` equals the `sut_id` established in step 5. **A differing hostname mid-run is a conformance error, not a second partition** — one run directory is produced by one SUT. Checking only the first record would silently write later records into the wrong partition.

**Unknown-field detection (SCH-10):** protobuf silently accepts fields the schema doesn't know, and — verified — `UnknownFields()` is *not available* on dynamically-built classes under the upb backend, while re-serialization *preserves* unknown bytes so a size comparison detects nothing. So detection is done by **scanning the raw wire format for field numbers** and comparing against `known_field_numbers`. This runs on the **first record of each file**, not every record: a producer's schema is uniform within a file, so a sample is sufficient and the cost is O(files) rather than O(records). Unknown field numbers are reported in the load report; the load does not fail.

**Reconciliation (INT-1), the full chain:**
```
framed == decoded + quarantined          (nothing vanished between framing and decode)
decoded == written                       (nothing vanished between decode and store)
written == store.count(run_id, sut_id)   (the database agrees)
```
`async_insert` **must be disabled** on the ClickHouse connection. With it enabled, rows are buffered server-side and `count()` can read before they are visible — reconciliation would fail a load that is actually fine.

**Memory bound (SRC-3):** worst case is `batch_size × number_of_components` records buffered at once (≈120 MB at 100K × 6 components). Bounded and independent of run size, but it scales with component count — so the batcher flushes the largest pending batch if total buffered records exceed a configured cap.

## 7. Module layout

```
analysis_fw/
  cli.py              entry point: args, exit codes, JSON report
  config.py           load + validate; secrets from env
  errors.py           typed exceptions → exit codes
  core/
    record.py         Record
    schema.py         descriptor loading, SchemaRegistry, type mapping, version hash
    framing.py        varint de-framing; byte-accurate error positions
    wirescan.py       raw field-number scan (SCH-10)
    pipeline.py       the §6 flow
    reconcile.py      counters, quarantine, load report
  sources/
    base.py           RecordSource protocol
    local_dir.py      hierarchy walk, rotation ordering, Config/ exclusion
  stores/
    base.py           Store protocol
    clickhouse.py     DDL generation, batch insert, manifest
    memory.py         test adapter
  schemas/            .proto files per producer (contract artifacts)
tests/
```

## 8. Configuration

```yaml
producer:
  name: profile_fw
  database: profile_fw          # one database per producer

schema:
  proto_dir: ./schemas/profile_fw
  components:
    1: { message: profilefw.LinuxBlock, table: block }
    2: { message: profilefw.LinuxNvme,  table: nvme  }

source:
  type: local_dir
  framing: varint               # fixed by the format contract; setting exists for future transports

store:
  type: clickhouse
  host: localhost
  port: 8123
  batch_size: 100000
  max_buffered_records: 500000  # batcher flush cap (§6)
  auto_migrate: true            # add-only (SCH-9)
  # credentials from CH_USER / CH_PASSWORD env

load:
  mode: fail                    # fail | replace | skip
  components: []                # empty = all; else a subset of component ids (CFG-1)
  quarantine_dir: ./quarantine
  max_bad_records: 100
  progress_every: 100000

logging: { level: INFO, format: json }
```

Input path is **not** in config — Airflow passes it per invocation.

**Quarantine format** is JSON Lines — one object per bad record with `source_ref`, `record_index`, `byte_offset`, `error`, and the raw bytes base64-encoded. JSON and base64 keep it language-neutral and readable from Go (PORT-2); pickle or any Python-specific format is prohibited.

## 9. Errors and exit codes

| Code | Class | Retry sensible? |
|---|---|---|
| 0 | success | — |
| 2 | configuration invalid | no — fix config |
| 3 | source/input error (missing path, framing, non-conforming) | no — fix data |
| 4 | schema error (proto won't compile, component unmapped, type conflict) | no — fix schema |
| 5 | database error (connect, write, migrate) | **yes** — usually transient |
| 6 | integrity failure (reconciliation, quarantine threshold) | no — investigate |
| 1 | unexpected | maybe |

This split is the point of ORCH-2: Airflow retries 5, alerts on the rest.

## 10. Load report (stdout, JSON)

```json
{"status":"complete","run_id":"ProfileData-fio-4k-randread-20260627-144048",
 "sut_id":"dgx-spark-01","tag":1,"duration_s":12.4,"schema_version":"sha256:ab12…",
 "migrations_applied":["ALTER TABLE block ADD COLUMN retry_count UInt32"],
 "unknown_fields":{"1":[27]},
 "components":{"1":{"table":"block","files":1,"framed":1000,"decoded":1000,
                    "quarantined":0,"written":1000,"in_db":1000}},
 "totals":{"framed":6000,"decoded":6000,"quarantined":0,"written":6000,"in_db":6000}}
```

## 11. Test plan

| Test | Proves |
|---|---|
| End-to-end load, verify counts + values | QUAL-1 |
| Load twice in each mode (fail/replace/skip) | RUN-2, RUN-3 |
| Kill mid-load, re-run, assert correct state | RUN-4, ORCH-4 |
| Manifest read returns latest state after several writes | §5 correctness |
| Truncated file → error with byte position | SCH-7 |
| Corrupt record → quarantined, counted, threshold fires | INT-2 |
| Store down mid-load → exit 5, manifest `in_progress`, retry recovers | ORCH-2 |
| Run pipeline against `MemoryStore` | STORE-4, QUAL-3 |
| Add a payload field to `.proto`, reload with **no code change** | SCH-5, QUAL-3 |
| Data with a field the schema lacks → loaded, reported, not failed | SCH-10 |
| Header/path component mismatch → rejected | SCH-1, QUAL-4 |
| Second hostname mid-run → rejected | SCH-1 |
| `Config/run_config.log` present → ignored, not parsed | §3.1 |
| `core/` importing an adapter → fails | §2 dependency rule |

## 12. Build order

1. **Skeleton + contracts** — config, errors, the four protocol definitions, `MemoryStore`. Nothing real yet, but the seams exist and the dependency test passes.
2. **Schema** — proto compilation, registry, type mapping, version hash, DDL generation. Unit-testable with no database.
3. **Source** — hierarchy walk, rotation, framing, wire scan, incremental yield.
4. **Pipeline + reconcile** — end to end against `MemoryStore`. No ClickHouse needed yet.
5. **ClickHouse store** — DDL, batch insert, manifest, auto-migrate.
6. **CLI + report** — exit codes, JSON output, Airflow-facing behaviour.
7. **Hardening** — failure tests, kill/retry, throughput measurement.

Steps 1–4 need no database, so most of the logic is testable before touching the office server.

## 13. Traceability

| Requirement | Where satisfied |
|---|---|
| SRC-1..5 | §3.1 source port, `LocalDirSource` |
| SCH-1..10 | §4 derivation, §6 conformance checks + wire scan, §3.2 version hash |
| REC-1..3 | §3.3 Record — tuple of scalars; provenance in `RawRecord` |
| STORE-1..4 | §3.4 store port, ClickHouse + Memory adapters |
| RUN-1..5 | §5 manifest + `(run_id, sut_id)` partitioning |
| INT-1..3 | §6 reconciliation chain, §8 quarantine, §10 report |
| ORCH-1..5 | §9 exit codes, §10 report, §6 step 7 progress |
| CFG-1..3 | §8 config, env secrets |
| PORT-1..2 | `.proto` + DDL + config as artifacts; JSON/base64 report and quarantine |
| QUAL-1..5 | §11 test plan |

## 14. To verify on the real server

- `PARTITION BY (run_id, sut_id)` with two `String` columns, and `DROP PARTITION` by tuple value — expected to work, not yet run against ClickHouse 25.6.
- Partition-count behaviour as runs accumulate.

## 15. Open items carried from requirements

- Absolute nanosecond timestamps in payloads — only if Profile FW changes to send them.
- Go migration trigger — revisit when the streaming module starts.
