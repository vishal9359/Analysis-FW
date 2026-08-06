# Overview — what this project is and why

*This is the "why" and the boundary. For how the code works today see
[architecture.md](architecture.md); for what's next see [roadmap.md](roadmap.md);
for terms see [glossary.md](glossary.md).*

## The System Analysis Framework

A **System Analysis Framework** that runs AI (and enterprise-storage) workloads on
GPU-based systems (**DGX Spark**, DGX Workstation), **profiles the full host + SSD
stack**, and analyzes **where the IO overheads are** — split into host-domain vs
SSD-domain behavior. The insight feeds future SSD/storage development.

It serves **three projects** that all need the same profiling + analysis on
different SSD types / use-cases: **On-Device AI, TraceVision, UVP Control**. Rather
than build it three times, it is one common framework.

Source of truth for requirements: [reference/requirements/Frameworks.txt](reference/requirements/Frameworks.txt) (the PM's document).

## Sub-frameworks (and who owns what)

The framework is split into sub-frameworks, **one engineer each**:

- **Use-case Run & Deploy FW** — deploys/configures the SUT, runs the workloads.
- **Profile FW** — collects host + SSD data on the SUT (eBPF-based), writes it to a
  defined log-folder hierarchy in a defined format.
- **Analysis FW** — *this project* — everything from the SUT outward.
- **UVP Control** — UVP-specific control (separate).

```
 On-Device AI      TraceVision       UVP Control
       \               |               /
        \______________|______________/
                       ▼
        ┌───────────── System Analysis Framework ─────────────┐
        │  Use-case Run & Deploy FW      Tool Dev & Deployment │
        │  Profile FW    UVP Control     Analysis FW  ← us     │
        └─────────────────────────────────────────────────────┘
```

## The ownership boundary (important)

**Profile FW produces data on the SUT. Analysis FW owns everything from the SUT
outward** — shipping the data off the SUT, ingesting it, the database, the queries,
and the UI. The seam between the two is the **data format + log-folder contract**
(see [architecture.md](architecture.md) and
[reference/requirements/profile-log-format-details.txt](reference/requirements/profile-log-format-details.txt)).

## The data (this drives every design choice)

1. **Per-IO trace events** (eBPF / blktrace) — the dominant volume: **~200K–1M
   events/s per SUT**, peak ~30M/s aggregate at 30 SUTs. High-cardinality
   (PIDs, LBAs, queues), irregular, event-shaped.
2. **Periodic telemetry** — CPU/GPU/memory utilization, NVMe queue stats, SSD
   SMART/log-page samples. Secondary volume.

**Both raw per-IO events AND aggregates are kept durable.** That is the driving
storage constraint.

## Hard constraints (from requirements + confirmed decisions)

- **Profiling must not perturb the SUT** — collection must not steal CPU/IO from the
  system being measured, and must **not write to the SSD under test**.
- **Never-block rule** — nothing in the profiling hot path touches the network or
  waits for a consumer. (Shapes the production streaming design.)
- **Loss is not acceptable; lag is.**
- **Queries are not fully known in advance** — the DB must support ad-hoc SQL, not
  just pre-baked queries.
- **Open-source / free-for-use licenses only** (e.g. Kafka/ClickHouse Apache-2.0 fine).
- Scale: **6–12 SUTs near-term, 10–30 at production** → multi-node DB required.
- Start on **Linux + DGX Spark** (ARM/Grace); Windows and other platforms later.

## Users & UI

Audience ranges from SSD firmware engineers to senior management. **UI is primary
for everyone; the CLI must stay in sync with the UI** — both are thin clients over
one shared backend. POC 1 UI = **Grafana default screens only**; custom screens come
later.

## How the work is organized

Agile, **2–3 POCs in phases**, each phase teaching the next. The user's manager
supplies high-level activities; the user details them for senior review. Current
state and the phase plan are in [roadmap.md](roadmap.md).
