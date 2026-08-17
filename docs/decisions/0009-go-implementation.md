# 0009 — Go implementation of Module 1

**Status:** Accepted. Supersedes the Python implementation, which is removed.

## Context

Module 1 was written in Python. The stated drivers for moving to Go were
**deployment simplicity** and **performance**. Both were measured before and
after the port rather than assumed.

## Decision

Port Module 1 to Go, mirroring the existing design exactly: same pipeline
(`discover → registry → reader → worker → store`), same database seam
([ADR-0008](0008-database-seam.md)), same input contract, same exit codes, same
JSON report, same 26 tests (plus 2 Go-specific ones).

Dependencies, pinned for Go 1.18 compatibility:

| Need | Library |
|---|---|
| Parse `.proto` at runtime | `github.com/bufbuild/protocompile` |
| Dynamic messages | `google.golang.org/protobuf` (`dynamicpb`) |
| ClickHouse | `github.com/ClickHouse/clickhouse-go/v2` |
| Bounded concurrency | `golang.org/x/sync/errgroup` |
| Config | `gopkg.in/yaml.v3` |

## Consequences

### Deployment — the goal was met

- **A single static binary.** No Python runtime, no `pip`, no virtualenv.
- **No `protoc` at all.** The Python version shelled out to the `protoc` bundled
  in `grpcio-tools`; `protocompile` parses `.proto` in pure Go. This removes the
  last external process dependency.
- **Cross-compiles.** `GOOS=linux GOARCH=amd64 go build ./cmd/analysis-fw`
  produces the office-box binary from any machine.

### Concurrency — simpler than what it replaced

Python used a **process pool** solely to escape the GIL, which forced a
picklable-job design: each child re-compiled the schema from descriptor bytes and
opened its own database connection. Go uses a **bounded goroutine pool**
(`errgroup` + semaphore), so the schema is compiled once and shared, and one
pooled connection serves every worker. `store.workers` keeps its meaning
(how many units load at once); the mechanism is lighter and the code is shorter.
`TestParallelMatchesSequential` pins the two paths to identical output.

### Performance — the goal was NOT met; Go is slower here

Measured on the same machine and the same 50,000 records, decode + row-building
only (in-memory store, no database in the measurement):

| | Throughput | Time |
|---|---|---|
| Python | ~126,000 records/s | 397 ms |
| **Go** | **~74,000 records/s** | **674 ms** |

Go is roughly **1.7× slower**, and profiling shows why: `dynamicpb` unmarshal
alone accounts for **454 ms of the 674 ms (67%)**. Python's protobuf runtime uses
the C++ `upb` backend even for dynamic messages; Go's `dynamicpb` is pure-Go
reflection, costing ~4M allocations for 50k records.

This is **not fixable by tuning.** An arena allocator for row building was tried
and reverted — it changed nothing, because row building is not the bottleneck.
The only way to close the gap is to abandon dynamic messages for generated Go
structs, which would require a build step per producer `.proto` and destroy the
runtime-schema property that is the point of the design
([ADR-0002](0002-runtime-schema-from-proto.md)).

**Accepted deliberately**, because:
- Current data volumes make it irrelevant — a run of sampled per-second counters
  is thousands of records, loading in well under a second either way.
- `store.workers` is a free knob; the loader is not the pipeline's bottleneck
  (ClickHouse ingest is).
- The deployment win is real and permanent.

**Revisit if** per-IO event tracing lands (~1M events/s → ~1.4B records per run),
where the 1.7× would cost real hours. The fix then is a generated-struct fast
path for known layers, keeping `dynamicpb` as the generic fallback — not a
language change.

### Operational change — the ClickHouse port

`clickhouse-go` speaks the **native protocol on port 9000**, where the Python
`clickhouse-connect` used **HTTP on 8123**. `config/config.yaml` now defaults to
`9000`. A deployment that exposed only 8123 must open 9000 (the standard
ClickHouse image serves both).
