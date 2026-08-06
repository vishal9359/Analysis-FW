# 0005 — `UInt64` sequence `run_id`/`record_id` as an MVP stopgap

**Status:** Accepted for the MVP — to be revisited for multi-node.

## Context

Every row needs identity: a `run_id` per run, and (where child tables exist) a
`record_id` linking a main row to its child rows. Options were weighed:

- **UUID** — too much space at billions of rows.
- **Producer-assigned id** — rejected: N SUTs/producers can assign the same id
  (collision).
- **Plain sequential counter** — not safe across multiple loader nodes.

## Decision

For the MVP (single loader node): **`run_id`** = the run directory name
(`ProfileData-<tag>-<timestamp>`); **`record_id`** = a `UInt64` sequence the loader
assigns per record, only when a payload has child tables.

Explicit user call: *"use a uint64 sequence number for now; when we re-architect
we'll pick the right approach."*

## Consequences

- Works and is compact for a single-node loader.
- **Not multi-node-safe** — when POC 2 introduces multiple loader/gateway nodes, both
  ids need a collision-free scheme (**Snowflake 64-bit** or **UUIDv7**), and `run_id`
  generation needs a multi-node-safe convention. Tracked as POC 2 schema work
  ([../roadmap.md](../roadmap.md), activity D).
