# Phase 1 design — plain-language request to Grafana graph

**Status:** draft for review (2026-10-10; updated 2026-10-12 with the team's answers;
steps 2–3 built 2026-10-15). What is done and what is next: [roadmap.md](roadmap.md),
"Now — Phase 1".
**Based on:** Phase 1 Goals A, B and C in [goals-and-requirements.md](goals-and-requirements.md),
and the tool decision in [phase1-tool-research.md](phase1-tool-research.md).
**Covers:** the trial first (two block tables), then what grows for the full build.

> **Two terms to read carefully**
>
> - **"LLM" or "AI" always means *our* LLM**: the company's LLM gateway today, or an
>   open-source model running in the office later. No AI service outside the office
>   is used anywhere in this design.
> - **A WrenAI "data model" is not an AI model.** WrenAI calls its table descriptions
>   *models*: a model says what a table is, what its columns mean, and which metrics
>   are calculated from them — like a well-documented database view. **WrenAI
>   contains no AI.** It is a description format plus a SQL translator, and it is never
>   given an LLM address or key.

---

## 1. Who does what

Three labels are used throughout:

- **OURS** — code and files we write, in this repository.
- **WRENAI** — open-source software (Apache-2.0), installed on the office box. No AI inside.
- **OUR LLM** — the company LLM gateway (later, an open-source model in the office),
  called only by our code.

### 1.1 Set-up

Each run is loaded into a database of its own (decided 2026-10-12, for as long as
Profile FW's protos keep changing), so most of the set-up happens per run, by itself.

```
  ONCE, WHEN A PROTO CHANGES                                DONE BY
  ─────────────────────────────────────────────────────────────────────────────
  tests/sample_protos/*.proto  (from Profile FW)
     │  tools/catalog.py skeleton · check ───────────────── OURS
     ▼
  knowledge/catalog/*.yaml: what every column means ─────── OURS (engineers review)

  FOR EACH RUN — automatic, when a request names the run
  ─────────────────────────────────────────────────────────────────────────────
  the run's own database  (written by the loader)
     │  compare its columns with the catalog ────────────── OURS (read-only)
     ▼
  per-interval views, in the run's database ─────────────── OURS (generated SQL)
  WrenAI data models for this run ───────────────────────── OURS (generated files)
```

### 1.2 Each request

```
  STEP                                                      DONE BY
  ─────────────────────────────────────────────────────────────────────────────
  request.yaml  (run + plain-language request)              you
     │
     ▼
  0 find the run's database; compare it with the catalog ── OURS (read-only)
     ▼
  1 understand the request ──────────────────────────────── OURS + OUR LLM
     ▼
  2 show the meaning in plain words, ask yes / no ───────── OURS (you answer)
     ▼
  3 write a query on the WrenAI data models ─────────────── OURS + OUR LLM
     ▼
  4 turn it into ClickHouse SQL, without running it ─────── WRENAI (dry_plan)
    refuse anything not described, or not read-only
    then check it (C1) ──────────────────────────────────── OURS
     ▼
  5 refresh the run's views; run; check (C2) ────────────── OURS
     ▼
  6 draw it in Grafana, in the run's folder ─────────────── OURS (Grafana API)
     ▼
  7 explain what the graph shows; check it (C3) ─────────── OURS + OUR LLM
  ─────────────────────────────────────────────────────────────────────────────
  OUR LLM = the company LLM gateway (later: an open-source model in the office).
  WRENAI's only job is step 4. It contains no AI and calls none.
```

### 1.3 In one table

| Capability | Done by | Notes |
|---|---|---|
| What every column means | **OURS** | The catalog, reviewed by engineers |
| Finding a run's database; checking its columns against the catalog | **OURS** | Read-only queries (section 5.1) |
| Turning running totals into per-interval changes | **OURS** | Generated ClickHouse views, one set per run |
| A file format to describe data: data models, columns, metrics, joins | **WRENAI** | We *generate* these files from our catalog |
| Turning a query on the data models into real ClickHouse SQL | **WRENAI** | `dry-plan`, called from our code; no AI involved |
| Rejecting anything but a read-only query; in "strict mode", also any table that is not described, and table functions that read data from elsewhere | **WRENAI** | We switch strict mode on. It has a gap (section 6), so our check C1 checks every table of the final SQL |
| Understanding the request, writing the query, explaining the result | **OURS + OUR LLM** | WrenAI has no AI |
| Asking you yes or no | **OURS** | Command line now; UI later |
| Checks before and after running | **OURS** | C1, C2, C3 |
| Running the query | **OURS** | The exact SQL that was checked |
| Drawing in Grafana | **OURS** | Grafana HTTP API |
| Records, test set, scoring | **OURS** | |

**WrenAI features we do not use** — none is needed, and some would reach outside the
office:

| Feature | What it is | Why not |
|---|---|---|
| `wren memory` | a search index over descriptions and examples | Not needed: our catalog is small enough to send whole. Allowed if needed later — its embedding model is open source and runs in the office, and downloading open-source model weights is allowed (decided 2026-10-12). |
| `wren ask` | prints a ready-made prompt for an AI agent | It calls nothing; our program writes its own prompts. |
| `wren serve` (MCP server), agent skills | let other AI tools, such as coding assistants, use WrenAI | Our program calls WrenAI directly. |
| `wren cloud` | Wren's paid hosted service | Outside the office. |
| `wren genbi` | an AI coding agent writes a dashboard app; `deploy` uploads it to Vercel or Cloudflare | Later, only with the team's in-office coding-agent API — and never `deploy`. |

## 2. Design rules

1. **Only our LLM.** Every AI call goes from our code to the company LLM gateway — or,
   later, an open-source model running in the office. No outside AI service, no AI
   inside any tool we adopt, and no tool feature that reaches outside the office.
2. **Code calculates, the AI explains.** The LLM is used in steps 1, 3 and 7 only.
3. **The AI never sees raw running totals.** The WrenAI data models describe the
   per-interval views, not the raw tables, so "adding up a running total" is not just
   forbidden — it is impossible.
4. **Meanings live in our catalog** (`knowledge/catalog/`). The views and the WrenAI
   files are generated from it, never edited by hand (requirement B6).
5. **Nothing runs before you say yes** (A4). The meaning the system picked is shown in
   plain words first.
6. **Grafana panels are built by our code** from a few templates. The AI only proposes
   the chart type, and our code checks it against the shape of the result.
7. **Every request leaves a record**: the request, the meaning, the SQL, a result
   summary, the link.
8. **No tool calling needed.** Each LLM step is one request with JSON out. Tool calling
   works on the gateway (readiness check) and can be used later, when the catalog is
   too big to send in full.

## 3. The parts

| # | Part | Built by | Lives in | Job |
|---|---|---|---|---|
| 1 | **Catalog** | OURS (drafted by AI, reviewed by engineers) | `knowledge/catalog/` | What every column means; metric formulas; open questions |
| 2 | **Per-interval views** | OURS (generated) | each run's own database | Turn running totals into the change per sample |
| 3 | **WrenAI data models** | OURS (generated per run, in WrenAI's format) | kept with each request's record, `out/<run>/` | Data models over the run's views, metric definitions, rules |
| 4 | **WrenAI planner** | WRENAI (installed, no AI) | Python library `wrenai` | Step 4: query on data models → ClickHouse SQL, not yet run |
| 5 | **Request runner** | OURS | `analysis/` (`python -m analysis`) | Finds the run (step 0), then the seven steps; the only code that calls our LLM |
| 6 | **Grafana writer** | OURS | `analysis/grafana.py` | Folder per run, dashboard per request, panel from a template |
| 7 | **Checks** | OURS | `analysis/checks.py` | C1 before running, C2 after running, C3 on the explanation |
| 8 | **Test set** | OURS (team + AI) | `tests/phase1/` | Requests with known right answers, and a score |

## 4. The catalog — knowledge comes first

```
 tests/sample_protos/<table>.proto
        │  tools/catalog.py skeleton      (column names from the loader's own code)
        ▼
 knowledge/catalog/<table>.yaml  ──review──►  status: confirmed
        │        ▲
        │        └── tools/catalog.py check   (names match? proto changed? formulas valid?)
        │ our generators, for each run (step 0)
        ├──────────────────────────────┐
        ▼                              ▼
 the run's views (SQL)          the run's WrenAI data models + metrics
```

**Now:** written for the two block tables, with the team's answers of 2026-10-12 — how
to review them is in [knowledge/README.md](../knowledge/README.md).

| File | Columns | State |
|---|---|---|
| `_common.yaml` | 9 shared columns | draft |
| `linux_block_1_stats.yaml` | 18 | 15 draft · 3 confirmed · all 3 questions answered · 10 metrics (IOPS, MiB/s, average latency, average size, busy %, queue depth) |
| `linux_block_2_misc.yaml` | 180 (18 single + 10 groups) | 180 draft · 16 of 17 questions answered · 8 metrics |

The latency histograms' ranges are not in the rows. The profiler writes the thresholds
to `latency_bucket_options.yaml`, and the loader stores them in the run's
`latency_buckets` table, one row per bucket, with a label such as `100 µs - 1 ms`
([ADR-0011](decisions/0011-run-level-config-sidecars.md)). Phase 1 can label the
histograms once that table is described too ([roadmap.md](roadmap.md), "Next"). Still
open in `block_2`: Q2 (the team is checking).

## 5. Per-interval views — removing the biggest trap (OURS)

Most columns are running totals: a sum of their samples is hundreds of times too
large (the readiness check measured this on a real run). So, for each table, a view
turns each running total into the change since the previous sample. Our generator
writes it from the catalog:

| Catalog says | The view gets |
|---|---|
| `kind: counter` | `<column>_delta` — the change since the previous sample. A drop (counter reset) gives "no value", never a negative. The raw total is **not** in the view. |
| `kind: gauge` | the column as is (`in_flight_io_count`) |
| `kind: setting` | the column as is (a value chosen when the profiler was started; the block tables have none today) |
| `label`, `timestamp` | as is (`run_id`, `hostname`, `device`, `ts`) |
| always | `interval_s` — seconds since the previous sample |
| `counts_since: boot` | the first sample's change is "no value" (the total since boot is not activity in this run) |
| `counts_since: profiler start` | the first sample's change is the value itself (assumed — the eBPF maps start at zero) |

**One set of views per run.** Each run has its own database, so its views are made
for it — in the run's own database, named `<table>_interval` (decided 2026-10-15).
Our code makes them in step 5 of each request (or with `python -m analysis prepare`),
from the current catalog and only with the columns that run has (section 5.1). It
replaces an older version, so a view always follows the catalog; a view is only a
definition, so this is instant. The core of the generated SQL, for one column
(`run_db` stands for the run's database; `analysis/views.py` writes the full one):

```sql
CREATE OR REPLACE VIEW run_db.linux_block_1_stats_interval AS
SELECT run_id, hostname, device, ts,
       if(rn = 1, NULL, dateDiff('second', prev_ts, ts))          AS interval_s,
       if(rn = 1 OR cur < prev, NULL, cur - prev)                 AS read_io_count_delta,
       in_flight_io_count
FROM (SELECT *, toInt64(read_io_count) AS cur,
             toInt64(lagInFrame(read_io_count) OVER w) AS prev,
             lagInFrame(ts) OVER w AS prev_ts,
             row_number() OVER wn AS rn
      FROM run_db.linux_block_1_stats
      WINDOW w  AS (PARTITION BY run_id, hostname, device ORDER BY ts
                    ROWS BETWEEN 1 PRECEDING AND CURRENT ROW),
             wn AS (PARTITION BY run_id, hostname, device ORDER BY ts))
```

After this, "total over a run" is a plain sum of `_delta`, and "per second" is
`_delta / interval_s`. The same window query already ran on a real run in the
readiness check: the sum of the changes equalled last minus first.

Views are computed at query time. If that is too slow later, the same SQL can fill a
table once, at load time.

### 5.1 Runs collected with different protos

Profile FW's protos still change, so two runs can have different columns. Step 0
compares the run's tables with the catalog, column by column:

| In the run | In the catalog | Result |
|---|---|---|
| yes | yes | Usable: it goes into the run's views and data models |
| no | yes | "Not collected in this run" — said in the answer (A11) |
| yes | no | Not usable until it is described: "not described yet" |
| yes, but another type | yes | Stop: the catalog was written for another proto version |

So an older run with fewer columns still works — only what it has can be asked for.
Runs loaded before the October 2026 renames (the shared `profile_fw` database, old
column names such as `read_ios`) are not covered: their names are not in the catalog.

*Later:* if the loader stored each table's proto fingerprint (for example as the
table's comment), step 0 could also tell exactly which proto version a run used — a
column can change meaning without changing its name.

## 6. What WrenAI does for us — and what it does not

**How we use it:** as a Python library on the office box. Our code calls one function,
`dry_plan` — the same as the `wren dry-plan` command. For this it needs no database
connection and no network.

**What it does (step 4 only):**

- **Reads the data models we generate for the requested run:** one per view in the
  run's database, listing only the columns that run has, each with its catalog meaning
  and unit; the catalog's metrics (`read_iops`, `avg_read_latency_ms`, …) as calculated
  fields; and later, joins between tables. The data model names are the same in every
  run (`linux_block_1_stats`), so the LLM's prompts and examples never change.
- **Plans the query.** Our LLM writes a query on the data models, for example
  `SELECT ts, read_iops, write_iops FROM linux_block_1_stats WHERE run_id = '…'`.
  `dry_plan` expands it into real ClickHouse SQL over the views, **without running
  it**.
- **Refuses what is not allowed.** Anything but a read-only query is always refused.
  With strict mode on, so is a table that is not described, and a table function that
  reads data from somewhere else (ClickHouse's `url()`, `s3()`, `file()` …). A column
  that does not exist fails here too — with one gap that WrenAI's own code documents:
  inside a query that defines its own `WITH` blocks, a column written without its
  table name can slip through and be silently replaced. So our rules tell the LLM to
  write `table.column` and no `WITH` blocks, and C1 refuses a query that does.
- **A gap found by testing (2026-10-15):** strict mode accepts a table name with a
  database in front, when the name matches a data model. `SELECT read_io_count FROM
  run_db.linux_block_1_stats` was planned into SQL that reads the **raw table** — the
  running totals the views exist to hide. So C1 does not trust WrenAI: it reads every
  table in WrenAI's final SQL and refuses anything but the run's own views (section 9).
  The LLM's query must also name data models alone, without a database.
- **It rewrites some ClickHouse functions** on the way through: `toStartOfMinute(ts)`
  came out as `dateTrunc('MINUTE', ts)`. Equivalent: on ClickHouse 25.6 both gave the
  same per-minute totals (2026-10-16).

**Why that is worth having:** a metric's formula is written once, in the catalog, and
the LLM asks for `read_iops` by name instead of writing the formula each time — so it
cannot get the formula wrong. Undescribed tables are refused before anything runs. And
as more tables are added, it handles the joins between them.

**What it does not do:**

- **No AI.** It contains no LLM, makes no LLM calls, and is never given an LLM address
  or key. The LLM is called only by our request runner.
- It does not read our catalog — our generator writes its files.
- It does not run our checks, ask you yes or no, draw graphs, or explain anything.
- The features listed in section 1.3 as not used stay unused.

**Checked in WrenAI's source code (2026-10-10; planner 0.8.1 on 2026-10-15)** — `wrenai`
0.15.0 and its compiled SQL planner `wren-core-py` 0.8.0 and 0.8.1, all Apache-2.0:

- **No LLM client and no usage statistics** (telemetry) anywhere in either package.
- **Network code exists only in the features we do not use:** `wren cloud` (Wren's
  hosted service), GenBI's deploy to Vercel or Cloudflare, `wren memory`'s embedding
  model download from Hugging Face, and a settings web page on `localhost`.
- **The compiled planner contains no HTTP client at all** — none among its 277 bundled
  libraries.

**Still to confirm on the office box**, with the version actually installed: run one
`dry_plan` under `strace -f -e trace=connect` — it should open no network connection
of any kind.

**Tried and dropped (2026-10-15): defining a data model by SQL (`ref_sql`)** instead of
a view, which would have created nothing in ClickHouse. WrenAI planned it, but rewrote
our SQL on the way: `dateDiff('second', prev_ts, ts)` became `DATE_DIFF(TS, prev_ts,
'second')` — arguments reordered and `ts` upper-cased, which ClickHouse would reject or
misread. So the per-run views stay: WrenAI only refers to them by name and never sees
their SQL.

**WrenAI is beta (0.15)**, so the exact file format is confirmed while building. If the
trial shows it adds too little, the decision at the end includes switching to Cube
Core, or doing this one step ourselves.

## 7. One request, step by step

```
request.yaml
    │
    ▼
0 FIND THE RUN ...... OURS ──► the run's database, and which described columns it has
    │   read-only. No such run, or a column of another type: stop, with the reason
    ▼
1 UNDERSTAND ........ OURS + OUR LLM ──► the meaning, as JSON
    │   sent to our LLM: the request, the run, a summary of what this run has, the rules
    ▼
2 CONFIRM ........... OURS — you answer ── no ──► rephrase ──► back to 1
    │ yes
    ▼
3 WRITE QUERY ....... OURS + OUR LLM ──► a query on the WrenAI data models
    │   sent to our LLM: the meaning, the data models' descriptions, confirmed examples
    ▼
4 PLAN .............. WRENAI ──► real ClickHouse SQL (not yet run)
  CHECK C1 .......... OURS
    │   any failure: the error goes back to step 3 (at most 2 retries), then stop
    │   with the reason
    ▼
5 RUN + CHECK C2 .... OURS ──► refresh the run's views, run, check the result
    ▼
6 DRAW .............. OURS ──► Grafana folder <run> → dashboard → panel → link
    ▼
7 EXPLAIN + CHECK C3  OURS + OUR LLM ──► printed, and stored on the panel
    ▼
record saved: out/<run>/<time>-<request>.json      OURS
```

**The request file:**

```yaml
run: ProfileData-fio-20261008-101500
request: Show read and write IOPS over time at the block layer.
```

The request names only the run. Step 0 finds the database that holds it by asking
ClickHouse which database has rows for that run, so it works whatever the database is
called. If two databases hold the same run (loaded twice), it stops and asks which;
`--db <name>` answers that (2026-10-20). A run that is not found is reported together
with the runs that are loaded.

**What step 2 shows** — the meaning, in plain words, before anything runs:

```
I will show:  read IOPS and write IOPS, per second
  from:       block layer - completed requests (linux_block_1_stats)
  run:        ProfileData-fio-20261008-101500, whole run (10:15:00 - 10:40:12)
  as:         a time-series chart
  assumed:    "block layer" means completed requests, not submitted bios
Proceed? [y/n]
```

**Requests the catalog cannot answer yet** — NVMe, syscall, SSD and filesystem data —
get a clear "this data is not described yet", or "not collected", and the rest of the
request is still answered (A11). During the trial only the two block tables are
described, so three of the five example requests will show this behaviour.

## 8. Grafana output (OURS)

- **One folder per run**, created when first needed. **One dashboard per request**,
  titled with the request.
- **The dashboard's time range is set to the data's time range.** Runs are loaded after
  they finish, so Grafana's default "last 6 hours" would show an empty graph.
- **The panel's query is the exact checked SQL**, visible under Edit (C4). The
  explanation goes into the panel description (the ⓘ icon) and is printed in the
  terminal.
- **The panel type comes from a template**, chosen by the shape of the result:

| Result | Panel |
|---|---|
| a time column and numbers | time series |
| one number | stat |
| categories and numbers | bar chart |
| size or latency buckets | bar chart |
| anything else | table |

## 9. The checks (OURS)

| Check | When | What it guarantees |
|---|---|---|
| **C1** | after WrenAI's plan, before running | Read-only. Reads only the requested run's per-interval views (`<table>_interval`), never the raw tables, and no other database — checked table by table on WrenAI's final SQL, because strict mode alone let a raw table through (section 6). No table functions (`url`, `s3`, `remote`, `file` …), so a query cannot read from, or send to, anywhere else. The LLM's query used `table.column` and no `WITH` blocks (section 6). Filters to the requested run. Time-bounded. |
| **C2** | after running | An empty result is explained (wrong run, no activity, data not collected), not drawn as a blank graph. No negative rates. Gaps are mentioned. |
| **C3** | on the explanation | Every number in the explanation appears in the result. Only the queried measurements are mentioned. |

## 10. Test set and scoring (OURS)

- `tests/phase1/requests.yaml` holds requests with known right answers. The answer
  key comes from the existing Grafana panels: their SQL, run on the same run, gives
  the expected numbers. Those panels use the old column names and the shared
  `profile_fw` database, so their SQL is adapted once to the new names and to the
  run's database.
- **Trial: 10 requests**, block layer only. They include one that must be refused or
  partly answered (filesystem latency) and one that can be read two ways.
- A scoring script runs each request without the yes/no prompt, records the meaning
  it picked, and compares numbers with the answer key.
- **Acceptance (full Phase 1):** 30 requests, pass mark 27, all five example requests,
  no wrong graph shown as right (C5, C6).

## 11. Repository layout

```
analysis/            OURS — Phase 1 code: python -m analysis ...
  __main__.py        command line (exists: check-run, prepare, plan, plan-file; the
                     request file comes with step 4)
  catalog.py         read knowledge/catalog                           (exists)
  runs.py            step 0: find a run's database; compare its columns with the catalog
                                                                      (exists)
  clickhouse.py      ClickHouse access: read-only, except CREATE VIEW (exists)
  views.py           generate the per-interval view SQL for a run     (exists)
  wren_project.py    generate the WrenAI data models for a run; dry_plan   (exists)
  llm.py             the only place that calls our LLM (as tools/llm_config.py)
  steps.py           understand · write query · explain
  checks.py          C1 · C2 · C3                       (exists: C1 on the SQL)
  grafana.py         folder · dashboard · panel templates
knowledge/           OURS — reviewed by engineers
  catalog/           what every column means                          (exists)
  rules.md           rules sent to our LLM                            (later)
  examples.yaml      confirmed request → query pairs (B5)             (later)
requests/            example request files
tests/test_analysis.py   tests of analysis/, on simulated runs - no ClickHouse  (exists)
tests/phase1/        trial test set and scoring
tools/               readiness check, LLM check, catalog check        (exist)
out/                 one record per request, with the WrenAI data models generated
                     for it (git-ignored; generated files are never edited by hand)
```

Switching later to an open-source model running in the office changes only the LLM
settings (address, model name) — nothing else, because `analysis/llm.py` is the only
code that talks to the LLM.

## 12. Before the trial can run

1. ~~Confirm the `block_2` proto fix.~~ **Done (2026-10-12):** the flags message is
   called `BiOpFlags`; our copy uses that name for the message and both fields.
2. ~~Decide how runs made with the new protos are loaded.~~ **Decided (2026-10-12):**
   each run is loaded into a database of its own, until Profile FW's protos are final.
   Old and new column names then never meet in one table. The team's `deploy.py`
   (outside this repo) picks the database: it writes a temporary config with the
   database name and runs `python -m src <run> --config <file>`, so the loader needs
   no change. The run's views go into the same database (decided 2026-10-15).
3. ~~**Load one fresh FIO run** captured with the new protos, into its own database, on
   the office box.~~ **Done:** a direct-I/O random-mix FIO run with latency buckets,
   loaded on 15 Oct; steps 2–3 ran on it on 2026-10-20. The data stays there; building
   and testing here needs only the output of the commands run there.
4. **Answer the catalog questions.** `block_1`: all answered. `block_2`: 16 of 17
   answered; Q2 is being checked by the team.
5. **Export the existing Grafana dashboards as JSON** — the answer key (section 10).

## 13. Build order

| Step | Who | Output | Done when |
|---|---|---|---|
| 0 | team | The prerequisites above | — |
| 1 | team | Catalog reviewed | `python tools/catalog.py check` passes; no `todo` left in the columns the trial uses |
| 2 | AI (OURS) | Step 0, and the per-interval views generated for a run | On a real run, the sum of the changes equals last minus first; an older run's missing columns are reported. *Built 2026-10-15; passed with a simulated run on a real ClickHouse 2026-10-16, and on a real run on the office box 2026-10-20* |
| 3 | AI (OURS) | WrenAI data models generated for a run; WrenAI plans a query; C1 on its SQL | A hand-written query on the data models plans and runs; `strace` shows WrenAI opens no network connection. *Built 2026-10-15; with a simulated run on a real ClickHouse a query planned and ran, and matched the raw table (2026-10-16); queries planned and ran on a real run on the office box (2026-10-20); `ref_sql` tried and dropped (section 6); `strace` on the office box pending* |
| 4 | AI (OURS) | The request runner, steps 1–7 | One request end to end, from request file to Grafana panel |
| 5 | team + AI | 10 trial requests with answers, and scoring | A score report |
| 6 | together | Decision: keep WrenAI, switch to Cube Core, or do step 4 ourselves | — |

**Then:** extend the catalog to the NVMe, syscall and SSD tables (`tools/catalog.py
skeleton` starts each file); grow to the 30-request acceptance set; try GenBI with the
team's in-office coding-agent API.

### Checking steps 2 and 3 on a real ClickHouse

First on a simulated run, on any machine with ClickHouse (README, "Phase 1": ClickHouse
in WSL2, `tests/make_fixture.py`). Then on the office box, with a real run. From the
repo root, after `pip install -r requirements.txt`, with `<run>` the run folder's name:

```
python -m analysis check-run <run>     # step 0: database, usable / missing columns
python -m analysis prepare <run>       # makes the views; "sum of changes = last minus first"
python -m analysis plan <run> "SELECT linux_block_1_stats.ts, linux_block_1_stats.read_iops
    FROM linux_block_1_stats WHERE linux_block_1_stats.run_id = '<run>'" --execute
strace -f -e trace=connect python -m analysis plan-file out/<run>/data_models.json \
    "SELECT linux_block_1_stats.ts FROM linux_block_1_stats"   # expect no connect() lines
```

Then compare `--execute`'s numbers with the existing Grafana panel for the same run.

## 14. Open design questions

1. **Views or tables:** start with views; switch to tables filled at load time only if
   views are too slow on long runs.
2. **Settings:** the runner needs the same LLM, ClickHouse and Grafana settings as
   `tools/config.json`. Reuse that file, or move it to `config/` when the package is
   built?
