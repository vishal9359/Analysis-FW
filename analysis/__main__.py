"""python -m analysis - the Phase 1 pieces built so far, for the office box.

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
its database name is not used here. Only `prepare` writes, and only CREATE VIEW.
`plan` saves the data models it used to out/<run>/data_models.json, which
`plan-file` reads.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import catalog as catalog_mod
from .catalog import ROOT
from .checks import model_query_problems, planned_sql_problems
from .runs import RunCheck, RunError, check_run, run_name
from .views import create_view_sql, verify_sql, view_name
from .wren_project import PlanError, data_models, plan

OUT = ROOT / "out"
STOP_TYPES = "\nSTOP: the catalog was written for another proto version of these columns."


def _report(run: RunCheck) -> None:
    print(f"run {run.run} is in database {run.database}")
    for t in run.tables.values():
        print(f"  {t.table}: {len(t.usable)} usable column(s)")
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
    _report(run)
    if run.problems:
        print(STOP_TYPES)
        return 1
    return 0


def cmd_prepare(db, args, cat) -> int:
    run = check_run(db, args.run, cat, args.db)
    _report(run)
    if run.problems:
        print(STOP_TYPES)
        return 1
    failed = 0
    for t in run.tables.values():
        db.execute(create_view_sql(run.database, t, cat))
        print(f"\nview {run.database}.{view_name(t.table)}: ready")
        queries = verify_sql(run.database, t, cat)
        if queries is None:
            continue
        view_q, raw_q, counters = queries
        got = db.rows(view_q, {"run": args.run})
        want = db.rows(raw_q, {"run": args.run})
        n = len(counters)
        series = [tuple(r[:-n]) for r in got]          # (hostname, device ...) per row
        if series != [tuple(r[:-n]) for r in want]:
            print("  CHECK FAILED: the view and the table do not have the same series")
            failed += 1
            continue
        # a sum over no values is empty (None): no change at all
        bad = [(key, name, s, w) for key, g, w_row in zip(series, got, want)
               for name, s, w in zip(counters, g[-n:], w_row[-n:]) if (s or 0) != w]
        if bad:
            failed += 1
            print(f"  changes do NOT add up for {len(bad)} counter(s) - a reset or a gap:")
            for key, name, s, w in bad[:10]:
                print(f"    {key} {name}: sum of changes {s}, last minus first {w}")
        else:
            print(f"  sum of changes = last minus first, for all {n} counters "
                  f"(series: {', '.join(str(s) for s in series) or '-'})")
    return 1 if failed else 0


def cmd_plan(db, args, cat) -> int:
    run = check_run(db, args.run, cat, args.db)
    if run.problems:
        _report(run)
        print(STOP_TYPES)
        return 1
    manifest, left_out = data_models(run, cat)
    out = OUT / args.run
    out.mkdir(parents=True, exist_ok=True)
    (out / "data_models.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for table, names in left_out.items():
        print(f"metrics not available in this run ({table}): {', '.join(names)}")
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
    args = ap.parse_args(argv)

    if args.cmd == "plan-file":
        return cmd_plan_file(args)
    try:
        run = run_name(args.run)
    except RunError as exc:
        print(f"STOP: {exc}")
        return 1
    if run != args.run:
        print(f"run name: {run} (the last part of the path given)")
        args.run = run
    from .clickhouse import ClickHouse
    try:
        db = ClickHouse.from_config(args.config)
    except Exception as exc:  # config error, or the server is not reachable
        print(f"cannot reach ClickHouse (config/config.yaml, CH_HOST / CH_PORT): "
              f"{' '.join(str(exc).split())}")
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
