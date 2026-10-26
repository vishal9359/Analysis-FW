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
analysis/                     ← Phase 1 code: python -m analysis ask <request.yaml> (the
                                whole request: runner.py, llm.py, steps.py, grafana.py,
                                checks.py) + score (score.py) + check-run | prepare |
                                plan | plan-file
requests/                     ← example request files (the five of the requirements, +1)
tests/phase1/requests.yaml    ← the trial test set: requests with reference queries
tools/                        ← office-box checks (check_phase1_env.py, check_llm.py) +
                                catalog.py; config.json = LLM + Grafana credentials,
                                git-ignored
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
├── phase1-runbook.md         ← running Phase 1 on the office machines: setup, every
│                             command, what each does inside, the errors seen so far
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
| Running Phase 1 on the office machines (setup, commands, what each does, errors) | [docs/phase1-runbook.md](docs/phase1-runbook.md) |
| Setting up a dev machine / trying Phase 1 on a simulated run | [docs/development.md](docs/development.md), "Phase 1" (ClickHouse in WSL2) |
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
"Now — Phase 1". In short (2026-10-22):

- **Branch:** Phase 1 work is on **`phase1`**, rebased onto `main` on 2026-10-20 (it has
  `main`'s `latency_buckets` loader); not yet merged. All 102 tests pass (46 loader,
  56 Phase 1); `python tools/catalog.py check` passes.
- **Done:** Module 1 loader, including run-level sidecars (`latency_buckets`,
  ADR-0011). Phase 1 requirements, tool choice (WrenAI trial, Grafana draws), office-box
  readiness check, design ([docs/phase1-design.md](docs/phase1-design.md)), Measurement
  Catalog for `linux_block_1_stats`, `linux_block_2_misc` and `latency_buckets`, every
  question answered. Decided: each run is loaded into a database of its own, and its
  views live there.
- **Steps 0–3 run on one real run** (office box, 2026-10-20): step 0, per-run views,
  WrenAI data models, check C1. That run corrected the catalog: the latency thresholds
  are rows of `latency_buckets`, not columns of `block_2`; `device` holds `nvme1n1`.
- **Built 2026-10-21, run on a simulated run only:** the whole request for the block
  layer — `python -m analysis ask` (our LLM's steps 1, 3, 7; checks C2, C3; the Grafana
  writer; a record per request, and an LLM log with every request and response in
  full) and labelled latency histograms (a `<table>_histograms` view per run). Run end to end on ClickHouse 25.6 and Grafana
  13.2.4 in WSL2 with a stand-in for our LLM; never yet with our LLM.
- **First run with our LLM (2026-10-22)**, from the office Windows machine: some right,
  some not understood, some wrong numbers — our LLM writing SQL is the weak point
  (roadmap, "First run with our LLM"). Built then: `out/<run>/summary.txt`, the test set
  `tests/phase1/requests.yaml` (17 requests with reference queries) and
  `python -m analysis score`.
- **Next:** the user checks the reference queries and scores the real run (the
  baseline); then labelled-row views for the flag and size groups, and the form our LLM
  fills while code writes the SQL — each scored against the baseline. The real run also
  has filesystem, platform and `block_3` tables, not catalogued yet.
- **Decided 2026-10-20:** build all of Phase 1 for the block layer first; the user
  reviews it before other layers are added.
- **Waiting on the team:** the existing Grafana dashboards exported as JSON (the answer
  key); a Grafana service-account token in `tools/config.json`.
- **Deferred debt** (all tracked in ADRs): append-only reload, single-node
  `run_id`/`record_id`, schema auto-migrate.

## Working rules (this repo's conventions)

- **Run it:** `python -m src <ProfileData-dir>` **from the repo root** (the package is
  `src/`; there is deliberately no `pyproject.toml` — see
  [ADR-0004](docs/decisions/0004-src-layout-and-external-config.md)). Batch: point at a
  parent of `ProfileData-*` runs; `--batch`, `--continue-on-error`.
- **Phase 1:** `python -m analysis ask <request.yaml>`, and `check-run | prepare | plan |
  plan-file`, from the repo root (docs/development.md, "Phase 1"). Phase 1 code goes in
  `analysis/`; `src/` stays the loader. `views.model_specs` is the one list of a run's
  data models: the WrenAI files, the prompts and the allowed names come from it. Only
  `analysis/llm.py` calls our LLM.
- **Config:** `config/config.yaml` (repo root, editable). Override with `--config PATH`
  or `CH_HOST`/`CH_PORT` env. `analysis/` reads only the host and port from it: it finds
  a run's database by the run's name (the folder's name; a path works too), or `--db`.
- **Test:** `python -m pytest tests/ -q` — runs the whole pipeline against an in-memory
  store, **no database needed**. The fixture rebuilds automatically when any
  `tests/sample_protos/*.proto` or `tests/make_fixture.py` changes; generate one by hand
  with `python tests/make_fixture.py <dir>`. Adding a `.proto` there needs **no test
  edit** — the unit set and record total are derived from the protos on disk.
  `tests/test_analysis.py` tests Phase 1 on a fake database, `tests/test_runner.py` the
  request runner with a scripted LLM and a fake Grafana; their WrenAI tests are skipped
  if `wrenai` is not installed. A sample proto that does not compile makes **every**
  test error — `python tools/catalog.py check` names it.
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
  (custom headers such as `x-dep-ticket`, `User-Id`) and the Grafana token:
  git-ignored — never commit it, and never print, log or save header values or the
  token (names only); a request's record keeps no headers. Keep internal hostnames (the
  LLM gateway URL, model path) out of committed files, and Grafana dashboard exports
  too: the repo is on GitHub.
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
  in WSL2 (docs/development.md, "Phase 1"), and may run a local Grafana for trying
  panels. Our LLM is not reachable from a dev machine: `ask` is tried there with a
  stand-in. Shell commands run in Git Bash: a multi-line `python -c` or a heredoc with
  an apostrophe fails — use a script file, or the edit tools for code; add `-X utf8`
  when Python prints non-ASCII text.
- **Office test box:** Linux (IST timezone), Python 3.12, **ClickHouse 26.6 in Docker
  (26.6.1.1193, seen 2026-10-22) at `localhost:8123`, no password**; Grafana
  (container, ClickHouse plugin); WrenAI installed. Runs are loaded there, one database
  per run, by the team's `deploy.py`. ClickHouse's `default` user has no password and
  may log in only from the box itself (the Docker image's default), so another machine
  gets "Authentication failed" until its address is allowed.
- **Office Windows machine:** where the user runs Phase 1 (`python -m analysis`, the
  readiness check) since 2026-10-22, because the company LLM API is reached from there.
  It uses the box's ClickHouse and Grafana over the office network: `config/config.yaml`
  (for `python -m analysis`) and the `"clickhouse"` block of `tools/config.json` (for the
  check, which falls back to `config/config.yaml`) name the box's address, and the box
  lets this machine's address in. Query results reach this machine. The LLM gateway is
  OpenAI-compatible, serves only `/chat/completions` and needs custom headers — all in
  `tools/config.json`. Requests to localhost bypass any proxy; Python `requests` needs
  `REQUESTS_CA_BUNDLE` pointed at the system certificates for outside HTTPS on the box.
  Phase 1 readiness: `python tools/check_phase1_env.py` (standard library only).

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
