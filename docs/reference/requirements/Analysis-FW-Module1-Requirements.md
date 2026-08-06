# Analysis Framework — Module 1 (Offline Ingest) Requirements

**Date:** 2026-07-21 · **Parent doc:** `Analysis-FW-Production-Requirements.md` (full framework)
**Everything listed here is required for Module 1.** Tags (`SRC-1`) exist so the design can refer to items precisely.

---

## 1. Purpose

Read a completed `ProfileData-<tag>-<timestamp>` directory from local disk, decode every record, load it into the database — completely, correctly, repeatably.

Built so these land later **without reworking the core**:
1. Online/streaming input instead of files.
2. New payload fields, new layers, and new producers (UVP Control, TraceVision) — no code change.
3. A different database.

## 2. The format contract (foundation)

**Every Profile FW that feeds the Analysis FW must conform to `profile-log-format-details.txt`.** This is a mandated contract, not a per-producer negotiation. It fixes:

- **Generic header, fields 1–5** — `timestamp`, `hostname`, `component`, `tag`, `loglevel` — identical names, types and field numbers in every message.
- **Payload in field 6** — producer/layer-specific, the only part that varies.
- **Framing** — varint length-delimited records.
- **Directory hierarchy** — `ProfileData-<tag>-<timestamp>/` with `Config/`, `Linux/{Block,NVMe,Syscall,Filesystem,Memory}/`, `SSD/`.
- **Payload fields are scalars only** — no repeated, nested or map fields. Every payload field maps to exactly one column, mechanically.
- **Event time is the header timestamp** (`Sat Jun 27 14:40:48 2026`, 1-second resolution). Payloads carry **durations** (e.g. `latency_ns`), which are clock-independent — not absolute nanosecond timestamps.
- **Component ids** — the `component` value identifies the layer. Ids are **scoped per producer**, not globally.

**One database per producer.** Each Profile FW's data lands in its own ClickHouse database, so component ids cannot collide between producers. The producer identity is supplied at invocation (the run directory name does not carry it).

Because the header is fixed, a record is **self-describing**: the loader knows its time, host, layer, and target table without any per-producer mapping.

## 3. Where it sits

```
Airflow  — owns scheduling, retries, start/stop of profiling
   │  delivers the ProfileData-* tree locally, invokes Module 1 with a path
   ▼
MODULE 1:  source → decode → canonical record → validate → batch → store
   │
   ▼
Database (ClickHouse today, swappable)
```

Because Airflow orchestrates: Module 1 is a **one-shot job** (no scheduling, no folder watching, no daemon), and **Airflow retries killed tasks** — which is why idempotency below is a correctness requirement, not a convenience.

## 4. Out of scope

Fetching data from SUTs · streaming ingest · **non-conforming input (any format outside the contract above)** · query layer / API / UI · aggregate computation in code · multi-node DB deployment.

---

## 5. Seams (the core of this module)

*Structural requirements — each says one thing can be replaced without touching another.*

**Source — where records come from**
- **SRC-1** The engine reads from a *source abstraction*, never from files directly. Local-directory source is the only implementation.
- **SRC-2** Adding a source (live stream, remote fetch) requires no change to decode, record model, store, or pipeline.
- **SRC-3** Sources yield records **incrementally** — never "read the whole run into memory, then return". This is what lets a live source fit later, and bounds memory now.
- **SRC-4** Understands the defined hierarchy including rotated files, in deterministic order.
- **SRC-5** Input root path is configurable per invocation.

**Schema — fixed header, flexible payload**
*Verified: producer `.proto` files compile at runtime and decode data with no generated classes and no code change. Byte-identical to generated-class decode, ~176K rec/s (vs 578K static, 43K for the POC loader).*
- **SCH-1** Input is validated against the format contract (§2). Non-conforming input is **rejected with a clear, specific error** — not partially loaded, not guessed at.
- **SCH-2** The generic header is read the same way for every producer, with no per-producer configuration.
- **SCH-3** The `component` value determines the target table, within the producer's own database. Ids are producer-scoped, so no cross-producer collision is possible.
- **SCH-4** Payload schemas come from **the producer's `.proto` files, loaded at runtime**. Generated per-message classes must not be a build dependency of the loader.
- **SCH-5** Adding a payload field, a new layer, or a new conforming producer is **new `.proto` files plus a component-id entry — no code change.**
- **SCH-6** Payload column names and types are **derived from the schema**. Field definitions are not restated in application code.
- **SCH-7** Framing is validated: a truncated or corrupt record is detected at a record boundary and reported with file + byte position — never silently skipped.
- **SCH-8** The schema version in force is recorded with each loaded run. *(Protobuf matches fields by number, not name; reusing a field number silently changes the meaning of old data.)*
- **SCH-9** When the schema gains a field the table lacks, the loader **adds the column automatically** (`ALTER TABLE … ADD COLUMN`). Auto-migration is **add-only** — it must never drop a column or change an existing column's type; those are reported as errors for a human to resolve. Every automatic change is logged and included in the load report.
- **SCH-10** Data containing payload fields unknown to the current schema (producer newer than us) does not fail the load; the condition is counted and reported.

**Canonical record — the keystone**
- **REC-1** One internal record representation, independent of both protobuf and the database. The decoder produces it; the store consumes it; neither knows the other exists.
- **REC-2** It carries the generic header + layer payload. Provenance (source file, position) travels **alongside** each record rather than inside it, and `run_id` is load-scoped — so a failing record can always be traced to its exact origin without paying for provenance on every row at billions of rows.
- **REC-3** **No protobuf types past the decoder**, **no database types before the store** (no SQL upstream). This is the reviewable test that the seams are real.

**Store — where records go**
- **STORE-1** All database access sits behind a store interface. ClickHouse is the only implementation.
- **STORE-2** Replacing the database is a new adapter only — no change to source, decoder, record model, or pipeline. **No SQL or DB-specific types outside the adapter.**
- **STORE-3** The adapter owns: schema create/verify/migrate, batched write (configurable size), per-run count, per-run delete, connection lifecycle.
- **STORE-4** A second lightweight adapter (in-memory/file) exists for tests. An interface with one implementation is an unproven guess.

---

## 6. Run identity & idempotency

- **RUN-1** `run_id` **is the run directory name** — no separate convention. Data is keyed by `(run_id, sut_id)`, where `sut_id` is the `hostname` already present in every record header, so runs started on several SUTs with the same tag and timestamp stay separable.
- **RUN-2** **Loading the same run twice leaves exactly one copy** — by construction.
- **RUN-3** Explicit behaviour when the run already exists: fail (default), replace, or skip. Default must never silently duplicate.
- **RUN-4** **A killed or partial load is recoverable with no manual cleanup** — either it leaves nothing behind, or a retry cleanly replaces it. (Airflow will kill and retry.)
- **RUN-4a** Replace/reload operates at `(run_id, sut_id)` granularity — reloading one SUT must never remove another SUT's data from the same run.
- **RUN-5** Run metadata stored with the data: `run_id`, source path, tag, SUT, times, per-layer counts, schema + loader version.

## 7. Integrity

- **INT-1** Every load reconciles: **read == decoded == written == rows in DB**. A mismatch fails the load.
- **INT-2** Bad records are counted and quarantined with their provenance — never silently dropped. A configurable threshold fails the load.
- **INT-3** Each load emits a machine-readable report: per layer — files, read, decoded, written, quarantined; plus totals, duration, status.

## 8. Airflow contract

- **ORCH-1** Non-interactive CLI entry point; explicit arguments; no prompts.
- **ORCH-2** Distinct exit codes by failure class (config / input / integrity / schema / database) so Airflow can branch and decide whether retry is sensible.
- **ORCH-3** Machine-readable result (the INT-3 report) for Airflow to capture.
- **ORCH-4** Safe to retry, including after a hard kill.
- **ORCH-5** Structured, timestamped logs to stdout/stderr; progress visible for long loads.

## 9. Configuration

- **CFG-1** Everything operational is configurable, nothing hardcoded: input path, **producer identity and its target database**, DB connection, schema location, batch size, layer set, `run_id` rules, thresholds.
- **CFG-2** Validated at startup with actionable errors — fail before touching data.
- **CFG-3** Secrets (DB credentials) not in plain text in the config file.

## 10. Go-migration readiness

*Python now, Go for the streaming path later. The risk is two codebases drifting.*

- **PORT-1** Contracts live **outside** the Python code as language-neutral artifacts: the `.proto` files, DDL, component registry, config schema. (All natively consumable from Go.)
- **PORT-2** No Python-specific serialization in anything persisted or transmitted (no pickle) — files, DB, config, reports.

## 11. Testing

- **QUAL-1** End-to-end: fixture data → load → verify counts and content.
- **QUAL-2** Idempotency: load twice, assert one copy, in every RUN-3 mode. Plus failure paths: truncated file, bad record, DB down mid-load, killed process then retry.
- **QUAL-3** Seam tests: run against the second store adapter (STORE-4); and load a payload with an added field **without changing code** (SCH-5).
- **QUAL-4** Non-conformance test: input violating the format contract is rejected with a clear error (SCH-1).
- **QUAL-5** Code, `.proto`, DDL, component registry, and config schema versioned together.

---

## 12. Open decisions

**Decided:** one database per producer, so component ids cannot collide (§2) · payload fields are scalars only (§2) · event time is the header timestamp, payloads carry durations (§2) · `run_id` is the directory name, keyed with `sut_id` (RUN-1) · schema drift auto-migrates, add-only (SCH-9) · layer payloads stay TBD without blocking us — the `.proto` supplies them (SCH-4).

**Rollups / materialized views are not in scope** — no aggregation is built now. If they are added later, they must be created *before* the first load worth keeping, because a view only fills on INSERT and would otherwise need a manual backfill.

| # | Decision | Why it matters | When |
|---|----------|----------------|------|
| 1 | **Absolute nanosecond timestamps in payloads** — if Profile FW ever sends them, epoch vs boot-relative must be settled | Boot-relative time lands as "1970" and time-range queries silently return nothing | Only if payloads change |
| 2 | **Go migration trigger** | Keeps it a decision instead of a drift | When the streaming module starts |

## 13. Done when

1. Airflow invokes it with a path + config, and a complete tree lands in the DB.
2. Re-running leaves exactly one copy, in every RUN-3 mode.
3. Killing it mid-load and retrying yields a correct state, no manual cleanup.
4. Reconciliation passes; corrupted input is quarantined and reported, not dropped.
5. Non-conforming input is rejected with a clear error.
6. The suite proves the seams: a second store adapter, and a payload schema change loaded with no code change.
7. Throughput and load times are measured and documented.
