# 0007 — Streaming transport ladder (Vector → gRPC gateway → Kafka)

**Status:** Accepted **direction** (POC 1 study output) — to be **built and validated
in POC 2**.

## Context

Production ingest must be **live, at fleet scale, with zero loss** while obeying the
**never-block rule** (nothing in the profiling hot path touches the network or waits
on a consumer). Offline files were a POC-1-only mechanism. Event rate assumption:
~200K–1M events/s per SUT; loss unacceptable, lag acceptable; open-source only.

## Decision

Adopt the streaming architecture and this **transport preference order**:

1. **Default — Vector agent → (optional Vector aggregator) → ClickHouse.** Prebuilt
   disk buffers, checkpointing, end-to-end ACKs; MPL-2.0. Benchmark at 1M ev/s/SUT
   within the on-SUT budget (8 cores, 8–16 GB).
2. **Escape hatch — custom gRPC batch push → stateless ingest gateway(s) → ClickHouse.**
   Batches with `batch_id`, ACK only after ClickHouse commit, insert dedup →
   effectively exactly-once; stateless gateways scale horizontally.
3. **Kafka — last resort.** Only if replay-as-a-feature or multiple independent
   consumers become requirements (no-loss is already met without it).

Supporting design (also decided):
- **Two lanes:** fast aggregates (~1–5 s, live dashboards) + trailing raw events
  (~30–60 s). Live view never depends on moving the firehose in real time.
- **Tiered buffering:** RAM in steady state; overflow-spill to local scratch disk
  (**not** the SUT's SSD) past a threshold. Backpressure grows the spill; it never
  reaches back to the ring buffer.
- **No-loss accounting:** drop counters at every stage + per-run reconciliation.

## Consequences

- POC 2's build starts from this ladder rather than re-deciding transport.
- Sizing math to validate: 1M ev/s × ~80 B ≈ 80 MB/s raw/SUT → ~10–17 MB/s compressed
  (zstd) → ~450 MB/s aggregate at 30 SUTs — fine on 25GbE + a 3–5-node cluster.
- Requires SUT provisioning: **separate NIC** for data collection + **non-SUT scratch
  disk** for spill.
- Full reasoning: [../reference/context/CONTEXT-System-Analysis-FW-Session.md](../reference/context/CONTEXT-System-Analysis-FW-Session.md)
  (§4) and the POC 1 streaming study
  ([../reference/context/POC1_Analysis_FW_Activities.md](../reference/context/POC1_Analysis_FW_Activities.md), Activity 5).
