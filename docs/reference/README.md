# Reference — imported source material (snapshots)

These files are **snapshots** imported from the cross-framework planning workspace
(`D:\Frameworks`) so this repo is **self-contained** — a fresh clone has the full
past/present/future context without needing that workspace.

**Read these as source material, not as the current position.** The **canonical,
maintained** view lives in the docs above them:
[overview.md](../overview.md) · [architecture.md](../architecture.md) ·
[roadmap.md](../roadmap.md) · [glossary.md](../glossary.md) ·
[decisions/](../decisions/). Where a snapshot and an ADR disagree, **the ADR wins**
(the snapshots are dated July–Aug 2026 and may contain noted-but-unreconciled points).

## requirements/ — what the framework must do

| File | What it is | Note |
|---|---|---|
| `Frameworks.txt` | The PM's requirements document — **the requirements source of truth** | Covers all sub-frameworks; Analysis FW is our slice |
| `Analysis-FW-Production-Requirements.md` | Production requirements for Analysis FW | Forward-looking |
| `Analysis-FW-Module1-Requirements.md` | Module 1 (loader) requirements | Superseded in part by the MVP variant |
| `Analysis-FW-Module1-MVP-Requirements.md` | Trimmed MVP requirements for Module 1 | What the current loader was built to |
| `Analysis-FW-Module1-Design.md` | Fuller/earlier Module 1 design | Design history; the maintained design is [../design.md](../design.md) |
| `profile-log-format-details.txt` | The profiling **data-format spec** (the Profile FW ↔ Analysis FW seam) | Directly relevant to the loader |

## context/ — session hand-off notes (how we got here)

| File | What it is |
|---|---|
| `CONTEXT-System-Analysis-FW-Session.md` | Master planning context: IO stack, streaming architecture, POC plan, decision ledger |
| `CONTEXT-Database-Selection-Session.md` | The DB-selection reasoning, verified findings, rebalancing strategy, revisit triggers |
| `POC1_Analysis_FW_Activities.md` | The detailed POC 1 activity plan (boss's 4 items + streaming study) |

> These two `CONTEXT-*` files are **session-organized** (a hand-off pattern). Their
> durable content has been distilled into the topic-organized docs above; they are kept
> here for depth and provenance.

## db-research/ — the database evaluation (backs ADR-0001)

| File | What it is |
|---|---|
| `ClickHouse-Database-Selection.md` | Executive decision doc (comparison, architecture, adopters, references) |
| `DB_Study_TimeSeries_Comparison.md` | Feature/lifecycle/ingest comparison matrices, LTTB, bake-off dimensions |
| `TimeSeries_DB_Landscape.md` | Why each DB family exists; our data = event-OLAP shape |
| `Time-Series-Database-Decision.md` | Detailed decision doc (matrices, OSS vs Cloud, benchmarks) |
| `Time-Series-Databases-Guide.md` | Full landscape + history reference |
| `TimeSeries-Database-Research.md` | Background research |

Current position and open issues (auto-rebalance, StarRocks/Doris revisit) are in
[../decisions/0001-database-clickhouse.md](../decisions/0001-database-clickhouse.md).

## Keeping these in sync

When one of these changes in the planning workspace and it matters here, **re-copy the
file** and, if it changes a decision, **add or update an ADR** — don't edit the snapshot
to reflect new thinking (that's what the canonical docs are for).
