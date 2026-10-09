#!/usr/bin/env python3
"""Phase 1 readiness check - run this on the office box.

Uses only the Python standard library: nothing to install.

What it checks
  Machine     Python version, PyPI reachable (for `pip install wrenai`),
              Node.js, whether wrenai is already installed.
  ClickHouse  connection, permissions, tables, runs, one device per run,
              device names across tables, and the per-interval query
              (running total -> change between samples) on real data.
              Read-only: nothing is written to ClickHouse.
  LLM API     model list, plain chat, tool calling (one call and its result,
              a forced call, several calls at once, a multi-step
              text-to-SQL loop like the real system, a long argument like an
              app file), JSON output, repeatability, response times against
              timeoutSeconds, and whether a prompt longer than numCtx is cut.
              Optional: a long-context test up to maxContextTokens.
  Grafana     health and version, login, the ClickHouse data source, a query
              through Grafana, and an optional write test that creates and
              then deletes a test folder and dashboard.

Usage, from the repo root

    cp tools/config.example.json tools/config.json    # once, then fill it in
    python tools/check_phase1_env.py

    # optional extras
    python tools/check_phase1_env.py --context-test --grafana-write-test

Settings come from tools/config.json (git-ignored: it holds credentials):
the "llm" block (baseUrl, defaultModel, customHeaders, timeoutSeconds,
numCtx, maxContextTokens, optional apiKey and rateLimitSeconds) and the
optional "clickhouse" and "grafana" blocks. Environment variables CH_URL,
CH_HOST, CH_PORT, CH_USER, CH_PASSWORD, CH_DATABASE, GRAFANA_URL,
GRAFANA_TOKEN, GRAFANA_USER and GRAFANA_PASSWORD override the file.
Defaults: ClickHouse at http://localhost:8123 (user default, database
profile_fw), Grafana at http://localhost:3000.
Behind a company proxy, set HTTPS_PROXY / NO_PROXY as usual.
Run `python tools/check_phase1_env.py --help` for every option, and
`python tools/check_llm.py` for a quick LLM-only check.

Output: a summary on screen, `phase1_check_report.json` (contains no
passwords, keys or tokens) and, if that test ran, `phase1_check_chart.html`
(a chart the LLM wrote). Send the .json file back.

Note: in the text-to-SQL test the LLM's SQL runs read-only against
ClickHouse, so a few result rows are sent to the LLM. Use --mock-sql to
avoid that.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm_config  # noqa: E402  (tools/llm_config.py)

PASS, FAIL, WARN, INFO, SKIP = "PASS", "FAIL", "WARN", "INFO", "SKIP"
RESULTS: list[dict] = []

# check names used by the final verdict
C_TOOL = "tool call + result"
C_LOOP = "multi-step text-to-SQL loop"
C_RULE = "SQL follows the running-total rule"
C_LONG = "long tool argument (app file)"
C_JSON = "JSON output"
C_GQUERY = "query through Grafana"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def record(group: str, name: str, status: str, detail: str, **data) -> str:
    RESULTS.append({"group": group, "check": name, "status": status,
                    "detail": detail, "data": data})
    print(f"  [{status}] {name}: {detail}", flush=True)
    return status


def section(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 60 - len(title)), flush=True)


def brief(value, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " ..."


class HttpResult:
    def __init__(self, status, body, elapsed, error=None):
        self.status = status      # HTTP status, or None when there was no response
        self.body = body          # parsed JSON when possible, else text
        self.elapsed = elapsed
        self.error = error        # connection-level failure text

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def describe(self) -> str:
        if self.status is None:
            return self.error or "no response"
        return f"HTTP {self.status}: {brief(self.body)}"


def http(method, url, headers=None, payload=None, data=None, timeout=60,
         insecure=False) -> HttpResult:
    hdrs = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif data is not None:
        body = data if isinstance(data, bytes) else data.encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    ctx = None
    if url.lower().startswith("https"):
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except Exception as exc:  # refused, DNS, TLS, timeout ...
        return HttpResult(None, None, time.monotonic() - t0,
                          f"{type(exc).__name__}: {exc}")
    elapsed = time.monotonic() - t0
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text.strip() else None
    except ValueError:
        parsed = text
    return HttpResult(status, parsed, elapsed)


def parse_json_reply(text: str):
    """Pull a JSON object out of a model reply (handles ```json fences)."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        break
                    break
        start = text.find("{", start + 1)
    return None


def first_number(rows):
    for row in rows or []:
        values = row.values() if isinstance(row, dict) else row
        for v in values:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


# --------------------------------------------------------------------------
# machine
# --------------------------------------------------------------------------

def check_machine(args) -> None:
    g = "machine"
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    record(g, "Python version", PASS if v >= (3, 11) else FAIL,
           f"{ver}" + ("" if v >= (3, 11) else " - wrenai needs Python 3.11 or newer"))

    r = http("GET", "https://pypi.org/pypi/wrenai/json", timeout=20,
             insecure=args.insecure)
    if r.ok and isinstance(r.body, dict):
        info = r.body.get("info", {})
        record(g, "PyPI reachable", PASS,
               f"wrenai {info.get('version')} is available "
               f"(needs Python {info.get('requires_python')})")
    else:
        record(g, "PyPI reachable", WARN,
               f"could not reach pypi.org ({r.describe()}); `pip install wrenai` "
               "may need a proxy (HTTPS_PROXY) or an internal mirror")

    tools = {}
    for exe in ("node", "npm"):
        path = shutil.which(exe)
        if path:
            try:
                out = subprocess.run([path, "--version"], capture_output=True,
                                     text=True, timeout=15).stdout.strip()
            except Exception as exc:
                out = f"error: {exc}"
            tools[exe] = out
    if tools:
        record(g, "Node.js", INFO, ", ".join(f"{k} {v}" for k, v in tools.items()))
    else:
        record(g, "Node.js", INFO,
               "not installed - may be needed only for the GenBI path, "
               "where the AI writes a dashboard web app")

    found = importlib.util.find_spec("wrenai") is not None
    cli = shutil.which("wren")
    record(g, "wrenai installed", INFO,
           ("yes" if found or cli else "not yet (fine - the trial installs it)")
           + (f"; wren CLI at {cli}" if cli else ""))


# --------------------------------------------------------------------------
# ClickHouse (read-only)
# --------------------------------------------------------------------------

EXPECTED_TABLES = [
    "linux_block_1_stats", "linux_block_2_misc", "linux_nvme_1_stats",
    "linux_nvme_1_stats_lba_ranges", "linux_nvme_1_stats_per_core",
    "linux_nvme_1_stats_per_queue", "linux_nvme_2_lbarandomness",
    "linux_nvme_2_lbarandomness_per_core_seq_random_entries",
    "linux_ssd_1_stats", "linux_syscall_1_stats",
    "linux_syscall_1_stats_io_patterns", "linux_syscall_2_ioflags",
]


class ClickHouse:
    def __init__(self, args):
        self.url = (args.ch_url or f"http://{args.ch_host}:{args.ch_port}").rstrip("/") + "/"
        self.headers = {"X-ClickHouse-User": args.ch_user}
        if args.ch_password:
            self.headers["X-ClickHouse-Key"] = args.ch_password
        self.db = args.ch_database
        self.insecure = args.insecure

    def query(self, sql, params=None, use_db=True, timeout=30):
        qs = {"readonly": "2", "max_execution_time": "20",
              "max_result_rows": "1000", "result_overflow_mode": "break"}
        if use_db:
            qs["database"] = self.db
        for k, v in (params or {}).items():
            qs[f"param_{k}"] = str(v)
        sql = sql.strip().rstrip(";")
        r = http("POST", self.url + "?" + urllib.parse.urlencode(qs), self.headers,
                 data=sql + "\nFORMAT JSON", timeout=timeout, insecure=self.insecure)
        if r.ok and isinstance(r.body, dict):
            return r.body.get("data", []), None
        return None, r.describe()


PER_INTERVAL_SQL = """
SELECT
    count()                                        AS samples,
    countIf(rn > 1 AND cur < prev)                 AS counter_resets,
    sumIf(cur - prev, rn > 1 AND cur >= prev)      AS sum_of_changes,
    max(cur) - min(cur)                            AS max_minus_min,
    sum(cur)                                       AS naive_sum,
    quantileExactIf(0.5)(gap, rn > 1)              AS median_interval_s,
    maxIf(gap, rn > 1)                             AS max_gap_s
FROM
(
    SELECT
        toInt64(read_ios)                              AS cur,
        toInt64(lagInFrame(read_ios) OVER w)           AS prev,
        dateDiff('second', lagInFrame(ts) OVER w, ts)  AS gap,
        row_number() OVER wn                           AS rn
    FROM linux_block_1_stats
    WHERE run_id = {run:String}
    WINDOW w  AS (PARTITION BY hostname, device ORDER BY ts
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
           wn AS (PARTITION BY hostname, device ORDER BY ts)
)
"""


def check_clickhouse(args, ctx) -> None:
    g = "clickhouse"
    if not re.fullmatch(r"[A-Za-z0-9_]+", args.ch_database):
        record(g, "connection", FAIL, f"invalid database name {args.ch_database!r}")
        return
    ch = ClickHouse(args)
    rows, err = ch.query("SELECT version() AS version, timezone() AS tz, "
                         "currentUser() AS user", use_db=False)
    if err:
        record(g, "connection", FAIL, f"{ch.url} - {err}")
        return
    info = rows[0]
    record(g, "connection", PASS,
           f"ClickHouse {info['version']}, user {info['user']}, server timezone {info['tz']}")
    ctx["ch"] = ch

    rows, err = ch.query("SHOW GRANTS", use_db=False)
    if err:
        record(g, "permissions", WARN, f"could not read grants: {err}")
    else:
        grants = " | ".join(str(next(iter(r.values()))) for r in rows)
        can_view = bool(re.search(r"GRANT ALL\b|CREATE VIEW|GRANT CREATE ON", grants))
        record(g, "permissions", PASS if can_view else WARN,
               ("can create views (needed for the per-interval views)" if can_view
                else "no CREATE VIEW permission seen - the per-interval views "
                     "will need it") + f"; grants: {brief(grants, 200)}",
               grants=grants)

    rows, err = ch.query("SELECT name, total_rows FROM system.tables "
                         "WHERE database = {db:String} ORDER BY name",
                         {"db": args.ch_database}, use_db=False)
    if err:
        record(g, "tables", FAIL, err)
        return
    names = {r["name"]: r["total_rows"] for r in rows}
    missing = [t for t in EXPECTED_TABLES if t not in names]
    extra = sorted(set(names) - set(EXPECTED_TABLES))
    record(g, "tables", PASS if not missing else WARN,
           f"{len(names)} table(s) in {args.ch_database}"
           + (f"; missing: {', '.join(missing)}" if missing else "")
           + (f"; not in the sample protos: {', '.join(extra)}" if extra else ""),
           tables=names)
    if "linux_block_1_stats" not in names:
        record(g, "runs", SKIP, "linux_block_1_stats not found")
        return

    rows, err = ch.query("SELECT run_id, count() AS samples, min(ts) AS first, "
                         "max(ts) AS last FROM linux_block_1_stats "
                         "GROUP BY run_id ORDER BY last DESC LIMIT 20")
    if err or not rows:
        record(g, "runs", FAIL if err else WARN, err or "no runs loaded yet")
        return
    ctx["run"] = rows[0]["run_id"]
    record(g, "runs", PASS,
           f"{len(rows)} run(s) found (showing up to 20); newest: {ctx['run']} "
           f"({rows[0]['samples']} samples, {rows[0]['first']} to {rows[0]['last']})",
           runs=rows)

    rows, err = ch.query("SELECT run_id, uniqExact(device) AS devices, "
                         "groupUniqArray(device) AS names FROM linux_block_1_stats "
                         "GROUP BY run_id HAVING devices > 1 LIMIT 20")
    if err:
        record(g, "one device per run", WARN, err)
    else:
        record(g, "one device per run", PASS if not rows else WARN,
               "every run has exactly one device" if not rows
               else f"{len(rows)} run(s) have several devices: {brief(rows, 200)}",
               runs_with_several_devices=rows)

    names_by_table = {}
    for table in ("linux_block_1_stats", "linux_ssd_1_stats"):
        if table in names:
            rows, err = ch.query(f"SELECT groupUniqArray(device) AS devices FROM {table}")
            if not err and rows:
                names_by_table[table] = rows[0]["devices"]
    if len(names_by_table) == 2:
        same = set(names_by_table["linux_block_1_stats"]) == set(names_by_table["linux_ssd_1_stats"])
        record(g, "device names across tables", PASS if same else INFO,
               ("same device names in the block and SSD tables" if same else
                "block and SSD tables name the device differently "
                f"({names_by_table}) - joins between them need a mapping"),
               devices=names_by_table)

    rows, err = ch.query(PER_INTERVAL_SQL, {"run": ctx["run"]})
    if err or not rows:
        record(g, "per-interval query on real data", FAIL,
               err or "no rows", sql=PER_INTERVAL_SQL)
        return
    s = rows[0]
    total = int(s["max_minus_min"])
    changes = int(s["sum_of_changes"])
    resets = int(s["counter_resets"])
    naive = int(s["naive_sum"])
    ctx["reference_total_reads"] = total
    status = PASS if (changes == total and resets == 0) else WARN
    ratio = (f"{naive / total:,.0f}x too large" if total else "not comparable (no reads)")
    record(g, "per-interval query on real data", status,
           f"run {ctx['run']}: total reads {total:,} (max - min) = {changes:,} "
           f"(sum of changes between samples); resets: {resets}; median sample "
           f"interval {s['median_interval_s']} s, largest gap {s['max_gap_s']} s. "
           f"Adding up the raw samples instead would give {naive:,} - {ratio}.",
           stats=s)


# --------------------------------------------------------------------------
# LLM API
# --------------------------------------------------------------------------

class Llm:
    OPTIONAL = ("max_tokens", "temperature", "seed")

    def __init__(self, settings: dict, args):
        self.url = llm_config.chat_url(settings)
        self.models_url = llm_config.models_url(settings)
        self.headers = llm_config.headers(settings)
        self.model = settings["defaultModel"]
        # the configured timeout is used as-is for the plain call; the heavier
        # checks get more time, so a slow reply is not mistaken for a failure
        self.config_timeout = float(settings.get("timeoutSeconds") or 120)
        self.timeout = args.llm_timeout or max(self.config_timeout, 120.0)
        self.pause = float(settings.get("rateLimitSeconds") or 0)
        self.insecure = args.insecure
        self.dropped: set[str] = set()     # optional params the server rejected
        self.durations: list[float] = []   # seconds, for every answered call
        self._last = 0.0

    def chat(self, messages, timeout=None, **params) -> HttpResult:
        payload = {"model": self.model, "messages": messages}
        payload.update({k: v for k, v in params.items()
                        if v is not None and k not in self.dropped})
        r = None
        for attempt in range(4):
            wait = self.pause - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            r = http("POST", self.url, self.headers, payload,
                     timeout=timeout or self.timeout, insecure=self.insecure)
            self._last = time.monotonic()
            if r.status is not None:
                self.durations.append(r.elapsed)
            if r.status == 429 and attempt < 3:      # rate-limited by the gateway
                time.sleep(max(self.pause, 5.0))
                continue
            if r.status == 400:
                text = r.body if isinstance(r.body, str) else json.dumps(r.body)
                bad = [p for p in self.OPTIONAL if p in payload and p in text]
                if bad:
                    for p in bad:
                        payload.pop(p, None)
                        self.dropped.add(p)
                    continue
            break
        return r

    @staticmethod
    def message(r: HttpResult):
        try:
            return r.body["choices"][0]["message"]
        except (TypeError, KeyError, IndexError):
            return None

    @staticmethod
    def finish(r: HttpResult):
        try:
            return r.body["choices"][0].get("finish_reason")
        except (TypeError, KeyError, IndexError):
            return None


def tool_calls_of(msg) -> list[dict]:
    """Normalise tool calls: [{id, name, args (dict or None), raw, legacy?}]."""
    out = []
    if not msg:
        return out
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw = fn.get("arguments")
        args = raw if isinstance(raw, dict) else None
        if isinstance(raw, str):
            try:
                args = json.loads(raw) if raw.strip() else {}
            except ValueError:
                args = None
        out.append({"id": tc.get("id") or f"call_{i}", "name": fn.get("name"),
                    "args": args,
                    "raw": raw if isinstance(raw, str) else json.dumps(raw)})
    if not out and msg.get("function_call"):  # old single-call format
        fc = msg["function_call"]
        raw = fc.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = None
        out.append({"id": "legacy", "name": fc.get("name"), "args": args,
                    "raw": raw, "legacy": True})
    return out


def assistant_echo(msg, calls) -> dict:
    return {"role": "assistant", "content": msg.get("content"),
            "tool_calls": [{"id": c["id"], "type": "function",
                            "function": {"name": c["name"],
                                         "arguments": c["raw"] or "{}"}}
                           for c in calls]}


TOOL_HINT = ("If the model wrote the call as plain text, the LLM server may not have "
             "tool parsing switched on (for vLLM: start it with "
             "--enable-auto-tool-choice and a --tool-call-parser for the model). "
             "Ask whoever runs the LLM service.")


def fn_tool(name, description, properties=None, required=None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": required or []}}}


DEVICE_TOOL = fn_tool("get_run_device", "Return the storage device profiled in a run.",
                      {"run_name": {"type": "string", "description": "Name of the run"}},
                      ["run_name"])

CATALOG = {
    "linux_block_1_stats": {
        "description": "Block-layer counters for the profiled device, read from "
                       "/sys/block/<device>/stat. One row per sample (every second).",
        "columns": [
            {"name": "run_id", "type": "String", "kind": "label", "meaning": "Name of the profiling run."},
            {"name": "ts", "type": "DateTime", "kind": "timestamp", "meaning": "Sample time, UTC, whole seconds."},
            {"name": "hostname", "type": "String", "kind": "label", "meaning": "Machine that was profiled."},
            {"name": "device", "type": "String", "kind": "label", "meaning": "Block device name."},
            {"name": "read_ios", "type": "UInt64", "kind": "counter", "unit": "count", "meaning": "Read requests completed since boot."},
            {"name": "write_ios", "type": "UInt64", "kind": "counter", "unit": "count", "meaning": "Write requests completed since boot."},
            {"name": "sectors_read", "type": "UInt64", "kind": "counter", "unit": "sectors of 512 bytes", "meaning": "Sectors read since boot."},
            {"name": "sectors_written", "type": "UInt64", "kind": "counter", "unit": "sectors of 512 bytes", "meaning": "Sectors written since boot."},
            {"name": "read_time_ms", "type": "UInt64", "kind": "counter", "unit": "ms", "meaning": "Time spent on reads since boot, added up over all requests."},
            {"name": "in_flight_ios", "type": "UInt64", "kind": "gauge", "unit": "count", "meaning": "Requests in flight at the moment of sampling."},
        ]},
    "linux_block_2_misc": {
        "description": "Block-layer bio counts from eBPF (bios submitted and bios split). "
                       "One row per sample.",
        "columns": [
            {"name": "run_id", "type": "String", "kind": "label", "meaning": "Name of the profiling run."},
            {"name": "ts", "type": "DateTime", "kind": "timestamp", "meaning": "Sample time, UTC."},
            {"name": "read_bios", "type": "UInt64", "kind": "counter", "unit": "count", "meaning": "Read bios submitted since the profiler started."},
            {"name": "read_splits", "type": "UInt64", "kind": "counter", "unit": "count", "meaning": "Read bios split since the profiler started."},
        ]},
    "linux_nvme_1_stats": {
        "description": "NVMe commands sent to the SSD, from eBPF. One row per sample.",
        "columns": [
            {"name": "run_id", "type": "String", "kind": "label", "meaning": "Name of the profiling run."},
            {"name": "ts", "type": "DateTime", "kind": "timestamp", "meaning": "Sample time, UTC."},
            {"name": "read_ios", "type": "UInt64", "kind": "counter", "unit": "count", "meaning": "NVMe read commands since the profiler started."},
            {"name": "write_ios", "type": "UInt64", "kind": "counter", "unit": "count", "meaning": "NVMe write commands since the profiler started."},
        ]},
}

RULES = """Rules for this data:
- Every table has run_id (the run) and ts (the sample time). Always filter by run_id.
- kind = counter means a running total. NEVER add up its samples: sum(read_ios) is wrong.
  The total for a run is max(column) - min(column) within that run.
  A per-second value is the difference between consecutive samples ordered by ts.
- kind = gauge is a reading at one moment. Never subtract its samples.
- Sectors are 512 bytes.
- Write ClickHouse SQL. Use the table names exactly as listed."""

AGENT_TOOLS = [
    fn_tool("list_tables", "List the available tables with a one-line description each."),
    fn_tool("describe_table", "Describe a table's columns: meaning, unit and kind.",
            {"table": {"type": "string"}}, ["table"]),
    fn_tool("run_sql", "Run a read-only ClickHouse SQL query and return up to 20 rows.",
            {"sql": {"type": "string"}}, ["sql"]),
    fn_tool("final_answer", "Give the final answer. Call this exactly once, at the end.",
            {"sql": {"type": "string", "description": "The final ClickHouse query"},
             "chart_type": {"type": "string",
                            "enum": ["timeseries", "line", "bar", "table", "stat", "histogram"]},
             "interpretation": {"type": "string",
                                "description": "One sentence: what you understood the user wants"},
             "explanation": {"type": "string",
                             "description": "Short: what the result shows"}},
            ["sql", "chart_type", "interpretation", "explanation"]),
]

WRITE_TOOL = fn_tool("write_file", "Write a text file.",
                     {"path": {"type": "string"}, "content": {"type": "string"}},
                     ["path", "content"])

JSON_QUESTION = (
    "Table linux_block_1_stats has columns run_id, ts and read_ios. read_ios is a "
    "running total of read requests completed since boot. Write a ClickHouse query "
    "for the total number of reads in run 'R1'. Reply with JSON only, exactly in "
    'this shape: {"sql": "...", "chart_type": "bar|line|table|stat", '
    '"explanation": "..."}')

JSON_SCHEMA = {"type": "json_schema", "json_schema": {
    "name": "answer", "strict": True, "schema": {
        "type": "object",
        "properties": {"sql": {"type": "string"},
                       "chart_type": {"type": "string",
                                      "enum": ["bar", "line", "table", "stat"]},
                       "explanation": {"type": "string"}},
        "required": ["sql", "chart_type", "explanation"],
        "additionalProperties": False}}}


def judge_counter_sql(sql: str):
    s = re.sub(r"\s+", " ", (sql or "").lower())
    if re.search(r"sum\s*\(\s*(toint64\s*\(\s*)?(\w+\.)?read_ios\b", s):
        return FAIL, "adds up the samples of the running total read_ios (the classic mistake)"
    uses_change = (("max(" in s and "min(" in s) or "laginframe" in s
                   or "neighbor(" in s or "runningdifference" in s
                   or "argmax(" in s)
    if uses_change and "run_id" in s:
        return PASS, "uses the change in the running total, filtered by run"
    if uses_change:
        return WARN, "uses the change in the running total but does not filter by run_id"
    return WARN, "could not judge automatically - please read the SQL"


def check_llm(args, ctx) -> None:
    g = "llm"
    if args.cfg is None:
        missing = not os.path.isfile(args.config or llm_config.DEFAULT_PATH)
        record(g, "configuration", SKIP if missing else FAIL, args.cfg_error)
        return
    try:
        settings = llm_config.llm_settings(args.cfg)
    except llm_config.ConfigError as exc:
        record(g, "configuration", FAIL, str(exc))
        return
    ctx["llm_summary"] = llm_config.summary(settings)
    llm = Llm(settings, args)
    s = ctx["llm_summary"]
    record(g, "configuration", INFO,
           f"{s['endpoint']}, model {s['model']}, headers {', '.join(s['customHeaders']) or 'none'}"
           f"; timeoutSeconds {s['timeoutSeconds']} (heavier checks use {llm.timeout:g} s), "
           f"numCtx {s['numCtx']}, maxContextTokens {s['maxContextTokens']}")

    # model list --------------------------------------------------------
    r = http("GET", llm.models_url, llm.headers, timeout=30, insecure=args.insecure)
    if r.ok and isinstance(r.body, dict):
        models = [m for m in (r.body.get("data") or []) if isinstance(m, dict)]
        ids = [m.get("id") for m in models]
        ctx_info = {}
        for m in models:
            if m.get("id") == llm.model:
                for k in ("max_model_len", "context_length", "context_window",
                          "max_context_length", "max_input_tokens", "max_tokens"):
                    if k in m:
                        ctx_info[k] = m[k]
        record(g, "model list", PASS if llm.model in ids else WARN,
               f"{len(ids)} model(s) listed"
               + ("" if llm.model in ids else f"; '{llm.model}' is not among them")
               + (f"; context information: {ctx_info}" if ctx_info else ""),
               models=ids[:50], context_info=ctx_info)
    else:
        record(g, "model list", WARN,
               f"not available ({r.describe()}) - some gateways hide it; not required")

    # plain chat ---------------------------------------------------------
    plain = [{"role": "user", "content": "Reply with exactly the word OK."}]
    r = llm.chat(plain, max_tokens=300, temperature=0, timeout=llm.config_timeout)
    slow_note = ""
    if r.status is None and "timed out" in (r.error or "").lower():
        r = llm.chat(plain, max_tokens=300, temperature=0)
        slow_note = (f" - but only with a longer timeout: it took {r.elapsed:.1f} s, more "
                     f"than timeoutSeconds ({llm.config_timeout:g} s)")
    msg = Llm.message(r)
    if not (r.ok and msg is not None):
        record(g, "plain chat", FAIL,
               f"{r.describe()} - check baseUrl (does it need /v1?), the custom headers "
               "and defaultModel in tools/config.json; the other LLM checks are skipped")
        return
    content = (msg.get("content") or "").strip()
    thinking = bool(msg.get("reasoning_content") or msg.get("reasoning"))
    note = (" - it also returned its reasoning, so it is a 'thinking' model "
            "(slower, uses more tokens)" if thinking else "") + slow_note
    if not content:
        record(g, "plain chat", WARN,
               f"empty answer (finish_reason={Llm.finish(r)}){note}",
               served_model=(r.body or {}).get("model"), usage=(r.body or {}).get("usage"))
    else:
        record(g, "plain chat", WARN if slow_note else PASS,
               f"answered {brief(content, 40)!r} in {r.elapsed:.1f} s{note}",
               served_model=(r.body or {}).get("model"), usage=(r.body or {}).get("usage"))

    # one tool call, then its result -----------------------------------------
    messages = [{"role": "user", "content":
                 "Which device was profiled in the run "
                 "'ProfileData-check-20261001-101500'? Use the tool to find out."}]
    r = llm.chat(messages, tools=[DEVICE_TOOL], tool_choice="auto",
                 temperature=0, max_tokens=1000)
    msg = Llm.message(r)
    if not r.ok:
        record(g, C_TOOL, FAIL, f"the server rejected a request with tools: {r.describe()}")
    else:
        calls = tool_calls_of(msg)
        if not calls:
            record(g, C_TOOL, FAIL,
                   f"no tool call came back; the model replied in text: "
                   f"{brief((msg or {}).get('content') or '', 160)!r}. {TOOL_HINT}")
        else:
            c = calls[0]
            if c["name"] != "get_run_device" or not isinstance(c["args"], dict) \
                    or "run_name" not in c["args"]:
                record(g, C_TOOL, FAIL,
                       f"malformed tool call: name={c['name']!r} arguments={c['raw']!r}")
            else:
                messages.append(assistant_echo(msg, calls))
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps({"device": "nvme3n1"})})
                r2 = llm.chat(messages, tools=[DEVICE_TOOL], temperature=0, max_tokens=1000)
                final = (Llm.message(r2) or {}).get("content") or ""
                legacy = " (old 'function_call' format)" if c.get("legacy") else ""
                if r2.ok and "nvme3n1" in final:
                    record(g, C_TOOL, PASS,
                           f"tool call, then answer from the tool result, both work "
                           f"({r.elapsed:.1f} s + {r2.elapsed:.1f} s){legacy}")
                elif r2.ok:
                    record(g, C_TOOL, WARN,
                           f"tool call works, but the answer after the tool result "
                           f"ignored it: {brief(final, 160)!r}{legacy}")
                else:
                    record(g, C_TOOL, FAIL,
                           f"tool call works, but sending the tool result back "
                           f"failed: {r2.describe()}{legacy}")

    # forced tool call -------------------------------------------------------
    forced = {}
    for label, choice in (("by name", {"type": "function",
                                       "function": {"name": "get_run_device"}}),
                          ("'required'", "required")):
        r = llm.chat([{"role": "user", "content": "Hello! How are you today?"}],
                     tools=[DEVICE_TOOL], tool_choice=choice, temperature=0,
                     max_tokens=1000)
        forced[label] = bool(r.ok and tool_calls_of(Llm.message(r)))
    record(g, "forced tool call", PASS if any(forced.values()) else WARN,
           "; ".join(f"forcing {k}: {'works' if v else 'does not work'}"
                     for k, v in forced.items())
           + " (lets our code make the model call a chosen tool at a chosen step)",
           forced=forced)

    # several tool calls at once ------------------------------------------------
    r = llm.chat([{"role": "user", "content":
                   "Which devices were profiled in the runs "
                   "'ProfileData-a-20261001-101500' and 'ProfileData-b-20261002-101500'? "
                   "Use the tool for each run."}],
                 tools=[DEVICE_TOOL], tool_choice="auto", temperature=0, max_tokens=1000)
    n = len(tool_calls_of(Llm.message(r))) if r.ok else 0
    record(g, "several tool calls at once", INFO,
           f"{n} tool call(s) in one reply - "
           + ("can call several tools at once" if n >= 2 else
              "calls tools one at a time (fine; only slower)" if n == 1 else
              f"no tool calls at all (see '{C_TOOL}')"))

    # multi-step text-to-SQL loop (the way the real system works) ---------------
    check_agent_loop(args, ctx, llm, g)

    # long argument, like an app file --------------------------------------------
    r = llm.chat([{"role": "user", "content":
                   "Write one self-contained HTML file named chart.html that draws a bar "
                   "chart of read IOPS per layer: syscall 1200, block 4800, nvme 4790. "
                   "Use plain JavaScript and inline SVG only - no external libraries or "
                   "links. Label the axes and the bars. Save it by calling write_file."}],
                 tools=[WRITE_TOOL], tool_choice="auto", temperature=0, max_tokens=8000)
    msg = Llm.message(r)
    calls = tool_calls_of(msg) if r.ok else []
    finish = Llm.finish(r)
    if not r.ok:
        record(g, C_LONG, FAIL, r.describe())
    elif not calls:
        record(g, C_LONG, FAIL, f"no tool call came back (finish_reason={finish}). {TOOL_HINT}")
    elif not isinstance(calls[0]["args"], dict):
        record(g, C_LONG, FAIL,
               f"the arguments were not valid JSON (finish_reason={finish}; "
               f"{len(calls[0]['raw'] or '')} characters) - long tool arguments get cut "
               "off or broken; this matters for the GenBI path")
    else:
        content = str(calls[0]["args"].get("content", ""))
        low = content.lower()
        good = len(content) >= 800 and ("<svg" in low or "<canvas" in low) \
            and "</html>" in low and "<script src=" not in low
        out = os.path.abspath(args.html_out)
        try:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(content)
            saved = f"saved to {out}"
        except OSError as exc:
            saved = f"could not save: {exc}"
        record(g, C_LONG, PASS if good else WARN,
               f"{len(content):,} characters of HTML in one tool call "
               f"(finish_reason={finish}, {r.elapsed:.1f} s); {saved}"
               + ("" if good else " - incomplete or uses external links; open it to see"))

    # JSON output -------------------------------------------------------------------
    works, replies = {}, {}
    variants = {"response_format=json_object": {"response_format": {"type": "json_object"}},
                "response_format=json_schema": {"response_format": JSON_SCHEMA},
                "plain prompt": {}}
    for label, extra in variants.items():
        r = llm.chat([{"role": "user", "content": JSON_QUESTION}],
                     temperature=0, max_tokens=1500, **extra)
        content = (Llm.message(r) or {}).get("content") if r.ok else None
        obj = parse_json_reply(content or "")
        works[label] = bool(obj and obj.get("sql"))
        replies[label] = obj if obj else (r.describe() if not r.ok else brief(content or "", 200))
    record(g, C_JSON, PASS if any(works.values()) else FAIL,
           "; ".join(f"{k}: {'works' if v else 'no'}" for k, v in works.items()),
           replies=replies)

    # repeatability --------------------------------------------------------------------
    label = next((k for k, v in works.items() if v), None)
    if label:
        sqls = []
        for _ in range(2):
            r = llm.chat([{"role": "user", "content": JSON_QUESTION}], temperature=0,
                         seed=7, max_tokens=1500, **variants[label])
            obj = parse_json_reply(((Llm.message(r) or {}).get("content") or "") if r.ok else "")
            sqls.append(" ".join(str(obj.get("sql", "")).split()) if obj else None)
        same = sqls[0] is not None and sqls[0] == sqls[1]
        record(g, "repeatability", PASS if same else WARN,
               "the same request twice gave the same SQL" if same else
               "the same request twice gave different SQL - our design must pin it "
               "down (temperature 0 and confirmed examples)",
               sqls=sqls, ignored_params=sorted(llm.dropped))
    else:
        record(g, "repeatability", SKIP, "no JSON variant worked")

    if llm.dropped:
        record(g, "parameters", INFO,
               f"the server rejected these optional parameters, so they were dropped: "
               f"{', '.join(sorted(llm.dropped))}")

    # prompt longer than numCtx (always) and long context (optional) -------------------
    num_ctx = int(settings.get("numCtx") or 8192)
    max_ctx = int(settings.get("maxContextTokens") or 0)
    if args.context_sizes:
        sizes = [int(x) for x in args.context_sizes.split(",") if x.strip()]
    elif args.context_test:
        sizes = sorted({16000, 64000, int(max_ctx * 0.85) if max_ctx else 110000})
    else:
        sizes = [int(num_ctx * 1.5)]
    check_context(args, llm, g, sizes, num_ctx, full=args.context_test)

    # response times against timeoutSeconds ---------------------------------------------
    if llm.durations:
        times = sorted(llm.durations)
        over = sum(1 for t in times if t > llm.config_timeout)
        record(g, "response times", WARN if over else PASS,
               f"{len(times)} calls; median {times[len(times) // 2]:.1f} s, slowest "
               f"{times[-1]:.1f} s; "
               + (f"{over} call(s) took longer than timeoutSeconds "
                  f"({llm.config_timeout:g} s) and would fail with that setting"
                  if over else f"all within timeoutSeconds ({llm.config_timeout:g} s)"),
               seconds=[round(t, 1) for t in times])


def check_agent_loop(args, ctx, llm, g) -> None:
    ch = None if args.mock_sql else ctx.get("ch")
    run = ctx.get("run") or "ProfileData-check-20261001-101500"

    def run_sql(sql):
        text = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql or "", flags=re.S).strip().lower()
        if not (text.startswith("select") or text.startswith("with")):
            return {"error": "only SELECT queries are allowed"}
        if ch is None:
            return {"columns": ["result"], "rows": [[48213]],
                    "note": "mock result - ClickHouse was not used"}
        rows, err = ch.query(sql)
        if err:
            return {"error": brief(err, 500)}
        return {"rows": rows[:20], "row_count": len(rows)}

    def execute(name, a):
        if a is None:
            return {"error": "the arguments were not valid JSON"}
        if name == "list_tables":
            return [{"table": t, "description": v["description"]} for t, v in CATALOG.items()]
        if name == "describe_table":
            return CATALOG.get(a.get("table")) or {"error": f"unknown table {a.get('table')!r}"}
        if name == "run_sql":
            return run_sql(a.get("sql", ""))
        if name == "final_answer":
            return {"ok": True}
        return {"error": f"unknown tool {name!r}"}

    system = ("You answer questions about storage profiling data in ClickHouse by "
              "calling tools. First find the right table and columns (list_tables, "
              "describe_table). Then test your SQL with run_sql. Finally call "
              "final_answer exactly once.\n\n" + RULES)
    question = f"Show the total number of read I/Os at the block layer for run '{run}'."
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    trace, final, error = [], None, None
    t0 = time.monotonic()
    for step in range(1, args.max_steps + 1):
        r = llm.chat(messages, tools=AGENT_TOOLS, tool_choice="auto",
                     temperature=0, max_tokens=3000)
        msg = Llm.message(r)
        if not (r.ok and msg is not None):
            error = f"step {step}: {r.describe()}"
            break
        calls = tool_calls_of(msg)
        if not calls:
            trace.append({"step": step, "text": brief(msg.get("content") or "", 300)})
            break
        messages.append(assistant_echo(msg, calls))
        for c in calls:
            result = execute(c["name"], c["args"])
            trace.append({"step": step, "tool": c["name"],
                          "args": c["args"] if c["args"] is not None else c["raw"],
                          "result": brief(result, 300)})
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(result, default=str)[:4000]})
            if c["name"] == "final_answer" and isinstance(c["args"], dict):
                final = c["args"]
        if final:
            break
    elapsed = time.monotonic() - t0
    used = [t.get("tool") for t in trace if t.get("tool")]
    sql_source = "real ClickHouse (read-only)" if ch else "a mock result"

    if final:
        looked = "describe_table" in used or "list_tables" in used
        record(g, C_LOOP, PASS if looked and "run_sql" in used else WARN,
               f"finished in {len(set(t['step'] for t in trace))} step(s), {elapsed:.1f} s; "
               f"tools used: {' -> '.join(used)}; SQL ran against {sql_source}",
               question=question, trace=trace, final=final)
        print(f"         understood : {brief(final.get('interpretation', ''), 200)}")
        print(f"         SQL        : {brief(final.get('sql', ''), 400)}")
        print(f"         chart      : {final.get('chart_type')}")
        print(f"         explanation: {brief(final.get('explanation', ''), 200)}")
        status, why = judge_counter_sql(final.get("sql", ""))
        record(g, C_RULE, status, why, sql=final.get("sql"))
        ref = ctx.get("reference_total_reads")
        if ch is not None and ref is not None:
            rows, err = ch.query(final.get("sql", ""))
            value = first_number(rows) if not err else None
            if value is not None and int(value) == int(ref):
                record(g, "answer matches reference", PASS,
                       f"{int(value):,} = the reference total from the per-interval check")
            else:
                record(g, "answer matches reference", WARN,
                       f"model's query gave {value if value is not None else err}; "
                       f"the reference total is {ref:,}")
    else:
        said = next((t["text"] for t in reversed(trace) if t.get("text")), "")
        record(g, C_LOOP, FAIL,
               (error or "the model never called final_answer")
               + f"; tools used: {' -> '.join(used) or 'none'}"
               + (f"; its last reply was text: {brief(said, 160)!r}" if said else ""),
               question=question, trace=trace)


def check_context(args, llm, g, sizes, num_ctx, full) -> None:
    """Plant a code near the START of a long prompt and ask for it back.

    A server that cannot hold the prompt (Ollama does this to anything longer
    than numCtx) drops the start without an error, so the code is lost - and
    the reported prompt_tokens comes back short of the real size."""
    outcome = []
    for size in sizes:
        code = f"ZX-{size % 9973:04d}-Q"
        lines = max(10, size * 4 // 78)
        body = [f"Note {i:06d}: routine log entry with nothing important to report.\n"
                for i in range(lines)]
        at = max(1, lines // 10)
        body[at] = f"Note {at:06d}: the secret verification code is {code}.\n"
        prompt = ("".join(body) + "\nQuestion: what is the secret verification code "
                  "mentioned in the text above? Reply with the code only.")
        r = llm.chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=300,
                     timeout=args.context_timeout)
        content = ((Llm.message(r) or {}).get("content") or "") if r.ok else ""
        used = ((r.body or {}).get("usage") or {}).get("prompt_tokens") if r.ok else None
        cut = isinstance(used, int) and used < size * 0.75
        found = r.ok and code in content
        outcome.append({"approx_tokens": size, "prompt_tokens_reported": used,
                        "found": found, "cut": cut, "seconds": round(r.elapsed, 1),
                        "error": None if r.ok else r.describe()})
        if not found or cut:
            break

    def describe(o):
        size = f"~{o['approx_tokens']:,} tokens"
        if o["cut"]:
            return (f"{size}: CUT - the server kept only {o['prompt_tokens_reported']:,} "
                    "tokens and dropped the rest without an error")
        if o["found"]:
            return f"{size}: ok ({o['seconds']} s)"
        why = brief(o["error"], 90) if o["error"] else "answered, but not with the code"
        return f"{size}: failed ({why})"

    cut = any(o["cut"] for o in outcome)
    ok = len(outcome) == len(sizes) and all(o["found"] and not o["cut"] for o in outcome)
    name = "long context" if full else f"prompt longer than numCtx ({num_ctx:,})"
    worked = [o["approx_tokens"] for o in outcome if o["found"] and not o["cut"]]
    if cut:
        hint = (f" - if the model runs on Ollama, numCtx ({num_ctx:,}) is the limit; our "
                "data descriptions will need roughly 20,000-40,000 tokens, so it must be raised")
    elif not ok:
        hint = (f" - the largest prompt that worked was ~{max(worked):,} tokens"
                if worked else " - even the smallest prompt failed")
    else:
        hint = ""
    record(g, name, FAIL if cut else (PASS if ok else WARN),
           "; ".join(describe(o) for o in outcome) + hint, runs=outcome)


# --------------------------------------------------------------------------
# Grafana
# --------------------------------------------------------------------------

class Grafana:
    def __init__(self, args):
        self.url = args.grafana_url.rstrip("/")
        self.insecure = args.insecure
        self.headers = {}
        if args.grafana_token:
            self.headers["Authorization"] = f"Bearer {args.grafana_token}"
        elif args.grafana_user:
            pair = f"{args.grafana_user}:{args.grafana_password or ''}".encode()
            self.headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()

    def call(self, method, path, payload=None) -> HttpResult:
        return http(method, self.url + path, self.headers, payload=payload,
                    timeout=30, insecure=self.insecure)


def check_grafana(args, ctx) -> None:
    g = "grafana"
    gf = Grafana(args)
    r = gf.call("GET", "/api/health")
    if not (r.ok and isinstance(r.body, dict)):
        record(g, "health", FAIL, f"{gf.url} - {r.describe()}")
        return
    version = r.body.get("version")
    record(g, "health", PASS,
           f"Grafana {version} at {gf.url}, database {r.body.get('database')} "
           "(tip: pin this version in the container instead of 'latest')")
    if not gf.headers:
        record(g, "login", SKIP, "set GRAFANA_TOKEN, or GRAFANA_USER and GRAFANA_PASSWORD")
        return
    r = gf.call("GET", "/api/org")
    if not r.ok:
        record(g, "login", FAIL, r.describe())
        return
    record(g, "login", PASS, f"logged in to organisation {(r.body or {}).get('name')!r}")

    r = gf.call("GET", "/api/datasources")
    if not (r.ok and isinstance(r.body, list)):
        record(g, "ClickHouse data source", FAIL, r.describe())
        return
    sources = [d for d in r.body if "clickhouse" in str(d.get("type", "")).lower()]
    if not sources:
        record(g, "ClickHouse data source", FAIL,
               f"none found among {len(r.body)} data source(s)")
        return
    ds = sources[0]
    record(g, "ClickHouse data source", PASS,
           f"{len(sources)} found; using {ds.get('name')!r} (type {ds.get('type')}, "
           f"uid {ds.get('uid')})",
           datasources=[{k: d.get(k) for k in ("name", "type", "uid", "isDefault")}
                        for d in sources])

    r = gf.call("GET", f"/api/plugins/{ds.get('type')}/settings")
    if r.ok and isinstance(r.body, dict):
        record(g, "plugin version", INFO,
               f"{ds.get('type')} {(r.body.get('info') or {}).get('version')}")

    def ds_query(sql):
        payload = {"queries": [{"refId": "A",
                                "datasource": {"type": ds.get("type"), "uid": ds.get("uid")},
                                "rawSql": sql, "format": 1, "queryType": "table",
                                "editorType": "sql"}],
                   "from": "now-5m", "to": "now"}
        res = gf.call("POST", "/api/ds/query", payload)
        frames = (((res.body or {}).get("results") or {}).get("A") or {}) \
            if isinstance(res.body, dict) else {}
        return res, frames

    r, frames = ds_query("SELECT 1 AS ok")
    if r.ok and frames.get("frames") and not frames.get("error"):
        detail = "Grafana ran SQL on ClickHouse"
        if re.fullmatch(r"[A-Za-z0-9_]+", args.ch_database):
            r2, f2 = ds_query(f"SELECT count() AS row_count FROM "
                              f"{args.ch_database}.linux_block_1_stats")
            if r2.ok and f2.get("frames") and not f2.get("error"):
                try:
                    count = f2["frames"][0]["data"]["values"][0][0]
                    detail += f"; linux_block_1_stats has {count:,} rows"
                except (KeyError, IndexError, TypeError):
                    pass
            else:
                detail += (f"; reading {args.ch_database}.linux_block_1_stats failed: "
                           f"{brief(f2.get('error') or r2.describe(), 200)}")
        record(g, C_GQUERY, PASS, detail)
    else:
        record(g, C_GQUERY, FAIL,
               f"{brief(frames.get('error') or r.describe(), 300)} - this tells us how "
               "the query JSON for panels must look", response=r.body)

    if not args.grafana_write_test:
        record(g, "write test", SKIP,
               "not run (add --grafana-write-test; creates and deletes a test folder "
               "and dashboard)")
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    r = gf.call("POST", "/api/folders", {"title": f"analysis-fw-check-{stamp}"})
    if not (r.ok and isinstance(r.body, dict)):
        record(g, "write test", FAIL, f"could not create a folder: {r.describe()}")
        return
    folder_uid = r.body.get("uid")
    dashboard = {"title": f"analysis-fw check {stamp}", "time": {"from": "now-1h", "to": "now"},
                 "panels": [{"id": 1, "type": "table", "title": "ClickHouse version",
                             "gridPos": {"h": 6, "w": 12, "x": 0, "y": 0},
                             "datasource": {"type": ds.get("type"), "uid": ds.get("uid")},
                             "targets": [{"refId": "A",
                                          "datasource": {"type": ds.get("type"),
                                                         "uid": ds.get("uid")},
                                          "rawSql": "SELECT version() AS clickhouse_version",
                                          "format": 1, "queryType": "table",
                                          "editorType": "sql"}]}]}
    r = gf.call("POST", "/api/dashboards/db",
                {"dashboard": dashboard, "folderUid": folder_uid, "overwrite": False})
    if not (r.ok and isinstance(r.body, dict)):
        record(g, "write test", FAIL, f"folder created, dashboard failed: {r.describe()}")
        gf.call("DELETE", f"/api/folders/{folder_uid}")
        return
    link = gf.url + str(r.body.get("url", ""))
    if args.keep_test_dashboard:
        record(g, "write test", PASS,
               f"created a folder and a dashboard; open {link} to check the panel shows "
               "the ClickHouse version")
        return
    d1 = gf.call("DELETE", f"/api/dashboards/uid/{r.body.get('uid')}")
    d2 = gf.call("DELETE", f"/api/folders/{folder_uid}")
    record(g, "write test", PASS if d1.ok and d2.ok else WARN,
           "created and deleted a test folder and dashboard"
           + ("" if d1.ok and d2.ok else
              f" - clean-up failed, delete folder 'analysis-fw-check-{stamp}' by hand"))


# --------------------------------------------------------------------------
# summary and entry point
# --------------------------------------------------------------------------

def summarize() -> dict:
    status = {r["check"]: r["status"] for r in RESULTS}
    counts = {s: sum(1 for r in RESULTS if r["status"] == s)
              for s in (PASS, FAIL, WARN, INFO, SKIP)}
    print("\n== Summary " + "=" * 51)
    print("  " + "  ".join(f"{k} {v}" for k, v in counts.items()))
    for r in RESULTS:
        if r["status"] in (FAIL, WARN):
            print(f"  [{r['status']}] {r['group']}: {r['check']}")

    def combine(parts):  # each part: PASS, FAIL, or None (not checked)
        if all(p == PASS for p in parts):
            return "possible"
        if any(p == FAIL for p in parts):
            return "blocked"
        return "not checked"

    def either(a, b):
        if PASS in (a, b):
            return PASS
        return FAIL if FAIL in (a, b) else None

    group_of = {C_TOOL: "llm", C_LOOP: "llm", C_LONG: "llm", C_JSON: "llm",
                C_GQUERY: "grafana"}

    def s(name):
        v = status.get(name)
        if v in (PASS, FAIL):
            return v
        if v == WARN:
            return FAIL
        # never reached: blocked if its service failed earlier, else not checked
        failed = any(r["group"] == group_of.get(name) and r["status"] == FAIL
                     for r in RESULTS)
        return FAIL if failed else None

    path1 = combine([s(C_TOOL), s(C_LOOP), s(C_LONG)])
    path2_llm = either(s(C_JSON), s(C_TOOL))
    path2 = combine([path2_llm, s(C_GQUERY)])
    verdict = {"path1_genbi": path1, "path2_grafana": path2,
               "path2_llm_side": {PASS: "possible", FAIL: "blocked"}.get(path2_llm, "not checked"),
               "running_total_rule": status.get(C_RULE, "not checked")}
    words = {"possible": "looks possible",
             "blocked": "blocked - see the FAIL/WARN lines above",
             "not checked": "not checked (some checks were skipped)"}
    print("\n  Path 1 (WrenAI GenBI, needs tool calling): " + words[path1])
    print("  Path 2 (Grafana panels, no tool calling needed): " + words[path2]
          + (f"; LLM side {verdict['path2_llm_side']}" if path2 != "possible" else ""))
    if C_RULE in status:
        print("  Model followed the running-total rule: "
              + ("yes" if status[C_RULE] == PASS else "no or unclear - see the SQL above"))
    return verdict


def parse_args(argv):
    # the settings file supplies defaults; environment variables and options override it
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    path = pre.parse_known_args(argv)[0].config
    cfg, cfg_error = None, None
    try:
        cfg = llm_config.load(path)
    except llm_config.ConfigError as exc:
        cfg_error = str(exc)
    chc = (cfg or {}).get("clickhouse") or {}
    gfc = (cfg or {}).get("grafana") or {}

    def e(env, value, default):
        return os.environ.get(env, str(value) if value not in (None, "") else default)

    p = argparse.ArgumentParser(description="Phase 1 readiness check for the office box.")
    a = p.add_argument
    a("--config", help=f"settings file (default: {llm_config.DEFAULT_PATH})")
    a("--llm-timeout", type=float, default=None,
      help="timeout in seconds for the heavier LLM checks (default: the larger of "
           "timeoutSeconds and 120)")
    a("--ch-url", default=e("CH_URL", chc.get("url"), ""))
    a("--ch-host", default=e("CH_HOST", chc.get("host"), "localhost"))
    a("--ch-port", default=e("CH_PORT", chc.get("port"), "8123"))
    a("--ch-user", default=e("CH_USER", chc.get("user"), "default"))
    a("--ch-password", default=e("CH_PASSWORD", chc.get("password"), ""))
    a("--ch-database", default=e("CH_DATABASE", chc.get("database"), "profile_fw"))
    a("--grafana-url", default=e("GRAFANA_URL", gfc.get("url"), "http://localhost:3000"))
    a("--grafana-token", default=e("GRAFANA_TOKEN", gfc.get("token"), ""))
    a("--grafana-user", default=e("GRAFANA_USER", gfc.get("user"), ""))
    a("--grafana-password", default=e("GRAFANA_PASSWORD", gfc.get("password"), ""))
    a("--insecure", action="store_true",
      help="skip TLS certificate checks (self-signed internal certificates)")
    a("--mock-sql", action="store_true",
      help="in the text-to-SQL test, do not run the model's SQL on ClickHouse")
    a("--max-steps", type=int, default=8)
    a("--context-test", action="store_true",
      help="also test long prompts, up to 85%% of maxContextTokens (slow)")
    a("--context-sizes", default=None,
      help="approximate prompt sizes in tokens, comma-separated (overrides the defaults)")
    a("--context-timeout", type=float, default=300)
    a("--grafana-write-test", action="store_true",
      help="create, then delete, a test folder and dashboard")
    a("--keep-test-dashboard", action="store_true",
      help="with --grafana-write-test: keep the test dashboard to look at")
    a("--skip-llm", action="store_true")
    a("--skip-clickhouse", action="store_true")
    a("--skip-grafana", action="store_true")
    a("--out", default="phase1_check_report.json")
    a("--html-out", default="phase1_check_chart.html")
    args = p.parse_args(argv)
    args.cfg, args.cfg_error = cfg, cfg_error
    return args


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = parse_args(argv)
    started = datetime.now(timezone.utc)
    print(f"Phase 1 readiness check - {started:%Y-%m-%d %H:%M:%S} UTC on "
          f"{platform.node()} ({platform.system()} {platform.release()})")
    ctx: dict = {}
    sections = [("Machine", check_machine, False),
                ("ClickHouse", check_clickhouse, args.skip_clickhouse),
                ("LLM API", check_llm, args.skip_llm),
                ("Grafana", check_grafana, args.skip_grafana)]
    for title, fn, skipped in sections:
        section(title)
        if skipped:
            record(title.lower(), "all checks", SKIP, "skipped on request")
            continue
        try:
            fn(args, ctx) if fn is not check_machine else fn(args)
        except Exception as exc:  # never crash halfway: record and move on
            record(title.lower(), "unexpected error", FAIL, f"{type(exc).__name__}: {exc}")
    verdict = summarize()

    report = {
        "generated_utc": started.isoformat(timespec="seconds"),
        "host": platform.node(),
        "python": sys.version.split()[0],
        "settings": {  # no keys, passwords, tokens or header values
            "config_file": os.path.abspath(args.config or llm_config.DEFAULT_PATH),
            "llm": ctx.get("llm_summary") or {"not_loaded": args.cfg_error},
            "clickhouse": args.ch_url or f"{args.ch_host}:{args.ch_port}",
            "clickhouse_user": args.ch_user, "clickhouse_database": args.ch_database,
            "grafana_url": args.grafana_url,
            "grafana_auth": "token" if args.grafana_token
            else ("user/password" if args.grafana_user else "none"),
            "mock_sql": args.mock_sql, "context_test": args.context_test,
            "grafana_write_test": args.grafana_write_test,
        },
        "verdict": verdict,
        "results": RESULTS,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"\nReport written to {os.path.abspath(args.out)} - please send this file back.")
    return 1 if any(r["status"] == FAIL for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
