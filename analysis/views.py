"""The views of one run, and the data models made over them (docs/phase1-design.md,
section 5).

Most columns are running totals: adding up their samples is hundreds of times
too large. The per-interval view turns each one into its change since the previous
sample (<column>_delta) and adds interval_s, so a plain sum over a run is right and
the raw totals are never seen. Generated from the catalog, with only the columns the
run has (step 0); it lives in the run's own database as <table>_interval.

A table whose catalog groups are positional buckets labelled by a run-level table -
the latency histograms, labelled by latency_buckets (ADR-0011) - also gets a histogram
view, <table>_histograms: one row per sample, histogram and bucket, with the bucket's
range. A run-level table has no time column and no view; it is read as it is.

model_specs() is the one list of the run's data models: the WrenAI files, the prompts
for our LLM and the names a query may use are all made from it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .catalog import Catalog, Histogram, Metric
from .clickhouse import quote
from .runs import RunCheck, TableCheck

VIEW_SUFFIX = "_interval"
HISTOGRAM_SUFFIX = "_histograms"
LEADING = ("run_id", "hostname", "ts")     # every view starts with these
AS_IS = {"gauge", "setting", "interval", "timestamp", "label", "dimension"}
_NAME = re.compile(r"\b[a-z][a-z0-9_]*\b")


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


# ---- histograms ------------------------------------------------------------------

def histogram_view_name(table: str) -> str:
    return table + HISTOGRAM_SUFFIX


def usable_histograms(run: RunCheck, check: TableCheck, catalog: Catalog) -> list[Histogram]:
    """The table's histograms this run can label: all their bucket columns are usable,
    and the table that labels them is in the run."""
    have = {c.name for c in check.usable}
    return [h for h in catalog.tables[check.table].histograms
            if h.labels in run.tables and all(c in have for c in h.columns)]


def _labels_columns(catalog: Catalog, labels: str) -> tuple[str, str, str | None]:
    """The labelling table's index, label and threshold columns, found by catalog kind:
    the dimension is the bucket's position, the label its range, the setting its
    threshold."""
    cols = list(catalog.tables[labels].columns.values())
    index = next(c.name for c in cols if c.kind == "dimension")
    label = next(c.name for c in cols if c.kind == "label")
    threshold = next((c.name for c in cols if c.kind == "setting"), None)
    return index, label, threshold


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=\.)\s", text, maxsplit=1)[0]


def histogram_columns(table: str, hists: list[Histogram], catalog: Catalog) -> list[ViewColumn]:
    """The histogram view's columns. Its histogram column says which histograms there
    are, each with the first sentence of its catalog meaning."""
    shared, own = catalog.shared, catalog.tables[table].columns
    which = "; ".join(f"'{h.name}' - {_first_sentence(own[h.columns[0]].meaning).rstrip('.')}"
                      for h in hists) + "."
    return ([ViewColumn(n, shared[n].type, shared[n].meaning, shared[n].unit, shared[n].kind, n)
             for n in LEADING]
            + [ViewColumn("interval_s", "Nullable(Int64)",
                          "Seconds since the previous sample. Empty on a run's first sample.",
                          "seconds", "interval", ""),
               ViewColumn("histogram", "String", f"Which histogram the row belongs to: {which}",
                          "name", "label", ""),
               ViewColumn("bucket_index", "UInt16", "The bucket's position, 0 being the fastest: "
                          "show buckets in this order.", "position", "dimension", ""),
               ViewColumn("bucket", "String", "The bucket's latency range, ready for a chart "
                          "legend, for example 50-100 µs or > 1 s.", "text", "label", ""),
               ViewColumn("threshold_us", "UInt64", "The threshold that opens the bucket, in "
                          "microseconds; 0 for the open-ended first and last buckets.",
                          "microseconds", "setting", ""),
               ViewColumn("io_count_delta", "Nullable(Int64)", "I/Os counted in this bucket "
                          "since the previous sample.", "count", "counter", "")])


def create_histogram_view_sql(database: str, run: RunCheck, check: TableCheck,
                              catalog: Catalog) -> str | None:
    """The table's histogram view, or None when the run cannot label its histograms.
    Column p is the labelling table's bucket p, and the last column - the overflow,
    every I/O above the last threshold - is bucket n, n being the run's number of
    thresholds. The columns in between, which a run with fewer than 16 thresholds does
    not use, are left out (ADR-0011, catalog Q16)."""
    hists = usable_histograms(run, check, catalog)
    if not hists:
        return None
    labels = hists[0].labels
    hists = [h for h in hists if h.labels == labels]
    index, label, threshold = _labels_columns(catalog, labels)
    items = ",\n        ".join(
        f"('{h.name}', {p}, {len(h.columns) - 1}, v.{quote(col + '_delta')})"
        for h in hists for p, col in enumerate(h.columns))
    thr = f"b.{quote(threshold)}" if threshold else "toUInt64(0)"
    lt = f"{quote(database)}.{quote(labels)}"
    picked = ", ".join(quote(c) for c in (index, label, threshold) if c)
    return (
        f"CREATE OR REPLACE VIEW {quote(database)}.{quote(histogram_view_name(check.table))} AS\n"
        f"SELECT x.run_id AS run_id, x.hostname AS hostname, x.ts AS ts, x.interval_s AS interval_s,\n"
        f"       x.histogram AS histogram, b.{quote(index)} AS bucket_index, b.{quote(label)} AS bucket,\n"
        f"       {thr} AS threshold_us, x.io_count_delta AS io_count_delta\n"
        f"FROM (SELECT v.run_id AS run_id, v.hostname AS hostname, v.ts AS ts,\n"
        f"             v.interval_s AS interval_s, h.1 AS histogram, h.2 AS position,\n"
        f"             h.3 AS last, h.4 AS io_count_delta\n"
        f"      FROM {quote(database)}.{quote(view_name(check.table))} AS v\n"
        f"      ARRAY JOIN [\n        {items}] AS h) AS x\n"
        f"INNER JOIN (SELECT run_id, max({quote(index)}) AS n FROM {lt} GROUP BY run_id) AS nb\n"
        f"    ON nb.run_id = x.run_id\n"
        f"INNER JOIN (SELECT DISTINCT run_id, {picked} FROM {lt}) AS b\n"
        f"    ON b.run_id = x.run_id\n"
        f"   AND b.{quote(index)} = multiIf(x.position = x.last, nb.n, x.position < nb.n, "
        f"x.position, 65535)")


def verify_histogram_sql(database: str, run: RunCheck, check: TableCheck,
                         catalog: Catalog) -> tuple[str, str, list[str]] | None:
    """Two queries, with parameter {run:String}: per histogram, the I/Os in the view,
    and the I/Os in all its bucket columns. They differ only by I/Os in the columns the
    run does not use, which should hold none (catalog Q16)."""
    hists = usable_histograms(run, check, catalog)
    if not hists:
        return None

    def total(h: Histogram) -> str:
        return " + ".join(f"ifNull({quote(c + '_delta')}, 0)" for c in h.columns)

    view = (f"SELECT histogram, sum(io_count_delta) FROM {quote(database)}."
            f"{quote(histogram_view_name(check.table))} WHERE run_id = {{run:String}} "
            f"GROUP BY histogram ORDER BY histogram")
    raw = (f"SELECT " + ", ".join(f"sum({total(h)})" for h in hists)
           + f" FROM {quote(database)}.{quote(view_name(check.table))} WHERE run_id = {{run:String}}")
    return view, raw, [h.name for h in hists]


# ---- the run's data models -----------------------------------------------------------

@dataclass
class ModelSpec:
    """One data model of a run: what WrenAI describes and our LLM may query."""
    name: str                      # the model's name: the table's, or <table>_histograms
    source: str                    # what it reads in the run's database
    kind: str                      # "interval" (per sample) · "run" (for the run) · "histogram"
    table: str                     # the catalog table it comes from
    about: str                     # its description
    columns: list[ViewColumn]
    metrics: list[Metric] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)   # metrics this run lacks a column for


def run_level_columns(check: TableCheck, catalog: Catalog) -> list[ViewColumn]:
    s = catalog.shared["run_id"]
    return ([ViewColumn("run_id", s.type, s.meaning, s.unit, s.kind, "run_id")]
            + [ViewColumn(c.name, c.type, c.meaning, c.unit, c.kind, c.name) for c in check.usable])


def model_specs(run: RunCheck, catalog: Catalog,
                buckets: dict[str, list[tuple[int, str]]] | None = None) -> list[ModelSpec]:
    """The run's data models: one per sampled table, over its per-interval view; one per
    histogram view; one per run-level table, read as it is. `buckets` is, per labelling
    table, this run's buckets as (index, label) - run_models reads them; without it the
    histogram model does not list them."""
    out: list[ModelSpec] = []
    labelled: dict[str, list[str]] = {}         # labelling table -> its histogram models
    for table, check in sorted(run.tables.items()):
        entry = catalog.tables[table]
        if entry.level == "run":
            out.append(ModelSpec(table, table, "run", table,
                                 f"{entry.observes} Rows for the whole run, with no time column.",
                                 run_level_columns(check, catalog)))
            continue
        have_cols = {c.name for c in check.usable}
        collected = [h for h in entry.histograms if all(c in have_cols for c in h.columns)]
        hists = usable_histograms(run, check, catalog)
        listed = (buckets or {}).get(hists[0].labels) if hists else None
        if hists and buckets is not None and not listed:
            hists = []                     # the labelling table has no rows for this run
        # bucket columns are read through the histogram view, which labels them; unlabelled
        # they are counts of unknown ranges
        in_histograms = {c + "_delta" for h in collected for c in h.columns}
        columns = [c for c in view_columns(check, catalog) if c.name not in in_histograms]
        have = {c.name for c in columns}
        metrics, left_out = [], []
        for m in entry.metrics:
            needs = {n for n in _NAME.findall(m.formula) if n.endswith("_delta") or n == "interval_s"}
            (metrics if needs <= have else left_out).append(m)
        about = (f"{entry.observes} One row per sample of run {run.run}. Running totals appear "
                 f"only as their change since the previous sample (<column>_delta); interval_s "
                 f"is the seconds in between.")
        if hists:
            about += f" Its latency histograms are the data model {histogram_view_name(table)}."
        elif collected:
            about += (f" Its latency histograms ({', '.join(h.name for h in collected)}) cannot "
                      f"be shown: this run has no {collected[0].labels} rows to label their "
                      f"buckets (the profiler was started without -lat-buckets).")
        out.append(ModelSpec(table, view_name(table), "interval", table, about, columns, metrics,
                             [m.name for m in left_out]))
        if hists:
            text = (f"The latency histograms of {table}: one row per sample, histogram and "
                    f"bucket, each bucket labelled with its range from {hists[0].labels}. For a "
                    f"distribution, filter to one histogram, sum io_count_delta grouped by "
                    f"bucket_index and bucket, select bucket (not bucket_index) and order by "
                    f"bucket_index. The first bucket holds every I/O below the first "
                    f"threshold, the last every I/O above the last threshold.")
            if listed:
                text += (" This run's buckets (bucket_index: bucket): "
                         + ", ".join(f"{i}: {label}" for i, label in listed) + ".")
            out.append(ModelSpec(histogram_view_name(table), histogram_view_name(table),
                                 "histogram", table, text, histogram_columns(table, hists, catalog)))
            labelled.setdefault(hists[0].labels, []).append(histogram_view_name(table))
    for s in out:          # a labelling table points to where its ranges are already joined
        if s.kind == "run" and s.table in labelled:
            s.about += (" To show a histogram, use " + ", ".join(labelled[s.table])
                        + ", which labels each bucket with its range.")
    return out


def bucket_list_sql(database: str, labels: str, catalog: Catalog) -> str:
    """A run's buckets in order, as (index, label), with parameter {run:String}."""
    index, label, _ = _labels_columns(catalog, labels)
    return (f"SELECT DISTINCT {quote(index)}, {quote(label)} FROM {quote(database)}."
            f"{quote(labels)} WHERE run_id = {{run:String}} ORDER BY {quote(index)}")


def run_models(db, run: RunCheck, catalog: Catalog) -> list[ModelSpec]:
    """model_specs, with this run's buckets read from its database (db.rows)."""
    labels = sorted({h.labels for check in run.tables.values()
                     for h in usable_histograms(run, check, catalog)})
    buckets = {t: [(int(i), str(label)) for i, label in
                   db.rows(bucket_list_sql(run.database, t, catalog), {"run": run.run})]
               for t in labels}
    return model_specs(run, catalog, buckets)


def create_views_sql(run: RunCheck, catalog: Catalog) -> list[str]:
    """Every view the run's data models read, in the order they must be made: the
    per-interval views, then the histogram views over them."""
    sampled = [c for t, c in sorted(run.tables.items()) if catalog.tables[t].level != "run"]
    out = [create_view_sql(run.database, c, catalog) for c in sampled]
    return out + [s for s in (create_histogram_view_sql(run.database, run, c, catalog)
                              for c in sampled) if s]
