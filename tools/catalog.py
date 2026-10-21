#!/usr/bin/env python3
"""Check the Measurement Catalog (knowledge/catalog/*.yaml) against the protos.

    python tools/catalog.py check                  # every catalog file against its .proto
    python tools/catalog.py skeleton <stem> ...    # start a file for a new table

The catalog says what every column means (docs/phase1-design.md, section 4).
Column names must match what the loader writes to ClickHouse exactly, so both
commands take them from the loader's own schema code (src/registry.py), never
from a hand-written list.

`check` fails (exit 1) when a column is missing, unknown or listed twice, when
an entry is incomplete, when a metric formula uses a column that is not a
counter, or when the proto has changed since the file was written (its
schema_fingerprint differs) - so a description can never silently go stale.
`skeleton` writes a file with every column marked todo, and never overwrites.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from analysis.catalog import KINDS, STATUSES, described  # noqa: E402
from src.registry import build_schema  # noqa: E402

CATALOG = ROOT / "knowledge" / "catalog"
PROTOS = ROOT / "tests" / "sample_protos"
LOADER_COLUMNS = {"run_id", "ts", "record_id", "_loaded_at"}   # described in _common.yaml

HEADER = """# =============================================================================
#  Measurement Catalog - {stem}
# =============================================================================
#  What every column of this table means. The analysis reads this instead of
#  guessing from column names. How to review: knowledge/README.md. In short:
#    status: todo       something is missing - fill in each TODO
#    status: draft      pre-filled from documents and field names - please check
#    status: confirmed  checked by an engineer - set this when you are done
#  Answer the questions first: they decide how the data checks are written.
#
#  Written for: tests/sample_protos/{stem}.proto
#  Do not rename 'column' or 'columns' entries: they must match ClickHouse exactly.
#  Check this file against the proto any time:  python tools/catalog.py check
# =============================================================================
"""


class Flow(list):
    """A list written on one line: [a, b, c]."""


class Dumper(yaml.SafeDumper):
    pass


Dumper.add_representer(Flow, lambda d, v: d.represent_sequence(
    "tag:yaml.org,2002:seq", v, flow_style=True))


def schema_tables(stem: str, protos: Path):
    """The loader's view of a proto: {table: [(column, path)]} without the shared
    columns, plus the schema fingerprint."""
    s = build_schema(stem, protos / f"{stem}.proto")
    shared = {"_".join(p) for p in s.generic_fields} | LOADER_COLUMNS
    tables = {s.stem: [("_".join(p), p) for p in s.payload_fields]}
    for ch in s.children:
        paths = [p for level in ch.level_fields for p in level]
        tables[ch.table] = [("_".join(p), p) for p in paths if "_".join(p) not in shared]
    return tables, s.schema_version


def runconfig_tables():
    """The tables the loader writes from a run's sidecar files (src/runconfig.py) - no
    .proto - as {table: [(column, path)]}, and a fingerprint of their columns."""
    from src import runconfig
    cols = [(c.name, (c.name,)) for c in runconfig.COLUMNS if c.name not in LOADER_COLUMNS]
    text = "\n".join(f"{c.name} {c.ch_type}" for c in runconfig.COLUMNS)
    return ({runconfig.TABLE: cols},
            "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])


def histogram_problems(catalog_dir: Path) -> list[str]:
    """Across files: a group with `labels` names a described run-level table, and its
    columns are counters of its own table (the buckets are matched by position)."""
    from analysis import catalog as analysis_catalog
    cat = analysis_catalog.load(catalog_dir)
    problems = []
    for table in cat.tables.values():
        for h in table.histograms:
            labels = cat.tables.get(h.labels)
            if labels is None or labels.level != "run":
                problems.append(f"{table.name}: histogram {h.name!r} is labelled by {h.labels!r}, "
                                f"which is not a described run-level table")
            not_counters = [c for c in h.columns
                            if c not in table.columns or table.columns[c].kind != "counter"]
            if not_counters:
                problems.append(f"{table.name}: histogram {h.name!r}: not counters of this "
                                f"table: {', '.join(not_counters[:6])}")
    return problems


def entry_problems(where: str, e: dict) -> list[str]:
    out = [f"{where}: missing '{k}'" for k in ("meaning", "unit", "kind", "status") if k not in e]
    if e.get("kind") not in KINDS | {"TODO"}:
        out.append(f"{where}: kind {e.get('kind')!r} is not one of {sorted(KINDS)}")
    if e.get("status") not in STATUSES:
        out.append(f"{where}: status {e.get('status')!r} is not one of {sorted(STATUSES)}")
    elif e["status"] != "todo" and "TODO" in (str(e.get("meaning")), str(e.get("unit")),
                                               str(e.get("kind"))):
        out.append(f"{where}: status is {e['status']} but it still contains TODO")
    return out


def check_file(path: Path, protos: Path) -> tuple[list[str], str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    problems: list[str] = []
    if path.name.startswith("_"):            # shared columns: entries only
        for e in doc.get("columns") or []:
            problems += entry_problems(f"{path.name}: {e.get('column')}", e)
        return problems, f"{len(doc.get('columns') or [])} shared columns"

    if doc.get("source") == "src/runconfig.py":   # written by the loader, from a sidecar file
        stem, (expected, fingerprint) = "src/runconfig.py", runconfig_tables()
    else:
        stem = Path(str(doc.get("proto", ""))).stem
        try:
            expected, fingerprint = schema_tables(stem, protos)
        except Exception as exc:                 # proto missing or does not compile
            return [f"{path.name}: cannot read {stem}.proto: {exc}"], ""
    if doc.get("schema_fingerprint") != fingerprint:
        problems.append(
            f"{path.name}: {stem} has changed since this file was written "
            f"(file says {doc.get('schema_fingerprint')}, now {fingerprint}). Review the "
            f"descriptions, then set schema_fingerprint: {fingerprint}")

    kinds, statuses = {}, {s: 0 for s in STATUSES}
    tables_doc = {t.get("table"): t for t in doc.get("tables") or []}
    for table, cols in expected.items():
        want = [c for c, _ in cols]
        tdoc = tables_doc.pop(table, None)
        if tdoc is None:
            problems.append(f"{path.name}: table {table} is not described")
            continue
        seen: dict[str, int] = {}
        for name, e in described(tdoc):
            seen[name] = seen.get(name, 0) + 1
            kinds[name] = e.get("kind")
        missing = [c for c in want if c not in seen]
        unknown = [c for c in seen if c not in want]
        twice = [c for c, n in seen.items() if n > 1]
        for label, names in (("missing (in the proto, not described)", missing),
                             ("unknown (described, not in the proto)", unknown),
                             ("listed more than once", twice)):
            if names:
                problems.append(f"{path.name}: {table}: {len(names)} column(s) {label}: "
                                + ", ".join(names[:12]) + (" ..." if len(names) > 12 else ""))
        for e in tdoc.get("columns") or []:
            problems += entry_problems(f"{path.name}: {e.get('column')}", e)
            statuses[e.get("status")] = statuses.get(e.get("status"), 0) + 1
        for gr in tdoc.get("groups") or []:
            problems += entry_problems(f"{path.name}: group {gr.get('group')!r}", gr)
            statuses[gr.get("status")] = statuses.get(gr.get("status"), 0) + len(gr.get("columns") or [])
    for extra in tables_doc:
        problems.append(f"{path.name}: table {extra} is described but not produced by the proto")

    for m in doc.get("metrics") or []:
        for base in re.findall(r"\b([a-z0-9_]+)_delta\b", str(m.get("formula", ""))):
            if kinds.get(base) != "counter":
                problems.append(f"{path.name}: metric {m.get('metric')}: '{base}_delta' needs "
                                f"'{base}' to be a counter column")

    open_q = sum(1 for q in doc.get("questions") or [] if str(q.get("answer")) == "TODO")
    total = sum(len(c) for c in expected.values())
    summary = (f"{total} columns; {statuses.get('todo', 0)} todo, {statuses.get('draft', 0)} "
               f"draft, {statuses.get('confirmed', 0)} confirmed; {open_q} open question(s); "
               f"{len(doc.get('metrics') or [])} metric(s)")
    return problems, summary


def cmd_check(args) -> int:
    files = sorted(args.catalog.glob("*.yaml"))
    if not files:
        print(f"no catalog files in {args.catalog}")
        return 1
    failed = 0
    for path in files:
        problems, summary = check_file(path, args.protos)
        print(f"{'FAIL' if problems else 'OK  '}  {path.name}: {summary}")
        for p in problems:
            print(f"        - {p}")
        failed += bool(problems)
    print(f"\n{len(files) - failed} of {len(files)} catalog file(s) match their protos.")
    across = histogram_problems(args.catalog) if not failed else []
    for p in across:
        print(f"FAIL  {p}")
    return 1 if failed or across else 0


def cmd_skeleton(args) -> int:
    rc = 0
    for stem in args.stems:
        path = args.catalog / f"{stem}.yaml"
        if path.exists():
            print(f"skipped {path}: it already exists (skeleton never overwrites)")
            rc = 1
            continue
        tables, fingerprint = schema_tables(stem, args.protos)
        doc = {"table": stem, "proto": f"{stem}.proto", "schema_fingerprint": fingerprint}
        for key in ("collector", "component", "collected_from", "counts_since", "sampling",
                    "device_filter", "observes"):
            doc[key] = "TODO"
        doc.update({"status": "todo", "questions": [], "tables": [], "metrics": []})
        for table, cols in tables.items():
            singles, groups = [], {}
            for name, p in cols:
                if len(p) > 1:   # a flattened sub-message: one group per sub-message
                    groups.setdefault(p[0], []).append(name)
                else:
                    singles.append({"column": name, "meaning": "TODO", "unit": "TODO",
                                    "kind": "TODO", "status": "todo"})
            tdoc = {"table": table, "columns": singles}
            if groups:
                tdoc["groups"] = [{"group": g, "columns": Flow(n), "meaning": "TODO",
                                   "unit": "TODO", "kind": "TODO", "status": "todo"}
                                  for g, n in groups.items()]
            doc["tables"].append(tdoc)
        args.catalog.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER.format(stem=stem) + "\n" + yaml.dump(
            doc, Dumper=Dumper, sort_keys=False, width=92), encoding="utf-8")
        print(f"wrote {path} ({sum(len(c) for c in tables.values())} columns, all todo)")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check the Measurement Catalog against the protos.")
    ap.add_argument("--catalog", type=Path, default=CATALOG, help=f"default: {CATALOG}")
    ap.add_argument("--protos", type=Path, default=PROTOS, help=f"default: {PROTOS}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="check every catalog file against its proto")
    sk = sub.add_parser("skeleton", help="start a catalog file for new tables")
    sk.add_argument("stems", nargs="+", help="proto file name without .proto")
    args = ap.parse_args(argv)
    return cmd_check(args) if args.cmd == "check" else cmd_skeleton(args)


if __name__ == "__main__":
    raise SystemExit(main())
