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
AGENTS.md                     ← pointer here, for Codex and other AI agents
README.md                     ← USER-FACING release doc (install, configure, run,
                                query, troubleshoot) — no internals
knowledge/                    ← what every column means (Measurement Catalog), reviewed
                                by engineers; README.md says how; checked by
                                `python tools/catalog.py check`
src/                          ← Module 1, the loader: python -m src <run>
analysis/                     ← Phase 1 code: python -m analysis ... (steps 0, views,
                                WrenAI data models, check C1 so far)
tools/                        ← office-box checks (check_phase1_env.py, check_llm.py) +
                                catalog.py; config.json = credentials, git-ignored
docs/
├── development.md            ← developer guide: architecture, input contract,
│                             derived schema, fixture generator, tests, Phase 1 commands
├── overview.md               ← charter: what/why, sub-frameworks, ownership, data shape
├── architecture.md           ← end-to-end data flow; current MVP vs streaming target
├── roadmap.md                ← STATUS BOARD: MVP done · Phase 1 now (done / next /
│                             waiting / known issues) · Phase 2 · POC 2 · deferred
├── goals-and-requirements.md ← goals + requirements (DRAFT): Phase 1 = plain-language
│                             request to Grafana graph; Phase 2+ = automated reports
├── phase1-design.md          ← Phase 1 design: who does what (ours · WrenAI · our LLM),
│                             request flow, views, Grafana, checks, build order (DRAFT)
├── phase1-tool-research.md   ← open-source tool comparison for Phase 1 (+ sources)
├── Profile-code-flow.md      ← how Profile FW works: eBPF → maps → Go collectors → .pb
├── Tracepoints-details.md    ← every tracepoint / kprobe / sysfs source, per collector
├── analysis-architecture.md   ← high-level architecture of the Phase 2 report system
│                             (DRAFT): 6 parts, 6 steps, 2 diagrams
├── diagrams/                  ← architecture + workflow SVGs
├── glossary.md               ← domain terms (IO stack, eBPF, ids, DB, .pb format, streaming,
│                             Phase 1)
├── design.md                 ← MVP loader design detail (partly out of date: roadmap,
│                             "Known issues"; the code and docs/development.md win)
├── block-metrics.md          ← IOPS / bandwidth / latency query recipes
├── timestamp-format.md       ← timestamp contract + parsing
├── decisions/                ← ADRs — append-only; "why is it built this way?"
│   ├── README.md             ← ADR index + how to add one
│   ├── 0001…0007-*.md        ← ClickHouse · runtime-schema · .pb-format · src-layout ·
│   │                            id-stopgap · append-only · streaming-ladder
│   ├── 0010-payload-field-shapes-to-tables.md
│   │                          ← how field shape decides the table (0008/0009 are
│   │                            taken on the `query` / `go_version` branches)
│   └── 0011-run-level-config-sidecars.md
│                              ← run-level sidecar files → own tables (latency_buckets)
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
| Working on the code / data flow | [docs/architecture.md](docs/architecture.md) + [docs/development.md](docs/development.md) |
| Changing anything a user sees (CLI, config, errors, tables) | [README.md](README.md) — keep it accurate; it is the release doc |
| Picking up the work: what is done, what is next, what is pending | [docs/roadmap.md](docs/roadmap.md), "Now — Phase 1" |
| Planning next work / POC 2 / what's deferred | [docs/roadmap.md](docs/roadmap.md) |
| Working on Phase 1 (plain-language request to graph) | [docs/phase1-design.md](docs/phase1-design.md), then [docs/goals-and-requirements.md](docs/goals-and-requirements.md) + [docs/phase1-tool-research.md](docs/phase1-tool-research.md) |
| Setting up a machine / running Phase 1 commands | [docs/development.md](docs/development.md), "Phase 1" (ClickHouse in WSL2, a simulated run) |
| Reviewing or extending the Measurement Catalog | [knowledge/README.md](knowledge/README.md) |
| Working on the Phase 2 report system | [docs/goals-and-requirements.md](docs/goals-and-requirements.md) (Phase 2 part) |
| Designing the Phase 2 report system | [docs/analysis-architecture.md](docs/analysis-architecture.md) (draft) |
| Hitting an unfamiliar term | [docs/glossary.md](docs/glossary.md) |
| Asking "why is it built this way?" | [docs/decisions/](docs/decisions/) (ADRs) |
| Needing the MVP design detail | [docs/design.md](docs/design.md) |
| Writing block metric queries (IOPS/BW/latency) | [docs/block-metrics.md](docs/block-metrics.md) |
| Dealing with timestamps | [docs/timestamp-format.md](docs/timestamp-format.md) |
| Needing requirements / DB study / raw context | [docs/reference/](docs/reference/) |

## Status (keep this current)

The full board — done, next, waiting, known issues — is [docs/roadmap.md](docs/roadmap.md),
"Now — Phase 1". In short (2026-10-16):

- **Branch:** Phase 1 work is on **`phase1`**, not yet merged into `main`. All 62 tests
  pass (46 loader, 16 Phase 1); `python tools/catalog.py check` passes.
- **Done:** Module 1 loader. Phase 1 requirements, tool choice (WrenAI trial, Grafana
  draws), office-box readiness check, design ([docs/phase1-design.md](docs/phase1-design.md)),
  Measurement Catalog for `linux_block_1_stats` and `linux_block_2_misc` with the team's
  answers. Decided: each run is loaded into a database of its own, and its views live
  there.
- **Built, never yet run on a real ClickHouse:** `analysis/` — step 0, per-run views,
  per-run WrenAI data models, check C1 (design build order §13, steps 2–3).
- **Next:** run steps 2–3 on a machine with ClickHouse (docs/development.md, "Phase 1": WSL2 and a
  simulated run), then on the office box with a real run; then build step 4, the
  request runner (our LLM, Grafana writer, checks C2 and C3).
- **Waiting on the team:** catalog Q2 (block_2 sizes before or after splitting); the
  existing Grafana dashboards exported as JSON (the answer key).
- **Deferred debt** (all tracked in ADRs): append-only reload, single-node
  `run_id`/`record_id`, schema auto-migrate.

## Working rules (this repo's conventions)

- **Run it:** `python -m src <ProfileData-dir>` **from the repo root** (the package is
  `src/`; there is deliberately no `pyproject.toml` — see
  [ADR-0004](docs/decisions/0004-src-layout-and-external-config.md)). Batch: point at a
  parent of `ProfileData-*` runs; `--batch`, `--continue-on-error`.
- **Phase 1:** `python -m analysis check-run | prepare | plan | plan-file` from the repo
  root (docs/development.md, "Phase 1"). Phase 1 code goes in `analysis/`; `src/` stays
  the loader.
- **Config:** `config/config.yaml` (repo root, editable). Override with `--config PATH`
  or `CH_HOST`/`CH_PORT` env. `analysis/` reads its ClickHouse host and port from it too.
- **Test:** `python -m pytest tests/ -q` — runs the whole pipeline against an in-memory
  store, **no database needed**. The fixture rebuilds automatically when any
  `tests/sample_protos/*.proto` or `tests/make_fixture.py` changes; generate one by hand
  with `python tests/make_fixture.py <dir>`. Adding a `.proto` there needs **no test
  edit** — the unit set and record total are derived from the protos on disk.
  `tests/test_analysis.py` tests Phase 1 on a fake database; its WrenAI tests are
  skipped if `wrenai` is not installed. A sample proto that does not compile makes
  **every** test error — `python tools/catalog.py check` names it.
- **When a proto changes:** run `python tools/catalog.py check`, update the matching
  `knowledge/catalog/*.yaml` (new columns, `schema_fingerprint`), and ask the team about
  anything unclear by adding a question to that file.
- **Schema is generic:** never hardcode payload/header field names in the loader — it
  derives everything from the `.proto` by structure. Use `FieldDescriptor.is_repeated`
  (not `.label`; it exists from protobuf 6.31). See
  [ADR-0002](docs/decisions/0002-runtime-schema-from-proto.md).
- **Field shape decides the table:** scalar → column · singular message → flattened
  prefixed columns · repeated → child table · repeated-inside-repeated → one leaf table
  with the outer level's scalars denormalized onto each row · repeated scalar →
  rejected. Add a shape by extending these rules, never by special-casing a field name.
  See [ADR-0010](docs/decisions/0010-payload-field-shapes-to-tables.md).
- **Run-level config lives in `src/runconfig.py`:** a profiler sidecar file (today
  `latency_bucket_options.yaml`) is read once per run and written to its own
  `run_id`-keyed table, by `runner.run_load` — in *both* the sequential and pool paths.
  A missing file is normal; a malformed one fails the load. See
  [ADR-0011](docs/decisions/0011-run-level-config-sidecars.md).
- **Only our own LLM.** Every AI call goes to the company LLM API or, later, an
  open-source model running in the office — nothing on the LLM side from outside the
  office, and no tool that calls an AI service of its own. WrenAI contains no AI: its
  "models" are *data models* (table descriptions). See
  [docs/phase1-design.md](docs/phase1-design.md) §1. Open-source model weights may be
  downloaded to run in the office.
- **Never trust WrenAI's SQL alone:** check C1 (`analysis/checks.py`) reads every table
  in it — strict mode once let a raw table through (design §6).
- **Secrets stay out of git and output.** `tools/config.json` holds the LLM credentials
  (custom headers such as `x-dep-ticket`, `User-Id`): git-ignored — never commit it,
  and never print, log or save header values (names only). Keep internal hostnames
  (the LLM gateway URL, model path) out of committed files: the repo is on GitHub.
- **Plain English** in docs and questions, readable by a new engineer; no extra files
  (other AI tools such as Codex read this repo too).
- **Ingest is append-only** (re-loading a run duplicates rows) — see
  [ADR-0006](docs/decisions/0006-mvp-append-only-ingest.md).
- **Timestamps** stored as RFC 3339 UTC; the loader tolerates several formats and
  clamps out-of-range — see [docs/timestamp-format.md](docs/timestamp-format.md).
- **Commit/push only when asked.** Git user is **vishal9359**; remote is
  `github.com/vishal9359/Analysis-FW`. Co-author trailer as configured.
- **Live branches** — `main` (Python), `phase1` (Phase 1 work, from `main`; merge when
  the trial is done), `query` (+ database seam, ADR-0008), `go_version` (+ Go port,
  ADR-0009). They have diverged: a loader fix generally needs applying to both the
  Python and Go implementations, and a new ADR on `main` must skip the numbers the
  other branches already use (next free: 0012). `query` and `go_version` stopped in
  August 2026.

## Environment

- **Dev machines:** Windows. The first one has **no ClickHouse** — DB behaviour was
  validated with the in-memory store and a fake database. The next one runs ClickHouse
  in WSL2 (docs/development.md, "Phase 1"). Shell commands run in Git Bash: a multi-line
  `python -c` or a heredoc with an apostrophe fails — use a script file; add `-X utf8`
  when Python prints non-ASCII text.
- **Office test box:** Linux (IST timezone), Python 3.12, **ClickHouse 25.6 in Docker
  at `localhost:8123`, no password**; Grafana (container, ClickHouse plugin); WrenAI
  installed. The user pulls the repo there to run against real data, which never
  leaves it. Runs are loaded one database per run by the team's `deploy.py`. The
  company LLM gateway is OpenAI-compatible, serves only `/chat/completions` and needs
  custom headers — all in `tools/config.json`. Python `requests` needs
  `REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt` for outside HTTPS. Phase 1
  readiness is checked there with `python tools/check_phase1_env.py` (standard
  library only).

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
