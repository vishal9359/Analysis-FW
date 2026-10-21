"""Steps 1, 3 and 7 of a request (docs/phase1-design.md, section 7): what we send our
LLM, and how each answer is checked before anything uses it.

Code calculates, the AI explains. The LLM says what a request means (step 1), writes
one query on the run's WrenAI data models (step 3) and explains the result (step 7).
The prompts are built from the catalog and the run, never from a hand-written list of
names. Every answer becomes checked values here: a measure must be a metric or column
of this run's data models, time words become times clipped to the run, and the numbers
the explanation may quote come from a summary that code computes from the result.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .catalog import Catalog
from .runs import RunCheck
from .views import ModelSpec

CHARTS = ("timeseries", "bar", "stat", "table")
TIME_KINDS = ("whole_run", "last", "first", "between")
KIND_WORDS = {
    "counter": "change since the previous sample",
    "gauge": "a reading at the moment of sampling - use as is (avg, max), never subtract",
    "setting": "chosen when the profiler started, fixed for the run - show it, never add up",
    "interval": "seconds since the previous sample",
    "label": "a label - filter or show it",
    "dimension": "an identifier - group by it",
    "timestamp": "the sample's time, UTC",
}


class StepError(Exception):
    """The LLM's answer cannot be used; the message says why, and can be sent back."""


# ---- the run, described for our LLM -------------------------------------------------

def run_names(specs: list[ModelSpec]) -> dict[str, set[str]]:
    """{data model: the metric and column names a query may use} for this run."""
    return {s.name: {c.name for c in s.columns} | {m.name for m in s.metrics} for s in specs}


def describe_models(specs: list[ModelSpec], run: RunCheck, catalog: Catalog) -> str:
    """The data models as plain text: per model what it holds, its metrics, then its
    columns - columns that share one description (a catalog group, such as the size
    buckets) on one line, so the whole catalog fits one prompt."""
    out = []
    for s in specs:
        out.append(f"Data model {s.name} - {s.about}")
        if s.metrics:
            out.append("  Metrics - one value per sample; use them by name:")
            out += [f"    {m.name}: {m.meaning} Unit: {m.unit}." for m in s.metrics]
        out.append("  Columns:")
        own = catalog.tables[s.table].columns
        lines: list[tuple[str, list[str]]] = []
        for v in s.columns:
            if v.kind == "counter" and v.source in own:
                text = f"[{KIND_WORDS['counter']}] of: {own[v.source].meaning} Unit: {v.unit}."
            else:
                text = f"[{KIND_WORDS.get(v.kind, v.kind)}] {v.meaning} Unit: {v.unit}."
            if lines and lines[-1][0] == text:
                lines[-1][1].append(v.name)
            else:
                lines.append((text, [v.name]))
        out += [f"    {', '.join(names)}: {text}" for text, names in lines]
        check = run.tables.get(s.table)
        if s.kind == "interval" and check is not None and check.not_collected:
            out.append(f"  Not collected in this run: {', '.join(check.not_collected)}")
        out.append("")
    return "\n".join(out).rstrip()


def unavailable(run: RunCheck) -> str:
    """What the run has but no data model describes, so no query may use it."""
    parts = []
    if run.undescribed_tables:
        parts.append("In this run, but not described yet (no data model, cannot be used): "
                     + ", ".join(run.undescribed_tables))
    if run.missing_tables:
        parts.append("Described, but not in this run: " + ", ".join(run.missing_tables))
    return "\n".join(parts) or "Everything in this run is described."


# ---- time --------------------------------------------------------------------------

def when(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    parts = [f"{h} h"] * bool(h) + [f"{m} min"] * bool(m) + [f"{s} s"] * bool(s or not (h or m))
    return " ".join(parts)


@dataclass(frozen=True)
class Bounds:
    start: datetime
    end: datetime
    whole: bool                 # the whole run: no time filter is needed
    words: str                  # "the whole run", "the last 30 min of the run" ...

    def describe(self) -> str:
        return (f"{self.words} ({when(self.start)} to {when(self.end)} UTC, "
                f"{duration((self.end - self.start).total_seconds())})")


def bounds(time: dict, span: tuple[datetime, datetime]) -> Bounds:
    """The time range the meaning asks for, as two times inside the run. Time words in
    a request are within the run, never relative to the clock (requirement A3)."""
    first, last = span
    kind = str((time or {}).get("kind") or "whole_run")

    def secs(key: str) -> float:
        try:
            v = float((time or {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
        return v if math.isfinite(v) and v > 0 else 0.0

    start, end, words = first, last, "the whole run"
    if kind == "last" and secs("seconds"):
        start, words = max(first, last - timedelta(seconds=secs("seconds"))), \
            f"the last {duration(secs('seconds'))} of the run"
    elif kind == "first" and secs("seconds"):
        end, words = min(last, first + timedelta(seconds=secs("seconds"))), \
            f"the first {duration(secs('seconds'))} of the run"
    elif kind == "between":
        a, b = secs("from_s"), secs("to_s")
        if b > a:
            start, end = first + timedelta(seconds=a), min(last, first + timedelta(seconds=b))
            words = f"from {duration(a)} to {duration(b)} after the run's start"
    if start >= end:
        start, end, words = first, last, "the whole run"
    whole = start <= first and end >= last
    return Bounds(start, end, whole, "the whole run" if whole else words)


# ---- step 1: what the request means --------------------------------------------------

@dataclass
class Measure:
    table: str
    name: str
    words: str


@dataclass
class Meaning:
    answerable: bool
    summary: str
    measures: list[Measure]
    layers: list[str]
    time: dict
    chart: str
    assumptions: list[str]
    not_available: list[dict]

    def as_dict(self) -> dict:
        return asdict(self)


UNDERSTAND_SYSTEM = """\
You help storage engineers get graphs of profiling data. An engineer asks, in plain
words, for something to see. You decide what the request means, using only the data
described below. You do not write SQL in this step.

Reply with one JSON object and nothing else:
{
  "answerable": true,
  "summary": "<what the graph will show, in a few plain words, e.g. read and write IOPS, per second>",
  "measures": [{"table": "<data model name>", "name": "<metric or column name, exactly as listed>",
                "words": "<the measure in plain words>"}],
  "layers": ["<the layer and data model in plain words, e.g. block layer - completed requests (linux_block_1_stats)>"],
  "time": {"kind": "whole_run"},
  "chart": "timeseries",
  "assumptions": ["<each choice you made where the request could be read more than one way>"],
  "not_available": [{"part": "<part of the request>", "reason": "<not collected, or not described yet>"}]
}

Rules:
- Use only the data models, metrics and columns listed, with their exact names.
- Prefer a listed metric over a column when one matches the request.
- "time" is within the run, never the current clock: {"kind": "whole_run"}, or
  {"kind": "last", "seconds": N} for the last N seconds of the run, or
  {"kind": "first", "seconds": N}, or {"kind": "between", "from_s": A, "to_s": B} in
  seconds after the run's start.
- If the request can be read more than one way, choose the most sensible meaning and say
  which in "assumptions". Never choose silently.
- If part of the request needs data that is not listed - another layer, or a table that
  is not described yet - put that part in "not_available" and answer the rest. Set
  "answerable" to false only when nothing in the request can be answered.
- "chart": "timeseries" for values over time; "bar" for values per category or per
  bucket; "stat" for one number; "table" when the request asks for a table.
"""


def understand_prompts(request: str, run: RunCheck, span: tuple[datetime, datetime],
                       specs: list[ModelSpec], catalog: Catalog,
                       problem: str | None = None) -> tuple[str, str]:
    first, last = span
    user = (f"Run: {run.run}, from {when(first)} to {when(last)} UTC "
            f"({duration((last - first).total_seconds())}).\n\n"
            f"Data available for this run:\n{describe_models(specs, run, catalog)}\n\n"
            f"{unavailable(run)}\n\n"
            f"Request: {request}")
    if problem:
        user += f"\n\nYour previous answer could not be used: {problem}\nAnswer again."
    return UNDERSTAND_SYSTEM, user


def _texts(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v) for v in value or [] if str(v).strip()]


def parse_meaning(obj: dict, names: dict[str, set[str]]) -> Meaning:
    """Step 1's answer, checked: every measure is a metric or column of a data model
    of this run, the time is one we understand. Raises StepError."""
    problems, measures = [], []
    for m in obj.get("measures") or []:
        if not isinstance(m, dict):
            problems.append(f"a measure must be an object, got {m!r}")
            continue
        table, name = str(m.get("table") or ""), str(m.get("name") or "")
        if table not in names:
            problems.append(f"{table!r} is not a data model of this run "
                            f"(known: {', '.join(sorted(names))})")
        elif name not in names[table]:
            problems.append(f"{table}.{name} is not a metric or column of that data model")
        else:
            measures.append(Measure(table, name, str(m.get("words") or name)))
    answerable = obj.get("answerable", True) is not False
    if answerable and not measures and not problems:
        problems.append("the request is marked answerable, but no measure is given")
    time = obj.get("time") if isinstance(obj.get("time"), dict) else {"kind": "whole_run"}
    if str(time.get("kind") or "whole_run") not in TIME_KINDS:
        problems.append(f"time kind {time.get('kind')!r} is not one of {', '.join(TIME_KINDS)}")
    if problems:
        raise StepError("; ".join(problems))
    chart = str(obj.get("chart") or "timeseries").lower()
    not_available = [{"part": str(n.get("part") or ""), "reason": str(n.get("reason") or "")}
                     for n in obj.get("not_available") or [] if isinstance(n, dict)]
    return Meaning(answerable=answerable and bool(measures),
                   summary=str(obj.get("summary") or ", ".join(m.words for m in measures)),
                   measures=measures, layers=_texts(obj.get("layers")), time=time,
                   chart=chart if chart in CHARTS else "timeseries",
                   assumptions=_texts(obj.get("assumptions")), not_available=not_available)


def show(meaning: Meaning, run: str, b: Bounds) -> str:
    """Step 2: the meaning in plain words, before anything runs (requirement A4)."""
    chart = {"timeseries": "a time-series chart", "bar": "a bar chart",
             "stat": "a single number", "table": "a table"}[meaning.chart]
    lines = [f"I will show:  {meaning.summary}"]
    lines += [f"  {'from:' if i == 0 else '':12}{layer}" for i, layer in enumerate(meaning.layers)]
    lines.append(f"  {'run:':12}{run}, {b.describe()}")
    lines.append(f"  {'as:':12}{chart}")
    lines += [f"  {'assumed:' if i == 0 else '':12}{a}" for i, a in enumerate(meaning.assumptions)]
    lines += [f"  {'not shown:' if i == 0 else '':12}{n['part']} - {n['reason']}"
              for i, n in enumerate(meaning.not_available)]
    return "\n".join(lines)


# ---- step 3: the query on the data models ---------------------------------------------

@dataclass
class Query:
    sql: str
    chart: str
    units: dict[str, str]


QUERY_SYSTEM = """\
You write one SQL query for a graph, on the data models described below. A data model
is one table of profiling data with one row per sample - or, when its description says
so, rows for the whole run and no time column. Another program checks your query, turns
it into ClickHouse SQL, runs it and draws the graph.

Reply with one JSON object and nothing else:
{"sql": "<one SELECT statement>", "chart": "timeseries", "units": {"<output column>": "<its unit>"}}

Rules - a query that breaks one is refused:
1. Name tables by their data model name only, never with a database name in front.
2. Write every column with its table, e.g. linux_block_1_stats.read_iops, never read_iops alone.
3. No WITH blocks. One SELECT statement; a sub-query in FROM is allowed.
4. Always filter to the run: <data model>.run_id = '<the run>'. When a time range is given,
   also filter <data model>.ts with exactly the two times given, written as
   toDateTime('YYYY-MM-DD HH:MM:SS', 'UTC').
5. Running totals appear only as <column>_delta, the change since the previous sample. A
   total over a time range is sum(<column>_delta). A rate per second divides by interval_s.
   To change the time resolution, sum the _delta columns and interval_s in each group, then
   divide. Never use a raw running total.
6. A gauge - a reading at the moment - is used as is (avg, max, min); never subtract samples.
7. Use a listed metric (e.g. read_iops) when one matches, instead of writing its formula.
8. Give every output column an alias in plain words, in double quotes, e.g. AS "read IOPS".
   It becomes the graph's legend. For values over time select <data model>.ts AS ts and
   ORDER BY ts.
9. Shape: a time series is ts plus numbers; a bar chart is one text column plus numbers; a
   single number is one row of numbers. A latency distribution is a bar chart: the bucket
   column plus a number, ordered by bucket_index.
10. "units": for each output column, the unit from the descriptions, e.g. "I/O per second".
"""


def query_prompts(meaning: Meaning, run: RunCheck, b: Bounds, specs: list[ModelSpec],
                  catalog: Catalog, refused: list[tuple[str, str]] = ()) -> tuple[str, str]:
    time = ("Time range: the whole run - filter by run_id only." if b.whole else
            f"Time range: from {when(b.start)} to {when(b.end)} UTC - filter ts with "
            f"toDateTime('{when(b.start)}', 'UTC') and toDateTime('{when(b.end)}', 'UTC').")
    used = {m.table for m in meaning.measures}
    user = (f"Show: {meaning.summary}\n"
            f"Measures: " + "; ".join(f"{m.table}.{m.name} ({m.words})" for m in meaning.measures)
            + f"\nChart: {meaning.chart}\n"
            f"Assumed: {'; '.join(meaning.assumptions) or 'nothing'}\n"
            f"The run: {run.run}\n{time}\n\n"
            f"Data models:\n{describe_models([s for s in specs if s.name in used], run, catalog)}")
    for sql, reason in refused:
        user += f"\n\nThis query was refused:\n{sql}\nReason: {reason}"
    if refused:
        user += "\n\nWrite a corrected query."
    return QUERY_SYSTEM, user


def parse_query(obj: dict) -> Query:
    sql = obj.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise StepError("the answer has no \"sql\"")
    chart = str(obj.get("chart") or "timeseries").lower()
    units = obj.get("units") if isinstance(obj.get("units"), dict) else {}
    return Query(sql=sql.strip().rstrip(";"), chart=chart if chart in CHARTS else "timeseries",
                 units={str(k): str(v) for k, v in units.items()})


# ---- the result, summarized by code ---------------------------------------------------

def _base_type(t: str) -> str:
    for wrapper in ("Nullable(", "LowCardinality("):
        while t.startswith(wrapper) and t.endswith(")"):
            t = t[len(wrapper):-1]
    return t


def is_time(t: str) -> bool:
    return _base_type(t).startswith(("DateTime", "Date"))


def is_number(t: str) -> bool:
    return _base_type(t).startswith(("Int", "UInt", "Float", "Decimal"))


def fmt(x: float) -> str:
    """A number as a person reads it: 166,518 · 2.345 · 0.0123."""
    if isinstance(x, int) or (abs(x) >= 1000 and float(x).is_integer()):
        return f"{int(x):,}"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    if abs(x) >= 1:
        return f"{x:.3f}".rstrip("0").rstrip(".")
    return f"{x:.4g}"


@dataclass
class ColumnSummary:
    name: str
    type: str
    values: int                       # rows with a value
    empty: int                        # rows without one (NULL): no I/O in that second ...
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    first: str | None = None          # a time column: first and last value
    last: str | None = None
    texts: list[str] | None = None    # a text column: its values (up to 30)


def summarize(names: list[str], types: list[str], rows: list[tuple]) -> list[ColumnSummary]:
    out = []
    for i, (name, t) in enumerate(zip(names, types)):
        vals = [r[i] for r in rows]
        present = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
        s = ColumnSummary(name=name, type=t, values=len(present), empty=len(vals) - len(present))
        if is_time(t) and present:
            s.first, s.last = str(min(present)), str(max(present))
        elif is_number(t) and present:
            nums = [float(v) for v in present]
            s.minimum, s.maximum, s.mean = min(nums), max(nums), sum(nums) / len(nums)
            if all(isinstance(v, int) for v in present):
                s.minimum, s.maximum = int(s.minimum), int(s.maximum)
        elif present:
            s.texts = [str(v) for v in present[:30]]
        out.append(s)
    return out


def summary_text(summary: list[ColumnSummary], rows: int, units: dict[str, str]) -> str:
    lines = [f"rows: {rows}"]
    for s in summary:
        unit = f" ({units[s.name]})" if s.name in units else ""
        empty = f", {s.empty} empty" if s.empty else ""
        if s.first is not None:
            lines.append(f'"{s.name}": time, {s.first} to {s.last} UTC')
        elif s.mean is not None:
            only = s.values == 1
            lines.append(f'"{s.name}"{unit}: ' + (f"value {fmt(s.minimum)}" if only else
                         f"{s.values} values{empty}; min {fmt(s.minimum)}, max {fmt(s.maximum)}, "
                         f"mean {fmt(s.mean)}"))
        elif s.texts is not None:
            lines.append(f'"{s.name}": {", ".join(s.texts)}')
        else:
            lines.append(f'"{s.name}"{unit}: no values')
    return "\n".join(lines)


def summary_numbers(summary: list[ColumnSummary]) -> list[float]:
    """Every number the summary shows - what an explanation may quote (check C3)."""
    out = []
    for s in summary:
        out += [float(x) for x in (s.minimum, s.maximum, s.mean) if x is not None]
        out += [float(s.values), float(s.empty)]
        for text in s.texts or []:
            try:
                out.append(float(text))
            except ValueError:
                pass
    return out


# ---- step 7: what the graph shows ------------------------------------------------------

EXPLAIN_SYSTEM = """\
You explain a graph to a storage engineer, in 2 to 4 short sentences: what is plotted (the
measurement and the layer), the run and the time range, the units, and how the values
were calculated. Say only what the information below supports, and quote only numbers
that appear in the result summary. Do not explain why the values look the way they do.

Reply with one JSON object and nothing else: {"explanation": "<the sentences>"}
"""


def formulas(meaning: Meaning, specs: list[ModelSpec], catalog: Catalog) -> list[str]:
    """How each measure is calculated, from the catalog: the one definition."""
    by_name = {s.name: s for s in specs}
    out = []
    for m in meaning.measures:
        spec = by_name.get(m.table)
        if spec is None:
            continue
        own = catalog.tables[spec.table].columns
        metric = next((x for x in spec.metrics if x.name == m.name), None)
        column = next((c for c in spec.columns if c.name == m.name), None)
        if metric is not None:
            out.append(f"{m.name} = {metric.formula} ({metric.meaning})")
        elif column is not None and column.kind == "counter" and column.source in own:
            out.append(f"{m.name} = change since the previous sample of: "
                       f"{own[column.source].meaning}")
        elif column is not None:
            out.append(f"{m.name}: {column.meaning}")
    return out


def explain_prompts(request: str, meaning: Meaning, run: str, b: Bounds, sql: str,
                    summary: str, how: list[str], problem: str | None = None) -> tuple[str, str]:
    not_shown = "; ".join(f"{n['part']} ({n['reason']})" for n in meaning.not_available)
    user = (f"Request: {request}\n"
            f"Shows: {meaning.summary}\n"
            f"Layers: {'; '.join(meaning.layers)}\n"
            f"Run: {run}, {b.describe()}\n"
            f"Assumed: {'; '.join(meaning.assumptions) or 'nothing'}\n"
            f"Not shown: {not_shown or 'nothing'}\n"
            f"How the measures are calculated:\n  " + "\n  ".join(how) + "\n"
            f"Query:\n{sql}\n\n"
            f"Result summary:\n{summary}")
    if problem:
        user += f"\n\nYour previous explanation could not be used: {problem}\nWrite it again."
    return EXPLAIN_SYSTEM, user


def parse_explanation(obj: dict) -> str:
    text = obj.get("explanation")
    if not isinstance(text, str) or not text.strip():
        raise StepError("the answer has no \"explanation\"")
    return " ".join(text.split())


def plain_explanation(meaning: Meaning, run: str, b: Bounds, summary: list[ColumnSummary],
                      units: dict[str, str], how: list[str]) -> str:
    """An explanation written by code, used when our LLM's fails check C3 twice. It
    can only quote the summary, so it always passes."""
    parts = [f"{meaning.summary[:1].upper()}{meaning.summary[1:]}, from "
             f"{'; '.join(meaning.layers) or ', '.join(sorted({m.table for m in meaning.measures}))}, "
             f"for run {run}, {b.describe()}."]
    for s in summary:
        if s.mean is None:
            continue
        unit = f" {units[s.name]}" if s.name in units else ""
        parts.append(f"{s.name}: value {fmt(s.minimum)}{unit}." if s.values == 1 else
                     f"{s.name}: from {fmt(s.minimum)} to {fmt(s.maximum)}{unit}, "
                     f"mean {fmt(s.mean)}.")
    if how:
        parts.append("Calculated per sample as defined in the catalog: " + "; ".join(how) + ".")
    return " ".join(parts)


def as_json(value) -> str:
    return json.dumps(value, indent=2, default=str)
