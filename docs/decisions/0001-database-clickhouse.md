# 0001 — ClickHouse (OSS) as the database

**Status:** Accepted for POC 1 — to be revisited after POC 1.

## Context

The Analysis FW stores **per-IO trace events** (millions/s, high-cardinality: PIDs,
LBAs, queues) plus periodic telemetry, and must support **cross-layer JOINs** and
**ad-hoc queries** (the queries are not known in advance). Scale grows from 6–12 to
10–30 SUTs → multi-node needed. Licenses must be **free/open-source**.

## Decision

Use **ClickHouse (open-source, Apache-2.0, self-hosted)**. Start single-node + one
replica (ReplicatedMergeTree + ClickHouse Keeper); Grafana for UI; Python
`clickhouse-connect` for analysis; Kubernetes Operator if/when multi-node.

**Why it won the 4 binding requirements:** high-cardinality trace ingestion,
cross-layer JOINs, ad-hoc SQL, and completely free horizontal scaling — the only
engine meeting all four while being battle-tested at observability scale
(Cloudflare, Uber, Anthropic, OpenAI, Sentry). Full comparison and why each
alternative lost: [../reference/db-research/](../reference/db-research/) and
[../reference/context/CONTEXT-Database-Selection-Session.md](../reference/context/CONTEXT-Database-Selection-Session.md).

## Consequences

- Our **log-then-bulk-load** pipeline lands in ClickHouse's sweet spot (batch inserts;
  its small-insert weakness never applies).
- **Known weakness — ClickHouse OSS does not auto-rebalance shards.** Mitigated by a
  layered strategy: partition by time + `ORDER BY (run_id, …)`; add capacity via
  **shard weights** (config change, no data moved); point the loader at a new shard to
  balance faster; **re-ingest from raw logs** as the true-reshard escape hatch.
  `clickhouse-copier` is **deprecated — do not use it.**
- **Revisit triggers (post-POC 1):** if auto-rebalance becomes a top requirement,
  bake-off **StarRocks / Apache Doris** (Apache-2.0, auto-rebalance, meet all hard
  constraints — the strongest alternatives, not yet in the comparison matrices). If
  willing to pay, ClickHouse Cloud (SharedMergeTree) is a drop-in.
- The only test that truly decides is `clickhouse-benchmark` on **our own trace data on
  our own box**, using the ~10 analysis queries from the requirements.
