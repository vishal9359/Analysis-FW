"""The per-interval view of one table in one run (docs/phase1-design.md, section 5).

Most columns are running totals: adding up their samples is hundreds of times
too large. The view turns each one into its change since the previous sample
(<column>_delta) and adds interval_s, so a plain sum over a run is right and the
raw totals are never seen. Generated from the catalog, with only the columns the
run has (step 0); it lives in the run's own database as <table>_interval.
"""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import Catalog
from .clickhouse import quote
from .runs import TableCheck

VIEW_SUFFIX = "_interval"
LEADING = ("run_id", "hostname", "ts")     # every view starts with these
AS_IS = {"gauge", "setting", "interval", "timestamp", "label", "dimension"}


@dataclass(frozen=True)
class ViewColumn:
    name: str        # the column's name in the view
    ch_type: str     # its ClickHouse type
    meaning: str
    unit: str
    kind: str        # catalog kind of what it shows; "interval" for interval_s
    source: str      # the catalog column it comes from ("" for interval_s)


def view_name(table: str) -> str:
    return table + VIEW_SUFFIX


def partition_keys(check: TableCheck) -> list[str]:
    """One series per run, machine and device (or queue, core ...): the change is
    always taken against the previous sample of the same series."""
    return ["run_id", "hostname"] + [c.name for c in check.usable
                                     if c.kind in ("label", "dimension")]


def view_columns(check: TableCheck, catalog: Catalog) -> list[ViewColumn]:
    """The view's columns, in order: what the WrenAI data model describes."""
    since = catalog.tables[check.table].counts_since
    out = [ViewColumn(n, catalog.shared[n].type, catalog.shared[n].meaning,
                      catalog.shared[n].unit, catalog.shared[n].kind, n) for n in LEADING]
    keys = partition_keys(check)
    out += [ViewColumn(c.name, c.type, c.meaning, c.unit, c.kind, c.name)
            for c in check.usable if c.name in keys and c.name not in LEADING]
    out.append(ViewColumn("interval_s", "Nullable(Int64)",
                          "Seconds since the previous sample. Empty on a run's first sample.",
                          "seconds", "interval", ""))
    for c in check.usable:
        if c.name in keys or c.name in LEADING:
            continue
        if c.kind == "counter":
            out.append(ViewColumn(
                f"{c.name}_delta", "Nullable(Int64)",
                f"Change since the previous sample - the activity in between. The running "
                f"total (counted since {since}) is: {c.meaning}", c.unit, "counter", c.name))
        elif c.kind in AS_IS:
            out.append(ViewColumn(c.name, c.type, c.meaning, c.unit, c.kind, c.name))
    return out


def create_view_sql(database: str, check: TableCheck, catalog: Catalog) -> str:
    """CREATE VIEW for one table of one run, replacing an older one so the view
    always follows the current catalog (a view is only a definition: replacing it
    is instant). A counter that drops (a reset) gives "no value" for that sample,
    never a negative change."""
    first_is_value = catalog.tables[check.table].counts_since == "profiler start"
    keys = ", ".join(partition_keys(check))
    outer, inner = [], []
    for v in view_columns(check, catalog):
        if v.name == "interval_s":
            outer.append("if(__rn = 1, NULL, nullIf(dateDiff('second', __prev_ts, ts), 0)) "
                         "AS interval_s")
        elif v.kind == "counter":
            cur, prev = f"toInt64({quote(v.source)})", f"__prev_{v.source}"
            inner.append(f"toInt64(lagInFrame({quote(v.source)}) OVER w) AS {prev}")
            # profiler counters start at zero, so the first sample is the activity so
            # far; a since-boot counter's first sample is not activity of this run
            first = cur if first_is_value else "NULL"
            outer.append(f"if(__rn = 1, {first}, if({cur} < {prev}, NULL, {cur} - {prev})) "
                         f"AS {quote(v.name)}")
        else:
            outer.append(quote(v.name))
    lagged = "".join(f",\n             {x}" for x in inner)
    return (
        f"CREATE OR REPLACE VIEW {quote(database)}.{quote(view_name(check.table))} AS\n"
        f"SELECT " + ",\n       ".join(outer) + "\n"
        f"FROM (SELECT *,\n"
        f"             lagInFrame(ts) OVER w AS __prev_ts,\n"
        f"             row_number() OVER wn AS __rn{lagged}\n"
        f"      FROM {quote(database)}.{quote(check.table)}\n"
        f"      WINDOW w  AS (PARTITION BY {keys} ORDER BY ts\n"
        f"                    ROWS BETWEEN 1 PRECEDING AND CURRENT ROW),\n"
        f"             wn AS (PARTITION BY {keys} ORDER BY ts))")


def verify_sql(database: str, check: TableCheck,
               catalog: Catalog) -> tuple[str, str, list[str]] | None:
    """Two queries, run with parameter {run:String}, whose rows must be equal: the
    sum of each counter's changes from the view, and what it must come to from the
    raw table - last minus first (or last, for a counter that starts at zero).
    A difference means a reset or a gap in the samples. Returns (view query, raw
    query, counter names), or None when the table has no counters."""
    counters = [c.name for c in check.usable if c.kind == "counter"]
    if not counters:
        return None
    first_is_value = catalog.tables[check.table].counts_since == "profiler start"
    group = [k for k in partition_keys(check) if k != "run_id"]
    by = ", ".join(quote(k) for k in group)
    sums = ", ".join(f"sum({quote(c + '_delta')})" for c in counters)
    expect = ", ".join(
        f"toInt64(argMax({quote(c)}, ts))" if first_is_value
        else f"toInt64(argMax({quote(c)}, ts)) - toInt64(argMin({quote(c)}, ts))"
        for c in counters)
    view = (f"SELECT {by}, {sums} FROM {quote(database)}.{quote(view_name(check.table))} "
            f"WHERE run_id = {{run:String}} GROUP BY {by} ORDER BY {by}")
    raw = (f"SELECT {by}, {expect} FROM {quote(database)}.{quote(check.table)} "
           f"WHERE run_id = {{run:String}} GROUP BY {by} ORDER BY {by}")
    return view, raw, counters
