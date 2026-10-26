"""python -m analysis score: run a test set of requests, and compare each result with its
reference answer (docs/phase1-design.md, section 10).

Each request runs through the whole system - our LLM included, the meaning taken as
confirmed, nothing drawn unless asked - and its result is compared with the result of
the request's reference query, written by hand on the run's views. Every number column
of the reference must match one column of the result, value for value, in any order and
within 1 %, with the same number of rows. Column names and labels may differ: they are
words, which the person confirming the meaning reads. A test may also say which chart
the request names, and which parts of it must be reported as not available.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from .clickhouse import quote
from .runner import RULE, Outcome, Request, Runner
from .runs import RunError
from .steps import fmt, is_number

TOLERANCE = 0.01


@dataclass
class Test:
    id: str
    request: str
    reference: str                  # SQL on the run's views: <db> and {run:String}
    chart: str | None = None        # the chart the request names, if it names one
    not_available: list[str] = field(default_factory=list)   # words of parts to be refused
    note: str = ""


@dataclass
class Score:
    test: Test
    status: str                     # PASS · FAIL · STOP (the system stopped) · ERROR (the test)
    problems: list[str]
    outcome: Outcome | None = None


def load_tests(path) -> list[Test]:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RunError(f"cannot read the test set {path}: {exc}") from None
    tests = []
    for i, t in enumerate(doc or [], start=1):
        if not isinstance(t, dict) or not all(t.get(k) for k in ("id", "request", "reference")):
            raise RunError(f"{path}: test {i} needs an id, a request and a reference")
        tests.append(Test(id=str(t["id"]), request=" ".join(str(t["request"]).split()),
                          reference=str(t["reference"]).strip(),
                          chart=str(t["chart"]) if t.get("chart") else None,
                          not_available=[str(w) for w in t.get("not_available") or []],
                          note=" ".join(str(t.get("note") or "").split())))
    ids = [t.id for t in tests]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise RunError(f"{path}: test ids used more than once: {', '.join(twice)}")
    return tests


def _numbers(names, types, rows) -> dict[str, list[float]]:
    """Each number column's values, sorted, without the empty ones."""
    out = {}
    for i, (name, t) in enumerate(zip(names, types)):
        if is_number(t):
            out[name] = sorted(float(r[i]) for r in rows if r[i] is not None
                               and not (isinstance(r[i], float) and math.isnan(r[i])))
    return out


def _same(a: list[float], b: list[float]) -> bool:
    return len(a) == len(b) and all(math.isclose(x, y, rel_tol=TOLERANCE, abs_tol=1e-9)
                                    for x, y in zip(a, b))


def _describe(values: list[float]) -> str:
    if not values:
        return "no values"
    return (f"{len(values)} value(s) from {fmt(values[0])} to {fmt(values[-1])}, "
            f"sum {fmt(sum(values))}")


def compare(reference: tuple, result: tuple) -> list[str]:
    """What differs between the reference query's result and the system's: [] if they
    agree. Both are (column names, ClickHouse types, rows)."""
    problems = []
    if len(reference[2]) != len(result[2]):
        problems.append(f"{len(result[2])} row(s), the reference has {len(reference[2])}")
    want, have = _numbers(*reference), _numbers(*result)
    free = dict(have)
    for name, values in want.items():
        match = next((n for n, v in free.items() if _same(values, v)), None)
        if match is not None:
            free.pop(match)
            continue
        shown = "; ".join(f'"{n}": {_describe(v)}' for n, v in list(have.items())[:4])
        problems.append(f'no column of the result matches the reference\'s "{name}" '
                        f'({_describe(values)}) - the result has '
                        + (shown + ("; ..." if len(have) > 4 else "") if have
                           else "no number column"))
    return problems


def _reported(record: dict, word: str) -> bool:
    meaning = (record.get("steps") or {}).get("1_meaning") or {}
    return any(word.lower() in f"{n.get('part', '')} {n.get('reason', '')}".lower()
               for n in meaning.get("not_available") or [])


def score_one(runner: Runner, db, test: Test, run: str, database: str) -> Score:
    outcome = runner.ask(Request(run=run, text=test.request, db=database))
    if outcome.status not in ("drawn", "answered"):
        return Score(test, "STOP", [outcome.message], outcome)
    record = json.loads(Path(outcome.record).read_text(encoding="utf-8"))
    try:
        reference = db.query(test.reference.replace("<db>", quote(database)), {"run": run})
    except Exception as exc:  # noqa: BLE001 - the test's own query is wrong
        return Score(test, "ERROR", [f"the reference query failed: "
                                     f"{' '.join(str(exc).split())[:300]}"], outcome)
    problems = compare(reference, outcome.result)
    if test.chart and outcome.kind != test.chart:
        problems.append(f"shown as {outcome.kind}, but the request asks for {test.chart}")
    problems += [f'it did not report the "{w}" part as not available'
                 for w in test.not_available if not _reported(record, w)]
    return Score(test, "FAIL" if problems else "PASS", problems, outcome)


def report(scores: list[Score], source: str, run: str, database: str) -> str:
    """The score, then per test what was asked, what the system did, and what differs."""
    passed = sum(s.status == "PASS" for s in scores)
    out = [f"python -m analysis score {source} - {datetime.now():%Y-%m-%d %H:%M:%S}",
           f"run: {run} (database {database})",
           f"passed {passed} of {len(scores)}", ""]
    out += [f"  {s.status:<5} {s.test.id}" for s in scores]
    for s in scores:
        out += ["", RULE, f"{s.status}  {s.test.id}", RULE, f"request:      {s.test.request}"]
        if s.test.note:
            out.append(f"note:         {s.test.note}")
        out += [f"problem:      {p}" for p in s.problems]
        if s.outcome is not None and s.outcome.record:
            record = json.loads(Path(s.outcome.record).read_text(encoding="utf-8"))
            shown = ((record.get("steps") or {}).get("2_shown") or [""])[-1]
            out += [f"understood:   {line}" if i == 0 else f"              {line}"
                    for i, line in enumerate(shown.splitlines())]
            out.append(f"files:        {Path(s.outcome.record).name}"
                       + (f", {Path(s.outcome.llm_log).name}" if s.outcome.llm_log else ""))
        out += ["reference:"] + [f"  {line}" for line in s.test.reference.splitlines()]
    return "\n".join(out) + "\n"
