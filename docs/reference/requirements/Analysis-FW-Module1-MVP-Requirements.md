# Analysis Framework — Module 1 (MVP) — High-Level Requirements

**Date:** 2026-07-21 · **Scope:** bare-minimum core only — read offline files, load into ClickHouse.
**Supersedes for build scope:** the fuller `Analysis-FW-Module1-Requirements.md` / `-Design.md` (kept as reference; extras deferred below).

## Naming convention (input contract)

```
<layer>_<type>.proto        schema, one message (contract header + payload)
<layer>_<type>.pb           data, single file
<layer>_<type>.<NNN>.pb     data, split parts in order 000, 001, …
```
- `.pb` pairs with the `.proto` of the same `<layer>_<type>` stem.
- All parts of a stem = one logical stream.
- Table name = the stem.

## High-level requirements

**A. Input & discovery**
1. Read one `ProfileData-<tag>-<timestamp>` tree from a local path (given per run).
2. Walk the layer folders; discover every `<layer>_<type>` pair automatically.
3. A layer may have many types; a type may span many split `.pb` files.
4. Ignore `Config/`.

**B. Schema registry**
5. Compile each `.proto` found in the tree at runtime (schema ships with data).
6. Derive table name, columns and types from the proto — no hand-written schema.
7. Payload fields are scalars only.
8. Each `<layer>_<type>` maps to its own table.

**C. Read**
9. Decode length-delimited protobuf records incrementally (memory bounded, not whole-file).
10. Read a type's split parts in order as one stream.

**D. Validate (minimum)**
11. Detect truncated / corrupt records at a boundary and fail clearly.
12. Read the contract header (component, hostname, timestamp) from every record.

**E. Write**
13. Create the database (one per producer) and tables if missing.
14. Batch rows and bulk-insert into each type's table (configurable batch size).
15. `run_id` = directory name, keyed with `sut_id` (hostname).
16. Reloading a run replaces cleanly — no duplicates.
17. Basic reconciliation: rows written == rows in table for the run; mismatch fails.

**F. Parallelism**
18. Load independent units (types and split parts) concurrently with a configurable worker pool.
19. Use efficient bulk inserts; per-table writes stay consistent and countable.

**G. Run & config**
20. One-shot CLI: input path + config, non-interactive, clear success/failure exit code.
21. Configurable: DB connection, batch size, worker count, reload mode. Secrets from env.

## Deferred (not in MVP — from the fuller design)

Crash-recovery manifest · add-only auto-migrate · unknown-field detection · quarantine of bad records · schema-version tracking · streaming/remote fetch · query / UI · rollups.
