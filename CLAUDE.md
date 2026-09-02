# CLAUDE.md — start here

This file is the entry point for Claude working in this repo. It's an **index + the
always-true working rules**. Read the linked docs on demand; don't duplicate their
content here.

## What this is (one paragraph)

**Analysis FW** — the analysis half of a **System Analysis Framework** that profiles
SSD/host IO overheads for AI workloads on GPU systems (DGX Spark). Analysis FW owns
everything **from the SUT outward**: ingest → ClickHouse → queries → UI. The code
today is **Module 1: an offline loader** that reads Profile FW protobuf profiling data
and writes it to ClickHouse, with the schema **derived from the producer's `.proto` at
runtime**. Full charter: [docs/overview.md](docs/overview.md).

## Context map — every context file

```
CLAUDE.md                     ← you are here: entry index + working rules
README.md                     ← developer quickstart (install, run, test)
docs/
├── overview.md               ← charter: what/why, sub-frameworks, ownership, data shape
├── architecture.md           ← end-to-end data flow; current MVP vs streaming target
├── roadmap.md                ← POC 1 done · MVP now · POC 2 next · deferred · open questions
├── glossary.md               ← domain terms (IO stack, eBPF, ids, DB, .pb format, streaming)
├── design.md                 ← MVP loader design detail
├── block-metrics.md          ← IOPS / bandwidth / latency query recipes
├── timestamp-format.md       ← timestamp contract + parsing
├── decisions/                ← ADRs — append-only; "why is it built this way?"
│   ├── README.md             ← ADR index + how to add one
│   ├── 0001…0007-*.md        ← ClickHouse · runtime-schema · .pb-format · src-layout ·
│   │                            id-stopgap · append-only · streaming-ladder
│   └── 0010-payload-field-shapes-to-tables.md
│                              ← how field shape decides the table (0008/0009 are
│                                taken on the `query` / `go_version` branches)
└── reference/                ← imported snapshots from D:\Frameworks (SOURCE, not canonical)
    ├── README.md             ← index of everything below
    ├── requirements/         ← Frameworks.txt (source of truth), prod/module reqs,
    │                            data-format spec + decoded samples
    ├── context/              ← session context, POC 1 plans, cross-framework contracts,
    │                            retired dummy-data notes
    └── db-research/          ← ClickHouse decision + TSDB study + HA cluster runbook
```

**Reading order for a cold start:** this file → `docs/overview.md` →
`docs/architecture.md` → `docs/roadmap.md`. Pull anything else from the map above or
the task router below as the work needs it. `docs/reference/` is source material —
where it and an ADR disagree, the ADR wins.

## Read-when map

| If you're… | Read |
|---|---|
| New to the project / need the "why" & boundary | [docs/overview.md](docs/overview.md) |
| Working on the code / data flow | [docs/architecture.md](docs/architecture.md) + [README.md](README.md) |
| Planning next work / POC 2 / what's deferred | [docs/roadmap.md](docs/roadmap.md) |
| Hitting an unfamiliar term | [docs/glossary.md](docs/glossary.md) |
| Asking "why is it built this way?" | [docs/decisions/](docs/decisions/) (ADRs) |
| Needing the MVP design detail | [docs/design.md](docs/design.md) |
| Writing block metric queries (IOPS/BW/latency) | [docs/block-metrics.md](docs/block-metrics.md) |
| Dealing with timestamps | [docs/timestamp-format.md](docs/timestamp-format.md) |
| Needing requirements / DB study / raw context | [docs/reference/](docs/reference/) |

## Status (keep this current)

- **Now:** Module 1 offline loader — **built, 30 tests pass**, in good shape.
- **Next:** POC 2 — live streaming ingestion at fleet scale (see [docs/roadmap.md](docs/roadmap.md)).
- **Deferred debt** (all tracked in ADRs): append-only reload, single-node
  `run_id`/`record_id`, schema auto-migrate.

## Working rules (this repo's conventions)

- **Run it:** `python -m src <ProfileData-dir>` **from the repo root** (the package is
  `src/`; there is deliberately no `pyproject.toml` — see
  [ADR-0004](docs/decisions/0004-src-layout-and-external-config.md)). Batch: point at a
  parent of `ProfileData-*` runs; `--batch`, `--continue-on-error`.
- **Config:** `config/config.yaml` (repo root, editable). Override with `--config PATH`
  or `CH_HOST`/`CH_PORT` env.
- **Test:** `python -m pytest tests/ -q` — runs the whole pipeline against an in-memory
  store, **no database needed**. The fixture rebuilds automatically when any
  `tests/sample_protos/*.proto` changes; generate one by hand with
  `python tests/make_fixture.py <dir>`. Adding a `.proto` there needs **no test edit** —
  the unit set and record total are derived from the protos on disk.
- **Schema is generic:** never hardcode payload/header field names in the loader — it
  derives everything from the `.proto` by structure. protobuf 7.x: use
  `FieldDescriptor.is_repeated` (not `.label`). See
  [ADR-0002](docs/decisions/0002-runtime-schema-from-proto.md).
- **Field shape decides the table:** scalar → column · singular message → flattened
  prefixed columns · repeated → child table · repeated-inside-repeated → one leaf table
  with the outer level's scalars denormalized onto each row · repeated scalar →
  rejected. Add a shape by extending these rules, never by special-casing a field name.
  See [ADR-0010](docs/decisions/0010-payload-field-shapes-to-tables.md).
- **Ingest is append-only** (re-loading a run duplicates rows) — see
  [ADR-0006](docs/decisions/0006-mvp-append-only-ingest.md).
- **Timestamps** stored as RFC 3339 UTC; the loader tolerates several formats and
  clamps out-of-range — see [docs/timestamp-format.md](docs/timestamp-format.md).
- **Commit/push only when asked.** Git user is **vishal9359**; remote is
  `github.com/vishal9359/Analysis-FW`. Co-author trailer as configured.
- **Three live branches** — `main` (Python), `query` (+ database seam, ADR-0008),
  `go_version` (+ Go port, ADR-0009). They have diverged: a loader fix generally needs
  applying to both the Python and Go implementations, and a new ADR on `main` must skip
  the numbers the other branches already use.

## Environment

- **This dev machine:** Windows; **no ClickHouse here** — validate DB behavior via the
  in-memory store and tests. The real ClickHouse insert is verified on the office box.
- **Office test box:** Linux (IST timezone), Python 3.12, **ClickHouse 25.6 in Docker
  at `localhost:8123`, no password, database `profile_fw`**. The user pulls the repo
  there to run against real data.

## How this project's context is organized (so it scales)

- **overview / architecture / roadmap / glossary** = the maintained knowledge base
  (the durable "why / how / what-next / terms"). **Edit these in place.**
- **decisions/** = ADRs, one per decision, **append-only** — add a new ADR to change a
  decision; don't rewrite an old one. This is how "why is it like this?" stays
  answerable as the project grows.
- **reference/** = imported snapshots from the `D:\Frameworks` planning workspace
  (requirements, session context, DB research) so the repo is self-contained. **These
  are source material, not the current position — where a snapshot and an ADR disagree,
  the ADR wins.**
- **Maintenance protocol:** update docs in the same change as the code; record a
  decision as an ADR; re-copy a reference file if the upstream changes. Keep this
  `CLAUDE.md` a small index — push detail down into the docs.
