# Phase 1 design — plain-language request to Grafana graph

**Status:** draft for review (2026-10-10; updated 2026-10-12 with the team's answers;
steps 2–3 built 2026-10-15; step 4, the request runner, and the latency histograms built
2026-10-21, for the block layer). What is done and what is next: [roadmap.md](roadmap.md),
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
  7 explain what the graph shows; check it (C3) ─────────── OURS + OUR LLM
     ▼
  6 draw it in Grafana, in the run's folder ─────────────── OURS (Grafana API)
  ─────────────────────────────────────────────────────────────────────────────
  OUR LLM = the company LLM gateway (later: an open-source model in the office).
  WRENAI's only job is step 4. It contains no AI and calls none.
  Step 7 runs before step 6, so the panel carries the explanation.
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

**Now:** written for the block layer, with the team's answers of 2026-10-12 to
2026-10-20 — how to review them is in [knowledge/README.md](../knowledge/README.md).

| File | Columns | State |
|---|---|---|
| `_common.yaml` | 9 shared columns | draft |
| `linux_block_1_stats.yaml` | 18 | 15 draft · 3 confirmed · 4 of 4 questions answered · 10 metrics (IOPS, MiB/s, average latency, average size, busy %, queue depth) |
| `linux_block_2_misc.yaml` | 180 (18 single + 10 groups) | 180 draft · 17 of 17 questions answered · 8 metrics · 4 latency histograms |
| `latency_buckets.yaml` | 3 | draft · a run-level table: one row per bucket, no `ts` |

The latency histograms' ranges are not in the rows. The profiler writes the thresholds
to `latency_bucket_options.yaml`, and the loader stores them in the run's
`latency_buckets` table, one row per bucket, with a label such as `100 µs - 1 ms`
([ADR-0011](decisions/0011-run-level-config-sidecars.md)). In the catalog, each of
`block_2`'s four latency groups says `labels: latency_buckets`: its 17 columns are
buckets in position order, which that table labels (section 5.2).

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
| `level: run` (a table, not a column) | no view: the table has no `ts` and is read as it is (`latency_buckets`) |

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

### 5.2 Latency histograms — buckets with their ranges

`block_2` counts each completed read and write by its latency in four histograms
(device and driver layer, read and write), each 17 positional bucket columns. Which
range a bucket covers is chosen per run (`-lat-buckets`), so the columns alone cannot be
read: the ranges are the rows of the run's `latency_buckets` table. For a table whose
catalog groups say `labels: latency_buckets`, our generator makes a second view over its
per-interval view, `<table>_histograms` (built 2026-10-21): one row per sample,
histogram and bucket.

| Column | What it holds |
|---|---|
| `run_id`, `hostname`, `ts`, `interval_s` | as in the per-interval view |
| `histogram` | `read device latency`, `read driver latency`, `write device latency` or `write driver latency` — the catalog group's name |
| `bucket_index` | the bucket's position, 0 the fastest; charts order by it |
| `bucket` | the range, ready for a legend: `< 1 µs`, `1-10 µs`, `100 µs - 1 ms`, `> 1 s` |
| `threshold_us` | the threshold that opens the bucket; 0 for both open ends |
| `io_count_delta` | I/Os counted in the bucket since the previous sample |

Buckets are matched by position (ADR-0011). Take the real run's 15 thresholds, from
1 µs to 1 s: `latency_buckets` has 16 rows, `< 1 µs`, `1-10 µs`, …, `500 ms - 1 s` and
`> 1 s`. Column 0 (`…_lh_opt1`) is `< 1 µs`, column 1 (`…_bw_opt1_and_opt2`) is
`1-10 µs`, and so on up to column 14, `500 ms - 1 s`. The overflow column
(`…_latency_overflow`) is `> 1 s`: every I/O above the last threshold. That leaves
column 15 (`…_bw_opt15_and_opt16`), the range between a 15th and a 16th threshold that
was never set: it is unused and stays 0 (catalog Q16), so the view leaves it out.
`python -m analysis prepare` checks that no I/O is lost this way: per histogram, the
I/Os in the view must equal the I/Os in all 17 columns, or it prints a warning that
names the unused columns. A simulated run always warns, because `tests/make_fixture.py`
fills every column.

What our LLM is given:

- **The histogram view is a data model of its own**, `<table>_histograms`. Its
  description lists the run's own buckets (`0: < 1 µs, 1: 1-10 µs, …`), so "reads
  slower than 1 ms" can become `bucket_index >= k`.
- **The 68 bucket columns are not in the `block_2` data model**: unlabelled, they are
  counts of unknown ranges. They stay in the per-interval view, which the histogram
  view reads.
- **A run profiled without `-lat-buckets`** has no `latency_buckets` table, so it gets
  no histogram model, and the `block_2` model says why.
- **`latency_buckets` is a data model too**, with no time column, for "which latency
  buckets did this run use?".

A distribution, such as "show the read latency distribution", is then one plain query:
filter to one histogram, sum `io_count_delta` grouped by `bucket_index` and `bucket`,
order by `bucket_index`. Grafana draws it as a bar chart with the ranges along the
bottom. All of this ran on the simulated run, on ClickHouse 25.6 and Grafana 13.2.4
(2026-10-21): a distribution, slow reads over time, and one line per bucket over time.

## 6. What WrenAI does for us — and what it does not

**How we use it:** as a Python library on the office box. Our code calls one function,
`dry_plan` — the same as the `wren dry-plan` command. For this it needs no database
connection and no network.

**What it does (step 4 only):**

- **Reads the data models we generate for the requested run:** one per view in the
  run's database — the per-interval views and the histogram views (section 5.2) — and
  one per run-level table, read as it is (`latency_buckets`). Each lists only the
  columns that run has, each with its catalog meaning and unit; the catalog's metrics
  (`read_iops`, `avg_read_latency_ms`, …) are calculated fields; joins between tables
  come later. The data model names are the same in every run (`linux_block_1_stats`,
  `linux_block_2_misc_histograms`), so the LLM's prompts and examples never change.
  `views.model_specs` is the one list they come from: the WrenAI files, the prompts and
  the names a query may use are all made from it.
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
  table in WrenAI's final SQL and refuses anything the run's data models do not read
  (section 9).
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

**Confirmed on the office box (2026-10-20)**, with the version installed there: a
`dry_plan` run under `strace -f -e trace=connect` (`python -m analysis plan-file`)
opened no network connection.

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
7 EXPLAIN + CHECK C3  OURS + OUR LLM ──► printed, and stored on the panel
    ▼
6 DRAW .............. OURS ──► Grafana folder <run> → dashboard → panel → link
    ▼
record saved: out/<run>/<time>-<request>.json      OURS
```

**The request file:**

```yaml
run: ProfileData-fio-20261008-101500
request: Show read and write IOPS over time at the block layer.
# db: <database>     optional: the run's database, when it should not be searched for
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

### 7.1 As built (2026-10-21)

`python -m analysis ask <request.yaml>` runs every step (`analysis/runner.py`).
`--run` and `--db` replace the file's run and database, so one request file serves any
run. Add `--yes` to take the meaning as confirmed without asking (for scoring a test
set), and `--no-grafana` to stop after the explanation. The LLM and Grafana settings come from
`tools/config.json` (`--settings` names another file); the ClickHouse host and port
from `config/config.yaml`.

| Step | What our LLM is sent | What comes back, and how code checks it |
|---|---|---|
| 1 | The request; the run's first and last sample; every data model of the run, with its metrics and columns (columns that share a description, such as a group of buckets, on one line); what the run has that is not described | JSON: the measures (data model + exact name), the time, the chart, the assumptions, the parts not available. Every measure must be a metric or column of this run's data models. The time must be one of: the whole run, the last or first *N* seconds, or from *A* to *B* seconds after the start — code turns it into two times inside the run (A3). An answer that cannot be used is sent back once, with the reason |
| 3 | The meaning; the time range, with the two times to filter on; only the data models it uses; the query rules | JSON: one query, a chart, a unit per column. A query refused by C1, WrenAI or C2 goes back with the reason, at most twice; then the request stops with the last reason |
| 7 | The request, the meaning, the query, the catalog's formulas for the measures, and a summary of the result that code writes (rows; per number its minimum, maximum and mean; a text column's values) | JSON: 2 to 4 sentences. C3 checks them; after two failures, code writes the explanation from the summary, so it always says only what the result shows |

Data models with no `ts` (a run-level table) take no time range: a time in the request
is set aside for them. Each LLM call is one request with JSON out (design rule 8).

**The record**, `out/<run>/<time>-<request>.json`, keeps the request (and any
rephrasing), the run's database and data models, each step-1 answer and the meaning
used, the time range, every query with its C1 and C2 results and WrenAI's SQL, the
result summary, every explanation with its C3 result, the panel and the link, and for
each LLM call its step, duration, token use and `finish_reason`. It never holds the
LLM's request headers. **The LLM log**, `<same name>.llm.txt` beside it, holds every
request sent to our LLM and its response in full, written as it happens: the address,
the header names (never their values), the model and settings, the system and user
messages; the status, `finish_reason`, tokens, the model's reply and the JSON object the
code took from it. Running it all on the office machines, and what each step does
inside: [phase1-runbook.md](phase1-runbook.md).

## 8. Grafana output (OURS)

- **One folder per run**, created when first needed. **One dashboard per request**,
  titled with the request. Asking the same request about the same run again replaces
  its dashboard: the dashboard's id is made from the two.
- **The dashboard's time range is set to the data's time range** — the result's own
  first and last time when it has a time column (one table can end before the run
  does), else the range asked for. Runs are loaded after they finish, so Grafana's
  default "last 6 hours" would show an empty graph. The dashboard shows UTC.
- **The panel's query is the exact checked SQL**, visible under Edit (C4). The
  explanation goes into the panel description (the ⓘ icon) and is printed in the
  terminal, with any notes from C2.
- **The panel type comes from a template**, chosen by the shape of the result. Our LLM
  proposes a chart; when the result's shape does not suit it, the template that does is
  used, and the description says so.

| Result | Panel |
|---|---|
| a time column and numbers | time series |
| a time column, a text column and numbers (e.g. one row per second and bucket) | time series, one line per text value (Grafana's "prepare time series" transformation) |
| one number | stat |
| categories and numbers | bar chart, the categories along the bottom |
| size or latency buckets | bar chart |
| anything else | table |

**Settings** (`tools/config.json`, `grafana`): `url`, and `token` — a service-account
token with the Editor role — or `user` and `password`; `datasource` picks a ClickHouse
data source by name or id, else the first is used. The token is never printed.
`python tools/check_phase1_env.py --grafana-write-test` checks it on the office box.

## 9. The checks (OURS)

| Check | When | What it guarantees |
|---|---|---|
| **C1** | after WrenAI's plan, before running | Read-only. Reads only what the requested run's data models read — its views (`<table>_interval`, `<table>_histograms`) and its run-level tables — never a raw sampled table, and no other database: checked table by table on WrenAI's final SQL, because strict mode alone let a raw table through (section 6). No table functions (`url`, `s3`, `remote`, `file` …), so a query cannot read from, or send to, anywhere else. The LLM's query used `table.column` and no `WITH` blocks (section 6). Filters to the requested run. For a time range, filters `ts` with exactly the two times, written `toDateTime('…', 'UTC')`. |
| **C2** | after running | An empty result stops the request with why — the samples in that range are counted — and is never drawn as a blank graph. A number column with no value at all, or a negative value (a rate, count, size or time cannot be negative), sends the query back. Gaps are said in the description. |
| **C3** | on the explanation | Every number in it is in the result (within 1 %, for rounding) or in what the graph is made from: the run, its times, the request, the query, the catalog's formulas, the result's labels. Every metric or column it names is one the query uses; a name that is also a plain word (`device`, `range`) is not checked. |

All three are in `analysis/checks.py`; a refusal is sent back to our LLM with its
reason, so the next try can correct it.

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
  __main__.py        command line: ask, check-run, prepare, plan, plan-file   (exists)
  runner.py          one request, steps 0-7, and its record                   (exists)
  catalog.py         read knowledge/catalog                                   (exists)
  runs.py            step 0: find a run's database; compare its columns with the catalog
                                                                              (exists)
  clickhouse.py      ClickHouse access: read-only, except CREATE VIEW         (exists)
  views.py           the per-interval and histogram view SQL for a run, and the run's
                     data models (model_specs)                                (exists)
  wren_project.py    the WrenAI data models for a run; dry_plan               (exists)
  llm.py             the only place that calls our LLM (with tools/llm_config.py)
                                                                              (exists)
  steps.py           understand · write query · explain: prompts, and checked answers
                                                                              (exists)
  checks.py          C1 · C2 · C3                                             (exists)
  grafana.py         folder · dashboard · panel templates                     (exists)
knowledge/           OURS — reviewed by engineers
  catalog/           what every column means                                  (exists)
  rules.md           rules sent to our LLM            (later; today in steps.py)
  examples.yaml      confirmed request → query pairs (B5)                     (later)
requests/            example request files                                    (exists)
tests/test_analysis.py   step 0, views, data models, C1, prepare - no ClickHouse  (exists)
tests/test_runner.py     the request runner, with a scripted LLM - no ClickHouse,
                         LLM or Grafana                                       (exists)
tests/phase1/        trial test set and scoring
tools/               readiness check, LLM check, catalog check                (exist)
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
4. ~~**Answer the catalog questions.**~~ **Done (2026-10-20):** every question of both
   block tables is answered.
5. **Export the existing Grafana dashboards as JSON** — the answer key (section 10).
6. **A Grafana service-account token** (Editor role) in `tools/config.json`, as
   `grafana.token`, for step 6.

## 13. Build order

| Step | Who | Output | Done when |
|---|---|---|---|
| 0 | team | The prerequisites above | — |
| 1 | team | Catalog reviewed | `python tools/catalog.py check` passes; no `todo` left in the columns the trial uses |
| 2 | AI (OURS) | Step 0, and the per-interval views generated for a run | On a real run, the sum of the changes equals last minus first; an older run's missing columns are reported. *Built 2026-10-15; passed with a simulated run on a real ClickHouse 2026-10-16, and on a real run on the office box 2026-10-20* |
| 3 | AI (OURS) | WrenAI data models generated for a run; WrenAI plans a query; C1 on its SQL | A hand-written query on the data models plans and runs; `strace` shows WrenAI opens no network connection. *Built 2026-10-15; with a simulated run on a real ClickHouse a query planned and ran, and matched the raw table (2026-10-16); queries planned and ran on a real run on the office box, and `strace` showed no network connection (2026-10-20); `ref_sql` tried and dropped (section 6)* |
| 4 | AI (OURS) | The request runner, steps 1–7 | One request end to end, from request file to Grafana panel. *Built 2026-10-21 for the block layer, with the latency histograms (section 5.2). Run end to end on a simulated run, on ClickHouse 25.6 and Grafana 13.2.4, with a scripted stand-in for our LLM: time series, a stat for the last 2 minutes, a latency distribution, a C1 refusal sent back and corrected. Not yet run with our LLM, which only the office box reaches* |
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

### Checking step 4 on the office box

With `grafana.token` set in `tools/config.json` (section 8), use the files in
`requests/` — the five example requests of the requirements, and a latency
distribution — with `--run` naming the run:

```
python tools/check_phase1_env.py --grafana-write-test   # the token can make dashboards
python -m analysis prepare <run>                        # now also the histogram view
python -m analysis ask requests/io-counts.yaml --run <run>   # shows the meaning; answer y
```

`ask` prints the meaning, the checked SQL, the explanation and the Grafana link, and
saves the record in `out/<run>/`. The record shows what our LLM answered at each step:
it is the first look at how our LLM reads these prompts.

## 14. Open design questions

1. **Views or tables:** start with views; switch to tables filled at load time only if
   views are too slow on long runs.
2. ~~**Settings:**~~ **Built this way (2026-10-21), for review:** `ask` reads the LLM and
   Grafana settings from `tools/config.json`, the file the readiness checks already use
   (git-ignored), and the ClickHouse host and port from `config/config.yaml`, like the
   other `analysis` commands. Moving them to `config/` can come with the package.
