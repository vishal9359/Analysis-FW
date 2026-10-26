"""python -m analysis ask: one request, from request.yaml to a Grafana panel
(docs/phase1-design.md, section 7).

    0  find the run's database; compare its columns with the catalog   runs.check_run
    1  understand the request                                          our LLM
    2  show the meaning in plain words; ask yes or no                  you
    3  write a query on the run's WrenAI data models                   our LLM
    4  turn it into ClickHouse SQL; check C1                           WrenAI dry_plan, checks
    5  make the run's views; run the SQL, read-only; check C2          ClickHouse, checks
    7  explain what the graph shows; check C3                          our LLM, checks
    6  draw it in Grafana, in the run's folder                         grafana.py

The explanation is written before the panel is saved, so the panel carries it. A
failure at step 4 or 5 goes back to step 3 with the reason, at most twice; then the
request stops with the reason - a wrong graph is never drawn as if it were right. Every
request leaves a record in out/<run>/: the request, the meaning, every query and why it
was refused, the checks, the result summary, the explanation and the link. Beside it,
<same name>.llm.txt holds every request sent to our LLM and its response, in full, and
out/<run>/summary.txt gains a short block per request - what was asked and understood,
each query and its checks, the result, the outcome - to read or share.
"""
from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from .catalog import ROOT, Catalog
from .checks import (explanation_problems, model_query_problems, planned_sql_problems,
                     result_problems, time_problems)
from .clickhouse import quote
from .grafana import GrafanaError, choose_panel
from .llm import LlmError
from .runs import RunError, check_run, run_name
from .steps import (Bounds, Meaning, StepError, bounds, explain_prompts, formulas,
                    is_number, is_time, parse_explanation, parse_meaning, parse_query,
                    plain_explanation, query_prompts, run_names, show, summarize,
                    summary_numbers, summary_text, understand_prompts, when)
from .views import create_views_sql, run_models
from .wren_project import PlanError, data_models, plan

OUT = ROOT / "out"
QUERY_RETRIES = 2              # step 3 is asked again at most twice (design section 7)
EXPLAIN_TRIES = 2              # then the explanation is written by code
RULE = "=" * 88


@dataclass
class Request:
    run: str                   # the run folder's name or path
    text: str                  # what to show, in plain words
    db: str | None = None      # the run's database, if it should not be searched for


def load_request(path, run: str | None = None, db: str | None = None) -> Request:
    """request.yaml: `run:` and `request:`, and optionally `db:` (requirement A2). A
    `run` or `db` given here (ask --run, --db) replaces the file's, so one request file
    serves any run."""
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RunError(f"cannot read the request file {path}: {exc}") from None
    if not isinstance(doc, dict) or not str(doc.get("request") or "").strip() \
            or not str(run or doc.get("run") or "").strip():
        raise RunError(f"{path} needs 'run:' (the run folder's name or path), or --run, and "
                       f"'request:' (what to show, in plain words)")
    db = db or doc.get("db")
    return Request(run=str(run or doc["run"]).strip(), text=" ".join(str(doc["request"]).split()),
                   db=str(db).strip() if db else None)


@dataclass
class Outcome:
    status: str                # drawn · answered (no Grafana) · declined · refused · stopped
    message: str
    record: Path | None = None
    link: str | None = None
    llm_log: Path | None = None    # every request to our LLM and its response
    summary: Path | None = None    # out/<run>/summary.txt, one block per request
    kind: str | None = None        # the panel the result suits: timeseries, bar, stat, table
    result: tuple | None = None    # (column names, types, rows) of the checked SQL's result

    @property
    def ok(self) -> bool:
        return self.status in ("drawn", "answered", "declined")


class Stop(Exception):
    """The request ends here, with a status and the reason."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def ask_user(shown: str) -> bool | str:
    """Step 2 on the command line: yes runs it; no lets you rephrase (A4, A6)."""
    print("\n" + shown)
    try:
        answer = input("Proceed? [y/n] ").strip().lower()
        if answer in ("y", "yes"):
            return True
        new = input("Rephrase the request, or press Enter to stop: ").strip()
    except EOFError:
        print()
        return False
    return new or False


def ask_nobody(shown: str) -> bool:
    """--yes: the meaning is printed and taken as confirmed (for scoring a test set)."""
    print("\n" + shown + "\nProceed? [y/n] y  (--yes)")
    return True


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].rstrip("-") or "request"


def summary_block(rec: dict, outcome: Outcome) -> str:
    """One request, from its record: what was asked and understood, each query with its
    checks, the result, the explanation and the outcome - the record and the LLM log cut
    down to what is needed to see what happened."""
    steps, req = rec.get("steps") or {}, rec.get("request") or {}
    out = [RULE, f"{rec.get('started', '')}  {outcome.status.upper()}  {req.get('request', '')}",
           RULE]

    def line(label: str, text) -> None:
        lines = str(text).splitlines() or [""]
        out.append(f"{label:<14}{lines[0]}")
        out.extend(f"{'':14}{rest}" for rest in lines[1:])

    def refused(problems) -> str:
        return "passed" if problems == "passed" else "refused - " + "; ".join(problems or [])

    line("request:", req.get("request", ""))
    if req.get("rephrased"):
        line("rephrased:", req["rephrased"])
    if rec.get("run"):
        line("run:", f"{rec['run']} (database {rec.get('database')})")
    for shown, answer in zip(steps.get("2_shown", []), steps.get("2_confirmed", [])):
        line("understood:", shown)
        line("answer:", "yes" if answer is True else
             f"rephrased: {answer}" if isinstance(answer, str) else "no")
    for i, q in enumerate(steps.get("3_queries", []), start=1):
        if "error" in q:
            line(f"query {i}:", f"(no query) {q['error']}")
            continue
        line(f"query {i}:", q.get("query", ""))
        line("  C1:", refused(q.get("c1")))
        c2 = q.get("c2")
        if isinstance(c2, dict):
            line("  C2:", "passed" + "".join(f"\n{n}" for n in c2.get("notes") or []))
            line("  SQL run:", q.get("planned", ""))
        elif c2 is not None:
            line("  C2:", refused(c2))
    res = steps.get("5_result")
    if res:
        columns = ", ".join(f"{n} ({t})" for n, t in zip(res.get("columns") or [],
                                                       res.get("types") or []))
        line("result:", f"{res.get('rows')} row(s): {columns}")
        line("summary:", res.get("summary", ""))
    for e in steps.get("7_explanations", []):
        if "text" in e:
            line("explanation:", f"{e['text']}\n(C3: {refused(e.get('c3'))})")
        elif "fallback" in e:
            line("explanation:", "written by code from the result summary: our LLM's "
                                 "failed check C3 twice")
        elif "error" in e:
            line("explanation:", f"(none) {e['error']}")
    if steps.get("6_grafana"):
        g = steps["6_grafana"]
        line("panel:", f"{g.get('panel')} - {g.get('link')}")
    line("outcome:", outcome.status if outcome.status in ("drawn", "answered") else
         f"{outcome.status}: {outcome.message}")
    if rec.get("error"):
        line("error:", rec["error"].strip().splitlines()[-1])
    calls = rec.get("llm_calls") or []
    if calls:
        line("LLM calls:", ", ".join(f"{c['step']} {c['seconds']} s"
                                     + ("" if c.get("ok") else " (failed)") for c in calls))
    line("files:", Path(outcome.record).name
         + (f", {Path(outcome.llm_log).name}" if outcome.llm_log else ""))
    return "\n".join(out)


def time_range(b: Bounds, types: list[str], rows: list[tuple]) -> tuple[datetime, datetime]:
    """The dashboard's time range: the result's own first and last time when it has a
    time column - one table can end before the run does - else the range asked for."""
    times = [r[i] for i, t in enumerate(types) if is_time(t) for r in rows
             if isinstance(r[i], datetime)]
    if times:
        start, end = max(b.start, min(times)), min(b.end, max(times))
        if start < end:
            return start, end
    return b.start, b.end


class Runner:
    def __init__(self, db, catalog: Catalog, llm, grafana=None,
                 confirm: Callable[[str], bool | str] = ask_user,
                 say: Callable[[str], None] = print, out: Path = OUT) -> None:
        self.db, self.catalog, self.llm, self.grafana = db, catalog, llm, grafana
        self.confirm, self.say, self.out = confirm, say, out

    def ask(self, request: Request) -> Outcome:
        rec: dict = {"request": {"run": request.run, "request": request.text, "db": request.db},
                     "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "steps": {}}
        stem = f"{datetime.now():%Y%m%d-%H%M%S}-{slug(request.text)}"
        run_id, log = request.run, None
        first_call = len(getattr(self.llm, "calls", []))      # one LLM may serve many requests
        try:
            run_id = run_name(request.run)
            log = self._start_llm_log(run_id, stem, request)
            outcome = self._ask(request, run_id, rec)
        except Stop as stop:
            outcome = Outcome(stop.status, str(stop))
        except (RunError, LlmError, GrafanaError) as exc:
            outcome = Outcome("stopped", str(exc))
        except Exception as exc:  # noqa: BLE001 - a bug: still keep the record and the log
            rec["error"] = traceback.format_exc()
            outcome = Outcome("stopped", f"an unexpected error, which is a bug: "
                                         f"{type(exc).__name__}: {exc}. The record holds the "
                                         f"details, and the LLM log what was asked so far")
        rec["llm_calls"] = [c.as_dict() for c in getattr(self.llm, "calls", [])[first_call:]]
        rec["llm_log"] = str(log) if log else None
        rec["outcome"] = {"status": outcome.status, "message": outcome.message,
                          "link": outcome.link}
        outcome.record = self._save(run_id, stem, rec)
        outcome.llm_log = log
        outcome.summary = self._summarize(run_id, rec, outcome)
        return outcome

    def _summarize(self, run_id: str, rec: dict, outcome: Outcome) -> Path:
        """Append the request's block to out/<run>/summary.txt: the record and the LLM
        log cut down to what is needed to see what happened - short enough to share."""
        path = self.out / (run_id or "unknown-run") / "summary.txt"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(summary_block(rec, outcome) + "\n")
        return path

    def _start_llm_log(self, run_id: str, stem: str, request: Request) -> Path | None:
        """<stem>.llm.txt beside the record: each request to our LLM and its response,
        written as it happens - so it is there even if the request stops half-way."""
        if not hasattr(self.llm, "log_to"):          # a stand-in for our LLM, in tests
            return None
        path = self.out / (run_id or "unknown-run") / f"{stem}.llm.txt"
        self.llm.log_to(path, f"python -m analysis ask - {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                              f"run: {run_id}\nrequest: {request.text}")
        return path

    # ---- the steps -----------------------------------------------------------------

    def _ask(self, request: Request, run_id: str, rec: dict) -> Outcome:
        run = check_run(self.db, run_id, self.catalog, request.db)           # step 0
        rec.update(run=run.run, database=run.database)
        self.say(f"run {run.run} is in database {run.database}")
        if run.problems:
            raise Stop("stopped", "the catalog was written for another proto version: "
                       + "; ".join(run.problems))
        if not run.tables:
            raise Stop("refused", "this run has none of the described tables")
        span = self._span(run)
        specs = run_models(self.db, run, self.catalog)
        manifest = data_models(run, specs)
        names = run_names(specs)
        kinds = {s.name: s.kind for s in specs}
        rec["data_models"] = sorted(names)

        text = request.text
        while True:
            meaning = self._understand(text, run, span, specs, names, rec)      # step 1
            b = bounds(meaning.time, span)
            if meaning.measures and all(kinds[m.table] == "run" for m in meaning.measures):
                b = bounds({"kind": "whole_run"}, span)   # rows for the run: no time range
            rec["steps"]["1_meaning"] = meaning.as_dict()
            rec["steps"]["time_range"] = {"from": when(b.start), "to": when(b.end),
                                          "whole_run": b.whole, "words": b.words}
            if not meaning.answerable:
                parts = "; ".join(f"{n['part']} - {n['reason']}" for n in meaning.not_available)
                raise Stop("refused", "nothing in the request can be answered from the "
                           "described data" + (f": {parts}" if parts else ""))
            shown = show(meaning, run.run, b)
            rec["steps"].setdefault("2_shown", []).append(shown)
            answer = self.confirm(shown)                                         # step 2
            rec["steps"].setdefault("2_confirmed", []).append(answer)
            if answer is True:
                break
            if isinstance(answer, str) and answer.strip():
                text = " ".join(answer.split())
                rec["request"]["rephrased"] = text
                continue
            raise Stop("declined", "not run: the meaning was not confirmed")

        planned, names_out, types_out, rows, query, notes = \
            self._query(meaning, run, b, specs, manifest, rec)                   # steps 3-5
        summary = summarize(names_out, types_out, rows)
        rec["steps"]["5_result"] = {"rows": len(rows), "columns": names_out, "types": types_out,
                                    "summary": summary_text(summary, len(rows), query.units)}
        kind, kind_note = choose_panel(meaning.chart, types_out, len(rows))
        how = formulas(meaning, specs, self.catalog)
        explanation = self._explain(text, meaning, run.run, b, query.sql, summary, len(rows),
                                    query.units, how, names, rec)                 # step 7
        said = [f"Not shown: {n['part']} - {n['reason']}." for n in meaning.not_available]
        said += [f"Note: {n}." for n in notes]
        if kind_note:
            said.append(f"Note: {kind_note}.")
        description = " ".join([explanation] + said)
        self.say("\nClickHouse SQL (checked by C1; this is the panel's query):\n" + planned)
        self.say("\nC2: passed" + "".join(f"\n  {n}" for n in notes))
        self.say("\nWhat the graph shows (checked by C3):\n" + description)
        link = None
        if self.grafana is not None:                                             # step 6
            start, end = time_range(b, types_out, rows)
            link = self.grafana.publish(run=run.run, request=text, title=meaning.summary,
                                        description=description, sql=planned, kind=kind,
                                        names=names_out, types=types_out, units=query.units,
                                        start=start, end=end)
            rec["steps"]["6_grafana"] = {"panel": kind, "link": link,
                                         "time": {"from": when(start), "to": when(end)}}
            self.say(f"\nGrafana: {link}")
        return Outcome("drawn" if link else "answered", description, link=link, kind=kind,
                       result=(names_out, types_out, rows))

    def _span(self, run) -> tuple[datetime, datetime]:
        """The run's first and last sample over its described sampled tables."""
        sampled = [t for t in sorted(run.tables) if self.catalog.tables[t].level != "run"]
        spans = [s for s in (self.db.time_span(run.database, t, run.run) for t in sampled) if s]
        if not spans:
            raise Stop("refused", f"run {run.run} has no samples in its described tables")
        return min(s[0] for s in spans), max(s[1] for s in spans)

    def _understand(self, text: str, run, span, specs, names, rec) -> Meaning:
        problem = None
        for _ in range(2):
            system, user = understand_prompts(text, run, span, specs, self.catalog, problem)
            obj = self.llm.ask_json("1 understand", system, user)
            rec["steps"].setdefault("1_answers", []).append(obj)
            try:
                return parse_meaning(obj, names)
            except StepError as exc:
                problem = str(exc)
                self.say(f"our LLM's reading could not be used ({problem}); asking again")
        raise Stop("stopped", f"our LLM's reading of the request could not be used: {problem}")

    def _query(self, meaning: Meaning, run, b: Bounds, specs, manifest, rec):
        allowed = {(m["tableReference"]["schema"], m["tableReference"]["table"])
                   for m in manifest["models"]}
        models = {m["name"] for m in manifest["models"]}
        refused: list[tuple[str, str]] = []
        tries = rec["steps"]["3_queries"] = []
        views_made = False
        for _ in range(1 + QUERY_RETRIES):
            system, user = query_prompts(meaning, run, b, specs, self.catalog, refused)
            try:
                query = parse_query(self.llm.ask_json("3 write query", system, user))
            except StepError as exc:
                tries.append({"error": str(exc)})
                refused.append(("(no query)", str(exc)))
                continue
            entry: dict = {"query": query.sql}
            tries.append(entry)
            problems = model_query_problems(query.sql, models, run.run)          # C1
            if not b.whole:
                problems += time_problems(query.sql, when(b.start), when(b.end))
            if not problems:
                try:
                    planned = plan(manifest, query.sql)                          # step 4
                except PlanError as exc:
                    problems = [f"WrenAI refused it: {exc}"]
                else:
                    entry["planned"] = planned
                    problems = planned_sql_problems(planned, allowed)            # C1
            if problems:
                entry["c1"] = problems
                refused.append((query.sql, "; ".join(problems)))
                self.say("C1 refused the query: " + "; ".join(problems))
                continue
            entry["c1"] = "passed"
            if not views_made:                                                   # step 5
                for sql in create_views_sql(run, self.catalog):
                    self.db.execute(sql)
                views_made = True
            names_out, types_out, rows = self.db.query(planned)
            problems, notes = result_problems(names_out, [is_number(t) for t in types_out], rows)
            entry["rows"] = len(rows)
            if not rows:
                entry["c2"] = problems
                raise Stop("stopped", self._why_empty(run, b, meaning, specs))
            if problems:                                                         # C2
                entry["c2"] = problems
                refused.append((query.sql, "the result cannot be right: " + "; ".join(problems)))
                self.say("C2 refused the result: " + "; ".join(problems))
                continue
            entry["c2"] = {"passed": True, "notes": notes}
            return planned, names_out, types_out, rows, query, notes
        raise Stop("stopped", f"no query passed the checks in {1 + QUERY_RETRIES} tries; the "
                   f"last was refused because {refused[-1][1]}")

    def _why_empty(self, run, b: Bounds, meaning: Meaning, specs) -> str:
        """C2: an empty result is explained, never drawn as a blank graph."""
        by_name = {s.name: s for s in specs}
        counts = []
        for name in sorted({m.table for m in meaning.measures}):
            s = by_name[name]
            source = f"{quote(run.database)}.{quote(s.source)}"
            if s.kind == "run":
                n = self.db.rows(f"SELECT count() FROM {source} WHERE run_id = {{run:String}}",
                                 {"run": run.run})[0][0]
                counts.append((name, n, "row(s) for the run"))
                continue
            n = self.db.rows(
                f"SELECT uniqExact(ts) FROM {source} "
                "WHERE run_id = {run:String} AND ts >= toDateTime({a:String}, 'UTC') "
                "AND ts <= toDateTime({b:String}, 'UTC')",
                {"run": run.run, "a": when(b.start), "b": when(b.end)})[0][0]
            counts.append((name, n, "sample(s) there"))
        where = "; ".join(f"{t} has {n} {what}" for t, n, what in counts)
        reason = ("so the measure had no value in them - for example no I/O"
                  if any(n for _, n, _ in counts) else "so there is nothing to show in that time range")
        return f"the query returned no rows for {b.describe()}: {where}, {reason}. Nothing was drawn."

    def _explain(self, text: str, meaning: Meaning, run: str, b: Bounds, sql: str, summary,
                 rows: int, units: dict, how: list[str], names: dict, rec: dict) -> str:
        stext = summary_text(summary, rows, units)
        results = summary_numbers(summary) + [float(rows)]
        labels = [t for s in summary for t in (s.texts or [])]      # e.g. a bar chart's buckets
        context = " ".join([run, b.describe(), text, meaning.summary, *meaning.layers,
                            *meaning.assumptions, sql, *how, *labels])
        everything = set().union(*names.values()) if names else set()
        tries = rec["steps"]["7_explanations"] = []
        problem = None
        for _ in range(EXPLAIN_TRIES):
            system, user = explain_prompts(text, meaning, run, b, sql, stext, how, problem)
            try:
                explanation = parse_explanation(self.llm.ask_json("7 explain", system, user))
            except StepError as exc:
                problem = str(exc)
                tries.append({"error": problem})
                continue
            problems = explanation_problems(explanation, results, context, everything, sql)
            tries.append({"text": explanation, "c3": problems or "passed"})
            if not problems:
                return explanation
            problem = "; ".join(problems)
            self.say("C3 refused the explanation: " + problem)
        tries.append({"fallback": "written by code from the result summary"})
        return plain_explanation(meaning, run, b, summary, units, how)

    def _save(self, run_id: str, stem: str, rec: dict) -> Path:
        folder = self.out / (run_id or "unknown-run")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stem}.json"
        path.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
        return path
