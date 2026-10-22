"""python -m analysis - Phase 1: a plain-language request to a Grafana graph.

    python -m analysis ask <request.yaml>       the whole request: our LLM reads it, you
                                                confirm the meaning, WrenAI and the checks
                                                make the SQL, Grafana draws it. --run and
                                                --db replace the file's; --yes skips the
                                                question; --no-grafana stops before
                                                drawing. Settings: tools/config.json
    python -m analysis check-run <run>          step 0: where the run is, what it has
    python -m analysis prepare <run>            step 0, then make (or remake) the run's
                                                views, then check their changes add up
    python -m analysis plan <run> "<query>"     WrenAI turns a query written on the data
                                                models into ClickHouse SQL; check C1.
                                                Add --execute to also run it, read-only
    python -m analysis plan-file <file> "<query>"
                                                the same planning and checks from saved
                                                data models - no ClickHouse at all

<run> is the run folder's name, e.g. ProfileData-fio-20261008-101500, or its path:
only the last part is used, because the loader names a run after its folder. Each
command searches every database for the run; --db <name> looks in that one only.
Only the host and port come from config/config.yaml (CH_HOST / CH_PORT override) -
its database name is not used here. Only `prepare` and `ask` write, and only
CREATE VIEW. `plan` saves the data models it used to out/<run>/data_models.json,
which `plan-file` reads.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import catalog as catalog_mod
from .catalog import ROOT, Catalog
from .checks import model_query_problems, planned_sql_problems
from .runs import RunCheck, RunError, TableCheck, check_run, run_name
from .views import (bucket_list_sql, create_histogram_view_sql, create_view_sql,
                    histogram_view_name, run_models, usable_histograms, verify_histogram_sql,
                    verify_sql, view_name)
from .wren_project import PlanError, data_models, plan

OUT = ROOT / "out"
STOP_TYPES = "\nSTOP: the catalog was written for another proto version of these columns."


def _report(run: RunCheck, cat: Catalog) -> None:
    print(f"run {run.run} is in database {run.database}")
    for t in run.tables.values():
        whole = " - rows for the whole run" if cat.tables[t.table].level == "run" else ""
        print(f"  {t.table}: {len(t.usable)} usable column(s){whole}")
        hists = usable_histograms(run, t, cat)
        if hists:
            print(f"    histograms, labelled by {hists[0].labels}: "
                  f"{', '.join(h.name for h in hists)}")
        if t.not_collected:
            print(f"    not collected in this run: {', '.join(t.not_collected)}")
        if t.not_described:
            print(f"    not described yet: {', '.join(t.not_described)}")
        for w in t.wrong_type:
            print(f"    TYPE DIFFERS - {w}")
    if run.missing_tables:
        print(f"  described, but not in this run: {', '.join(run.missing_tables)}")
    if run.undescribed_tables:
        print(f"  in this run, not described yet: {', '.join(run.undescribed_tables)}")


def _plan_and_check(manifest: dict, query: str, run: str | None) -> str | None:
    """C1 on the query, WrenAI's dry_plan, C1 on WrenAI's SQL. The SQL, or None."""
    models = {m["name"] for m in manifest["models"]}
    problems = model_query_problems(query, models, run)
    if problems:
        print("C1 refused the query:\n  " + "\n  ".join(problems))
        return None
    try:
        sql = plan(manifest, query)
    except PlanError as exc:
        print(f"WrenAI refused the query: {exc}")
        return None
    allowed = {(m["tableReference"]["schema"], m["tableReference"]["table"])
               for m in manifest["models"]}
    print(f"ClickHouse SQL from WrenAI:\n{sql}\n")
    problems = planned_sql_problems(sql, allowed)
    if problems:
        print("C1 refused WrenAI's SQL:\n  " + "\n  ".join(problems))
        return None
    print("C1: passed")
    return sql


def cmd_check_run(db, args, cat) -> int:
    run = check_run(db, args.run, cat, args.db)
    _report(run, cat)
    if run.problems:
        print(STOP_TYPES)
        return 1
    return 0


def _check_view(db, run: RunCheck, t: TableCheck, cat: Catalog) -> bool:
    """The per-interval view against its table: the changes of every counter add up to
    last minus first. False (and why, printed) if not."""
    queries = verify_sql(run.database, t, cat)
    if queries is None:
        return True
    view_q, raw_q, counters = queries
    got = db.rows(view_q, {"run": run.run})
    want = db.rows(raw_q, {"run": run.run})
    n = len(counters)
    series = [tuple(r[:-n]) for r in got]              # (hostname, device ...) per row
    if series != [tuple(r[:-n]) for r in want]:
        print("  CHECK FAILED: the view and the table do not have the same series")
        return False
    # a sum over no values is empty (None): no change at all
    bad = [(key, name, s, w) for key, g, w_row in zip(series, got, want)
           for name, s, w in zip(counters, g[-n:], w_row[-n:]) if (s or 0) != w]
    if bad:
        print(f"  changes do NOT add up for {len(bad)} counter(s) - a reset or a gap:")
        for key, name, s, w in bad[:10]:
            print(f"    {key} {name}: sum of changes {s}, last minus first {w}")
        return False
    print(f"  sum of changes = last minus first, for all {n} counters "
          f"(series: {', '.join(str(s) for s in series) or '-'})")
    return True


def _check_histograms(db, run: RunCheck, t: TableCheck, cat: Catalog) -> None:
    """The histogram view against the bucket columns. With n thresholds a histogram
    uses n+1 of its 17 columns: the overflow column holds every I/O above the last
    threshold, and the columns before it from bw_opt<n>_and_opt<n+1> on are unused and
    stay 0 (catalog Q16). So every I/O should be in the view; a difference is printed
    as a warning that names the unused columns, as those I/Os are not in the view."""
    hists = usable_histograms(run, t, cat)
    labels = hists[0].labels
    buckets = db.rows(bucket_list_sql(run.database, labels, cat), {"run": run.run})
    if not buckets:
        print(f"  {labels} has no rows for this run, so its histograms cannot be labelled")
        return
    n = int(buckets[-1][0])                            # the run's number of thresholds
    print(f"  {len(buckets)} buckets, from {labels}: {buckets[0][1]} to {buckets[-1][1]}")
    view_q, raw_q, names = verify_histogram_sql(run.database, run, t, cat)
    got = {name: int(v or 0) for name, v in db.rows(view_q, {"run": run.run})}
    want = dict(zip(names, (int(v or 0) for v in db.rows(raw_q, {"run": run.run})[0])))
    for h in hists:
        g, w = got.get(h.name, 0), want[h.name]
        if g == w:
            print(f"  {h.name}: all {w:,} I/Os are in its {len(buckets)} buckets")
            continue
        unused = list(h.columns[n:-1])
        if unused:
            where = unused[0] if len(unused) == 1 else f"{unused[0]} to {unused[-1]}"
            why = (f"are in columns this run does not use ({where}; it set {n} "
                   f"thresholds), which should stay 0 (catalog Q16)")
        else:
            why = f"do not fit the run's {len(buckets)} buckets"
        print(f"  WARNING - {h.name}: {w - g:,} of {w:,} I/Os {why}. They are not in the "
              f"histogram.")


def cmd_prepare(db, args, cat) -> int:
    run = check_run(db, args.run, cat, args.db)
    _report(run, cat)
    if run.problems:
        print(STOP_TYPES)
        return 1
    failed = 0
    for t in run.tables.values():
        if cat.tables[t.table].level == "run":
            continue                                   # read as it is: no view
        db.execute(create_view_sql(run.database, t, cat))
        print(f"\nview {run.database}.{view_name(t.table)}: ready")
        failed += not _check_view(db, run, t, cat)
        histograms = create_histogram_view_sql(run.database, run, t, cat)
        if histograms is not None:                     # made over the view just made
            db.execute(histograms)
            print(f"\nview {run.database}.{histogram_view_name(t.table)}: ready")
            _check_histograms(db, run, t, cat)
    return 1 if failed else 0


def cmd_plan(db, args, cat) -> int:
    run = check_run(db, args.run, cat, args.db)
    if run.problems:
        _report(run, cat)
        print(STOP_TYPES)
        return 1
    specs = run_models(db, run, cat)
    manifest = data_models(run, specs)
    out = OUT / args.run
    out.mkdir(parents=True, exist_ok=True)
    (out / "data_models.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for s in specs:
        if s.left_out:
            print(f"metrics not available in this run ({s.name}): {', '.join(s.left_out)}")
    sql = _plan_and_check(manifest, args.query, args.run)
    if sql is None:
        return 1
    if args.execute:
        rows = db.rows(sql)
        print(f"\n{len(rows)} row(s)" + (", the first 20:" if len(rows) > 20 else ":"))
        for r in rows[:20]:
            print("  " + " | ".join(str(v) for v in r))
    return 0


def cmd_plan_file(args) -> int:
    manifest = json.loads(Path(args.file).read_text(encoding="utf-8"))
    return 0 if _plan_and_check(manifest, args.query, None) is not None else 1


def _connect(args):
    """ClickHouse at config/config.yaml's host and port, or None (the reason printed)."""
    from .clickhouse import ClickHouse
    try:
        return ClickHouse.from_config(args.config)
    except Exception as exc:  # config error, or the server is not reachable
        print(f"cannot reach ClickHouse (config/config.yaml, CH_HOST / CH_PORT): "
              f"{' '.join(str(exc).split())}")
        return None


def cmd_ask(args) -> int:
    from tools import llm_config

    from .grafana import Grafana, GrafanaError
    from .llm import Llm
    from .runner import Runner, ask_nobody, ask_user, load_request
    try:
        request = load_request(args.file, run=args.run, db=args.db)
    except RunError as exc:
        print(f"STOP: {exc}")
        return 1
    try:
        settings = llm_config.load(args.settings)
        llm = Llm(settings)
        grafana = None if args.no_grafana else Grafana.from_config(settings)
    except (llm_config.ConfigError, GrafanaError) as exc:
        print(f"settings ({args.settings or 'tools/config.json'}): {exc}")
        return 2
    db = _connect(args)
    if db is None:
        return 2
    try:
        runner = Runner(db, catalog_mod.load(), llm, grafana,
                        confirm=ask_nobody if args.yes else ask_user)
        outcome = runner.ask(request)
    finally:
        db.close()
    files = f"record: {outcome.record}" + (
        f"\nLLM log (each request to our LLM and its response): {outcome.llm_log}"
        if outcome.llm_log else "")
    if outcome.status in ("drawn", "answered"):
        print(f"\n{files}")
        return 0
    label = "NOT RUN" if outcome.status == "declined" else "STOP"
    print(f"\n{label}: {outcome.message}\n{files}")
    return 0 if outcome.status == "declined" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m analysis", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=None,
                    help="loader config, for the ClickHouse host and port only "
                         "(default: config/config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run_help = "the run folder's name, e.g. ProfileData-fio-20261008-101500, or its path"
    db_help = "the database that holds the run (default: search every database for it)"
    for name, text in (("check-run", "step 0 only - read-only"),
                       ("prepare", "step 0, then make the run's views and check them")):
        s = sub.add_parser(name, help=text)
        s.add_argument("run", help=run_help)
        s.add_argument("--db", help=db_help)
    p = sub.add_parser("plan", help="plan a query written on the data models, and check it")
    p.add_argument("run", help=run_help)
    p.add_argument("query", help="SQL on the data models, e.g. SELECT linux_block_1_stats.ts, "
                                 "linux_block_1_stats.read_iops FROM linux_block_1_stats WHERE "
                                 "linux_block_1_stats.run_id = '<run>'")
    p.add_argument("--db", help=db_help)
    p.add_argument("--execute", action="store_true", help="also run it (read-only)")
    f = sub.add_parser("plan-file", help="plan from saved data models, without ClickHouse")
    f.add_argument("file", help="a data_models.json saved by `plan`")
    f.add_argument("query")
    a = sub.add_parser("ask", help="answer a plain-language request from a request.yaml")
    a.add_argument("file", help="request.yaml: run: <run folder's name or path>, "
                                "request: <what to show>, and optionally db: <database>")
    a.add_argument("--run", help="the run, instead of the file's run: - " + run_help)
    a.add_argument("--db", help="the run's database, instead of the file's db: (default: "
                                "search every database for the run)")
    a.add_argument("--yes", action="store_true",
                   help="take the meaning as confirmed, without asking (for a test set)")
    a.add_argument("--no-grafana", action="store_true",
                   help="stop after the explanation; draw nothing")
    a.add_argument("--settings", default=None,
                   help="the LLM and Grafana settings (default: tools/config.json)")
    args = ap.parse_args(argv)

    if args.cmd == "plan-file":
        return cmd_plan_file(args)
    if args.cmd == "ask":
        return cmd_ask(args)
    try:
        run = run_name(args.run)
    except RunError as exc:
        print(f"STOP: {exc}")
        return 1
    if run != args.run:
        print(f"run name: {run} (the last part of the path given)")
        args.run = run
    db = _connect(args)
    if db is None:
        return 2
    try:
        cat = catalog_mod.load()
        return {"check-run": cmd_check_run, "prepare": cmd_prepare,
                "plan": cmd_plan}[args.cmd](db, args, cat)
    except RunError as exc:
        print(f"STOP: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
