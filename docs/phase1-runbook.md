# Running Phase 1 — the steps, and what each one does

How to go from a loaded run to Grafana graphs drawn from plain-language requests, on the
office machines, and what happens inside at each step. The design behind it is in
[phase1-design.md](phase1-design.md); the status in [roadmap.md](roadmap.md).

**Where things are**

- **The Linux office server** runs ClickHouse and Grafana, in Docker. Runs are loaded
  there by the team's `deploy.py`, each into a database of its own.
- **The office Windows machine** runs the Phase 1 commands, because the company LLM API
  is reached from there. It uses the server's ClickHouse and Grafana over the network.
  The same commands work on the server itself, if the LLM API can be reached from there.

**In the commands below**

| Placeholder | What to put there |
|---|---|
| `<run-folder>` | the run folder's name, e.g. `ProfileData-fio-20261008-101500`. The loader names each run after its folder, so this is the run's name in ClickHouse. A path to the folder also works: only its last part is used, and the folder is never read |
| `<database>` | the database `deploy.py` loaded the run into. Optional: without it, every database is searched for the run |
| `<server-ip>` | the Linux server's address |

Run every command from the repo folder.

---

## Once per machine

### 1. Get the code

```
git pull
git log -1 --oneline
```

Phase 1 is on the `phase1` branch. `git log` shows the newest commit, to compare with the
one you were told to expect.

### 2. Install the Python packages

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest tests/ -q
```

On Linux, activate with `source .venv/bin/activate` (the server's copy uses `venv`).

**Inside:** the packages are `protobuf` and `grpcio-tools` (the loader reads the
profiler's `.proto` files with them), `PyYAML` (settings and the Measurement Catalog),
`clickhouse-connect` (the ClickHouse client), `wrenai` (turns a query written on our
table descriptions into ClickHouse SQL; it contains no AI and opens no network
connection) and `pytest`. The tests load generated runs into an in-memory store, and run
every Phase 1 step against a fake database, a scripted LLM and a fake Grafana — no
ClickHouse, LLM or Grafana needed. The last line should end in `passed`, with none
failed.

### 3. Settings

Two files, both edited by hand:

| File | Block | Used by | For |
|---|---|---|---|
| `tools/config.json` (git-ignored: it holds credentials; start from `tools/config.example.json`) | `llm` | `ask`, the readiness check | the LLM address, model, custom headers, `timeoutSeconds`, and `payload` — every key of it goes into each request, e.g. `temperature`, `max_tokens` |
| | `grafana` | `ask`, the readiness check | `url`, and `token`: a service-account token with the Editor role (or `user` and `password`) |
| | `clickhouse` | the readiness check only | `host`, `port`, `user` (`default`), `password` (empty), `database` (one run's database) |
| `config/config.yaml` | `store` | the loader, `python -m analysis` | `host` and `port` of ClickHouse's HTTP interface |

On the Windows machine, set `host: <server-ip>` and `port: 8123` under `store:` in
`config/config.yaml`, and the same host in the `clickhouse` block of `tools/config.json`.
On the server itself, both say `localhost`.

### 4. Let the Windows machine into ClickHouse (only when not working on the server)

The ClickHouse Docker image, started without a password, lets the `default` user in only
from the server itself. From any other machine every login fails with *"Authentication
failed: password is incorrect, or there is no user with such name"* — ClickHouse does not
say the real reason, which is the address. To let one more machine in, on the server:

1. Find the address the server sees. Run the failing command from Windows once, then:
   ```
   docker ps
   docker logs --tail 100 <clickhouse-container> 2>&1 | grep -i "authentication failed" | tail -1
   ```
   The address after `from:` is the Windows machine's.
2. See who is allowed now. On the server:
   ```
   curl -s "http://localhost:8123/" --data-binary "SELECT name, auth_type, host_ip FROM system.users FORMAT Vertical"
   ```
3. Write the allowed list again with the Windows address added — every address `host_ip`
   showed must stay, or the server itself is shut out:
   ```
   docker exec -i <clickhouse-container> sh -c 'cat > /etc/clickhouse-server/users.d/zz-allow-windows.xml' <<'EOF'
   <clickhouse>
     <users>
       <default>
         <networks replace="replace">
           <ip>::1</ip>
           <ip>127.0.0.1</ip>
           <ip>WINDOWS-IP</ip>
         </networks>
       </default>
     </users>
   </clickhouse>
   EOF
   ```

**Inside:** ClickHouse reads the files in `users.d/` itself within seconds and merges
them over its user settings; `replace="replace"` makes this list replace the one the
image wrote. Anyone at that address can then use `default` — full rights, no password —
so allow one address, never `::/0`. The file survives `docker restart`, but not a
container that is made again (e.g. a new image) unless it is mounted from outside.

### 5. The readiness check

```
python tools/check_phase1_env.py --skip-llm --grafana-write-test
```

Leave out `--skip-llm` to test the LLM too (slower). No line should say `FAIL`; `INFO`
lines only report, and `SKIP` lines are the parts left out.

**Inside:** standard Python only, nothing to install. It never writes to ClickHouse.

- **Machine:** the Python version, whether PyPI is reachable, whether `wrenai` is
  installed, and which HTTP client is used.
- **ClickHouse:** it connects — the address from the `clickhouse` block, else
  `config/config.yaml`; a request to `localhost` never goes through a proxy — reads
  the user's grants (views need `CREATE`), picks the database (the block's `database`,
  else the most recently loaded run's), lists its tables, finds its runs, checks there
  is one device per run, and on the newest run checks that the change-between-samples
  query adds up to "last minus first", while adding up the raw samples is far too large.
- **LLM:** the model list, a plain answer, tool calls, JSON output, repeat answers,
  response times, and whether a long prompt is cut.
- **Grafana:** health, login, the ClickHouse data source, a query run through Grafana,
  and with `--grafana-write-test` it makes a test folder and dashboard, then deletes
  them — proof the token may draw.

It writes `phase1_check_report.json` (no passwords, keys or tokens).

---

## For each run

### 6. Find the run — `check-run`

```
python -m analysis check-run <run-folder> --db <database>
```

**Inside:** read-only.

1. Reads the Measurement Catalog, `knowledge/catalog/*.yaml`: which tables and columns
   are described, and what they mean.
2. Asks ClickHouse which databases have a described table (`system.tables`), and in each
   — or only in `<database>` — whether a table holds rows of this run (`run_id`).
3. Reads that database's columns (`system.columns`) and compares them with the catalog.

It prints, per table: the usable columns; those *not collected in this run* (described,
but missing); those *not described yet* (in the run, not in the catalog); and the tables
the catalog does not describe yet. A column whose type differs from the catalog's stops
everything: the catalog was written for another proto version.

If the name is not a run's, it lists the runs that are loaded, with their databases; if
it is a database's name, it says so and names the run inside.

### 7. Make the views — `prepare`

```
python -m analysis prepare <run-folder> --db <database>
```

**Inside:** the only writes Phase 1 makes are these `CREATE OR REPLACE VIEW`s, in the run's
own database. A view is only a stored query: nothing is copied, and making one is instant.

1. Step 0 again, as `check-run`.
2. For each table with one row per sample: the view `<table>_interval`. Most columns are
   running totals, so for each one the view gives `<column>_delta`, the change since the
   previous sample of the same run, machine and device, and `interval_s`, the seconds in
   between. A drop (a counter reset) gives "no value", never a negative. Then it checks,
   counter by counter, that the changes add up to the last value minus the first in the
   raw table: `sum of changes = last minus first`.
3. For `linux_block_2_misc`: the view `linux_block_2_misc_histograms`. Its four latency
   histograms are 68 columns — 17 per histogram, named only by position. The view turns
   them into rows (time, histogram, bucket, I/Os) and labels each bucket with its range
   from the run's `latency_buckets` table, e.g. `100 µs - 1 ms`. With *n* thresholds a
   histogram uses *n*+1 of its 17 columns: the last, the overflow, holds every I/O above
   the last threshold, and the unused columns stay 0. It checks that every I/O is in one
   of the run's buckets, and warns, naming the columns, if not.
4. `latency_buckets` itself gets no view: it has one row per bucket and no time.

### 8. Ask — `ask`

```
python -m analysis ask requests/io-counts.yaml --run <run-folder> --db <database>
```

A request file has `run:` and `request:` (what to show, in plain words); `--run` and
`--db` replace the file's, so one file serves any run. `--yes` skips the yes/no question;
`--no-grafana` stops before drawing.

**Inside**, in the order it happens:

0. **Find the run** (as `check-run`), and read its first and last sample time and its
   latency buckets. From these and the catalog, build the run's *data models* — one
   description per view, and one for `latency_buckets`: what each holds, every column's
   meaning and unit, and the catalog's metrics (e.g. `read_iops`) with their formulas.
1. **Our LLM reads the request** (call *1 understand*). Sent: the rules, every data
   model of the run, what the run has that is not described, and the request. Back: JSON
   with what to show (each measure a data model and a metric or column name), the time
   (the whole run, the last or first *N* seconds, or *A* to *B* seconds after the
   start), the chart, the assumptions, and the parts it cannot answer. The code checks
   every name exists in this run and turns the time into two times inside the run;
   an answer it cannot use is sent back once, with the reason.
2. **You confirm.** It prints `I will show: …`. `y` goes on; `n` lets you type a better
   wording, which starts again at 1; Enter stops.
3. **Our LLM writes one query** (call *3 write query*). Sent: the meaning, the time range
   with the exact two times to filter on, the data models it uses, and the query rules.
   Back: JSON with the SQL, the chart and each column's unit.
4. **Check C1 and WrenAI.** C1 checks the query: one SELECT on the data models, every
   column as `table.column`, no `WITH`, filtered to the run — and to the time range,
   written `toDateTime('…', 'UTC')`. WrenAI turns it into ClickHouse SQL over the
   run's views, without running it. C1 then checks WrenAI's SQL reads only the run's
   views and run-level tables, never a raw table or anything else. A refusal goes back
   to step 3 with the reason — at most twice; then the request stops, with the reason.
5. **Run it, check C2.** The views are made (as `prepare`), and the SQL runs read-only,
   with times in UTC. No rows: it stops and says why, counting the samples in the time
   range. A negative value, or a column with no value at all, sends the query back to
   step 3. Rows without a value — e.g. the first sample — are noted.
7. **Our LLM explains** (call *7 explain*). Sent: the request, the meaning, the SQL, the
   catalog's formulas, and a summary the code computes from the result (rows; per number
   its minimum, maximum and mean; the labels). Back: 2 to 4 sentences. C3 checks every
   number is in the result (within 1 %) or in what the graph is made from — the
   request, the run and its times, the meaning, the query, the formulas, the result's
   labels — and every metric or column it names is in the query. Refused twice: the code
   writes the explanation itself, from the summary.
6. **Grafana draws it** — after step 7, so the panel carries the explanation. A folder
   named after the run, one dashboard per request (asking the same again replaces it),
   one panel. Its type follows the result's shape: time and numbers make a time series
   (with a text column, one line per value); one row of numbers a single stat; text and
   numbers a bar chart; else a table. The panel's query is the exact SQL C1 passed; the
   explanation is in the panel's ⓘ; the time range is the result's own.

It prints the meaning, the SQL, `C2: passed` (and its notes), the explanation, the
Grafana link, and two files in `out/<run-folder>/`, both named `<time>-<request>`:

| File | What it holds |
|---|---|
| `….json`, the record | the request, the data models, each LLM answer and the meaning used, the time range, every query with C1, WrenAI's SQL and C2, the result summary, every explanation with C3, the panel and link, and per LLM call its step, seconds, tokens and `finish_reason` |
| `….llm.txt`, the LLM log | every request sent to our LLM and its response, in full, written as it happens: the address, the header names (never their values), the model and settings, the whole system and user messages; then the HTTP status, why the model stopped (`finish_reason`; `length` means it hit `max_tokens`), the tokens, the model's reply as written, and the JSON object the code took from it |

`out/` is git-ignored: the records hold result numbers from real runs.

### 9. The example requests

`requests/` holds the five example requests of the requirements, and one more:

| File | Expect |
|---|---|
| `io-counts.yaml` | read and write I/O counts over time |
| `split-ios.yaml` | split read and write bios over time |
| `latency-fs-and-block.yaml` | block latency drawn; the filesystem part reported as "not described yet" |
| `iops-block-vs-nvme.yaml` | block IOPS drawn; the NVMe part reported as "not described yet" |
| `bandwidth-last-30-min.yaml` | a run shorter than 30 minutes is shown whole |
| `read-latency-distribution.yaml` | a bar chart, one bar per latency range |

To try your own wording, copy a file and change its `request:` line.

### 10. What to send back

For each request: a screenshot of the terminal, from `I will show` to the link, and one
of the Grafana panel; for one that stops, the `STOP:` line. The LLM log shows what our
LLM was asked and what it answered — the place to look when a reading or a query is
wrong.

---

## Errors seen so far

| Error | Cause | Fix |
|---|---|---|
| `Authentication failed: password is incorrect, or there is no user with such name` from the Windows machine | ClickHouse lets `default` in only from the server | step 4 |
| `run '…' was not found … Runs loaded: …` | not a run's name — e.g. the database's | the run folder's name, from that list, and `--db` |
| `'…' is the name of a database, not of a run` | the database's name given as the run | the run it names, and `--db` |
| `cannot reach ClickHouse (config/config.yaml, CH_HOST / CH_PORT)` | wrong host or port in `config/config.yaml` | step 3 |
| readiness check: `Unknown … identifier read_ios` | an old copy of the check (before the column renames) | step 1 |
| `UnicodeEncodeError` on Windows | the console cannot print a character such as `µ` | add `-X utf8` after `python` |
