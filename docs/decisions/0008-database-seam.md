# 0008 — A database seam: neutral schema, adapter owns the dialect

**Status:** Accepted.

## Context

[ADR-0001](0001-database-clickhouse.md) picks ClickHouse but names explicit
revisit triggers (StarRocks / Apache Doris if auto-rebalance becomes a top
requirement). That decision is only cheap to revisit if swapping the database is
genuinely contained.

A `Store` protocol already existed and the driver was isolated, but ClickHouse
knowledge had leaked **above** the seam in four places: ClickHouse type-name
strings in the derived schema (`Column.ch_type`), `CREATE TABLE … MergeTree` DDL
generated in `registry.py`, ClickHouse's `DateTime` range hardcoded as a clamp in
`worker.py`, and `runner.py` importing `ClickHouseStore` directly. Adding a
database meant editing **four files outside `store/`**.

## Decision

Push every database-specific concern below the seam:

- **Neutral column types.** `registry.ColumnType` (STRING, UINT64, DATETIME, …)
  replaces ClickHouse type strings. The schema layer never names a SQL dialect;
  each adapter maps neutral → its own types.
- **DDL belongs to the adapter.** `create_table_ddl` moved from `registry.py` to
  `store/clickhouse.py`. `MergeTree` / `PARTITION BY` exist only there.
- **The timestamp clamp is the store's limit, not the loader's.** `Store` exposes
  `timestamp_range`; `worker.ts_for_db(s, ts_range)` clamps to what the *target*
  store can represent. ClickHouse declares 1970–2106 (unsigned 32-bit epoch); a
  store with a wider range keeps the value.
- **One factory.** `store/factory.make_store(kind, …)` is the only place a
  concrete adapter is named. Config gains `store.kind` plus an adapter-specific
  `store.options` map (which is where `async_insert` now lives).

## Consequences

- **Adding a database = one new module in `store/` + one line in the factory.**
  Nothing above the seam changes. Verified by a leak check: no ClickHouse type,
  SQL keyword, or setting appears outside `src/store/`.
- Three tests encode the guarantee so it cannot silently rot:
  `test_schema_is_database_neutral`, `test_adapter_owns_the_sql_dialect`,
  `test_ts_clamp_follows_the_store_not_the_loader`.
- `MemoryStore` remains a real second implementation — the whole pipeline and
  the full suite run with no database, which is what proves the seam works.
- **Not covered:** the analysis queries. `docs/block-metrics.md` uses ClickHouse
  SQL (`lagInFrame`, `toInt64`). Query portability is a separate, larger problem
  and is deliberately out of scope — swapping the DB means rewriting queries.
