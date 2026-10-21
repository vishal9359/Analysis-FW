"""Phase 1 tests: step 0, the per-run views and histogram views, the WrenAI data
models, check C1, and `prepare`.
Run:  python -m pytest tests/ -v

No ClickHouse needed: a fake database holds what the loader would create from
tests/sample_protos/ and the run's latency_bucket_options.yaml. The tests that call
WrenAI or sqlglot are skipped when wrenai is not installed (pip install -r
requirements.txt).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from analysis import catalog as catalog_mod  # noqa: E402
from analysis.runs import RunError, check_run, find_database, run_name  # noqa: E402
from analysis.views import (create_histogram_view_sql, create_view_sql,  # noqa: E402
                            create_views_sql, model_specs, partition_keys, usable_histograms,
                            verify_histogram_sql, verify_sql, view_columns)
from analysis.wren_project import WREN_TYPES, data_models  # noqa: E402
from src import runconfig  # noqa: E402
from src.registry import build_schema  # noqa: E402

B1, B2, NVME = "linux_block_1_stats", "linux_block_2_misc", "linux_nvme_1_stats"
LB, H2 = runconfig.TABLE, "linux_block_2_misc_histograms"
RUN, DB = "ProfileData-fio-20261015-101500", "profile_fw_fio_20261015"


def loader_columns(stem: str) -> dict[str, str]:
    """{column: ClickHouse type} exactly as the loader would create the table."""
    s = build_schema(stem, HERE / "sample_protos" / f"{stem}.proto")
    return {c.name: c.ch_type for c in s.columns}


def bucket_columns() -> dict[str, str]:
    """latency_buckets, as the loader creates it from the run's thresholds file."""
    return {c.name: c.ch_type for c in runconfig.COLUMNS}


class FakeDB:
    """runs.Database over plain dicts: {database: {table: {column: type}}}."""

    def __init__(self, databases: dict, runs: dict[str, set[str]]):
        self.databases, self.runs = databases, runs

    def find_tables(self, names):
        return [(d, t) for d, tables in self.databases.items() for t in tables if t in names]

    def has_run(self, database, table, run):
        return run in self.runs.get(database, set())

    def run_ids(self, database, table):
        return sorted(self.runs.get(database, set()))

    def columns(self, database):
        return self.databases[database]

    def close(self):
        pass


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


def run_db(**changes) -> dict:
    tables = {t: loader_columns(t) for t in (B1, B2, NVME)}
    tables[LB] = bucket_columns()
    for table, edit in changes.items():
        edit(tables[table])
    return tables


def full_run(cat, **changes):
    db = FakeDB({DB: run_db(**changes), "profile_fw_other": run_db()},
                {DB: {RUN}, "profile_fw_other": {"ProfileData-other-20261001-090000"}})
    return check_run(db, RUN, cat)


# ---- catalog ---------------------------------------------------------------

def test_catalog_reads_every_column(cat):
    assert set(cat.shared) >= {"run_id", "ts", "hostname"}
    assert len(cat.tables[B1].columns) == 18
    assert len(cat.tables[B2].columns) == 180          # groups count once per column
    assert cat.tables[B2].columns["read_4kb"].kind == "counter"
    assert cat.tables[B2].columns["read_device_latency_latency_overflow"].kind == "counter"
    assert cat.tables[B1].counts_since == "boot"
    assert cat.tables[B2].counts_since == "profiler start"
    assert {m.name for m in cat.tables[B1].metrics} >= {"read_iops", "avg_read_latency_ms"}


def test_catalog_reads_the_histograms_and_what_labels_them(cat):
    hists = cat.tables[B2].histograms
    assert [h.name for h in hists] == ["read device latency", "read driver latency",
                                       "write device latency", "write driver latency"]
    for h in hists:                                     # in position order, overflow last
        assert len(h.columns) == 17 and h.labels == LB
        assert h.columns[0].endswith("_latency_lh_opt1")
        assert h.columns[-1].endswith("_latency_overflow")
    assert cat.tables[LB].level == "run" and cat.tables[B2].level == "sample"
    assert {c.kind for c in cat.tables[LB].columns.values()} == {"dimension", "label", "setting"}


# ---- step 0 ----------------------------------------------------------------

def test_step0_finds_the_database_that_holds_the_run(cat):
    run = full_run(cat)
    assert run.database == DB
    assert set(run.tables) == {B1, B2, LB}
    assert run.undescribed_tables == [NVME]
    assert run.missing_tables == []
    assert run.problems == []
    for t in run.tables.values():                       # a run from today's protos
        assert t.not_collected == [] and t.not_described == []
        assert len(t.usable) == len(cat.tables[t.table].columns)


def test_step0_unknown_or_twice_loaded_run_stops(cat):
    db = FakeDB({DB: run_db()}, {DB: {RUN}})
    with pytest.raises(RunError, match="not found"):
        find_database(db, "ProfileData-nope", cat)
    twice = FakeDB({DB: run_db(), "copy": run_db()}, {DB: {RUN}, "copy": {RUN}})
    with pytest.raises(RunError, match="more than one database"):
        find_database(twice, RUN, cat)


def test_step0_db_option_names_the_database(cat):
    twice = FakeDB({DB: run_db(), "copy": run_db()}, {DB: {RUN}, "copy": {RUN}})
    assert find_database(twice, RUN, cat, DB) == DB             # --db settles "loaded twice"
    assert find_database(twice, RUN, cat, "copy") == "copy"
    with pytest.raises(RunError, match="none of the described tables.*copy"):
        find_database(twice, RUN, cat, "no_such_db")
    other = FakeDB({DB: run_db(), "copy": run_db()}, {DB: {RUN}, "copy": {"ProfileData-b"}})
    with pytest.raises(RunError, match=r"not found in database 'copy'\. "
                                       r"Runs loaded: ProfileData-b \(in copy\)"):
        find_database(other, RUN, cat, "copy")


def test_step0_not_found_says_which_runs_are_loaded(cat):
    db = FakeDB({DB: run_db()}, {DB: {RUN}})
    with pytest.raises(RunError, match=rf"Runs loaded: {RUN} \(in {DB}\)"):
        find_database(db, "ProfileData-nope", cat)


def test_step0_a_database_name_given_as_the_run(cat):
    db = FakeDB({DB: run_db()}, {DB: {RUN}})
    with pytest.raises(RunError, match=rf"'{DB}' is the name of a database, not of a run\. "
                                       rf"It holds {RUN}\. .*--db {DB}"):
        find_database(db, DB, cat)


def test_run_name_from_the_folder_name_or_its_path(tmp_path):
    assert run_name(RUN) == RUN
    assert run_name(f"/data/runs/{RUN}") == RUN
    assert run_name(f"/data/runs/{RUN}/") == RUN
    folder = tmp_path / RUN
    folder.mkdir()
    assert run_name(str(folder)) == RUN
    with pytest.raises(RunError, match="no run given"):
        run_name("  ")


def test_commands_take_a_path_and_db(cat, monkeypatch, capsys):
    from analysis import clickhouse
    from analysis.__main__ import main
    fake = FakeDB({DB: run_db(), "copy": run_db()}, {DB: {RUN}, "copy": {RUN}})
    monkeypatch.setattr(clickhouse.ClickHouse, "from_config",
                        classmethod(lambda cls, path=None: fake))
    assert main(["check-run", f"/data/runs/{RUN}/", "--db", DB]) == 0
    out = capsys.readouterr().out
    assert f"run name: {RUN}" in out and f"run {RUN} is in database {DB}" in out
    assert main(["check-run", RUN]) == 1                        # in two databases: needs --db
    assert "Choose one with --db" in capsys.readouterr().out


def test_step0_older_run_and_proto_changes(cat):
    merges = ["read_bio_back_merges", "write_bio_back_merges",
              "read_bio_front_merges", "write_bio_front_merges"]

    def older_block2(cols):                 # a run from before the merge counters were added
        for name in merges:
            del cols[name]
        cols["brand_new_field"] = "UInt64"

    def retyped_block1(cols):
        cols["read_io_count"] = "UInt32"

    run = full_run(cat, **{B2: older_block2, B1: retyped_block1})
    b2 = run.tables[B2]
    assert b2.not_collected == merges
    assert b2.not_described == ["brand_new_field"]
    assert run.problems == ["linux_block_1_stats.read_io_count: catalog UInt64, run UInt32"]


def test_step0_table_missing_from_run(cat):
    db = FakeDB({DB: {B1: loader_columns(B1)}}, {DB: {RUN}})
    run = check_run(db, RUN, cat)
    assert set(run.tables) == {B1} and run.missing_tables == [LB, B2]


# ---- per-interval views ----------------------------------------------------

def test_view_turns_counters_into_changes(cat):
    run = full_run(cat)
    b1 = run.tables[B1]
    assert partition_keys(b1) == ["run_id", "hostname", "device"]
    names = [c.name for c in view_columns(b1, cat)]
    assert names[:5] == ["run_id", "hostname", "ts", "device", "interval_s"]
    assert "read_io_count_delta" in names and "in_flight_io_count" in names
    assert "read_io_count" not in names                 # the raw running total is not there
    sql = create_view_sql(DB, b1, cat)
    assert sql.startswith(f"CREATE OR REPLACE VIEW `{DB}`.`{B1}_interval` AS")
    assert f"FROM `{DB}`.`{B1}`" in sql
    # since boot: a run's first sample is not activity of the run
    assert ("if(__rn = 1, NULL, if(toInt64(`read_io_count`) < __prev_read_io_count, NULL, "
            "toInt64(`read_io_count`) - __prev_read_io_count)) AS `read_io_count_delta`") in sql
    assert "PARTITION BY run_id, hostname, device ORDER BY ts" in sql


def test_view_profiler_counters_start_at_zero(cat):
    b2 = full_run(cat).tables[B2]
    assert partition_keys(b2) == ["run_id", "hostname"]
    names = {c.name for c in view_columns(b2, cat)}
    assert {"read_bios_delta", "read_4kb_delta"} <= names
    sql = create_view_sql(DB, b2, cat)
    assert "if(__rn = 1, toInt64(`read_bios`), " in sql


def test_view_shows_a_setting_as_is(cat):
    """A setting - chosen when the profiler started, fixed for the run - is never
    differenced. The block tables have none today (their latency thresholds are rows of
    latency_buckets), so a copy of the catalog gets one."""
    import copy
    cat2 = copy.deepcopy(cat)
    cat2.tables[B2].columns["chosen_threshold_us"] = catalog_mod.Column(
        "chosen_threshold_us", "UInt64", "A threshold chosen when the profiler started.",
        "microseconds", "setting", "draft")

    def with_setting(cols):
        cols["chosen_threshold_us"] = "UInt64"
    db = FakeDB({DB: run_db(**{B2: with_setting})}, {DB: {RUN}})
    names = {c.name for c in view_columns(check_run(db, RUN, cat2).tables[B2], cat2)}
    assert "chosen_threshold_us" in names and "chosen_threshold_us_delta" not in names


def test_view_has_only_the_columns_the_run_has(cat):
    def no_flush(cols):
        del cols["flush_io_count"], cols["flush_time_ms"]
    b1 = full_run(cat, **{B1: no_flush}).tables[B1]
    names = {c.name for c in view_columns(b1, cat)}
    assert "flush_io_count_delta" not in names and "read_io_count_delta" in names


def test_view_sql_parses_as_clickhouse(cat):
    sqlglot = pytest.importorskip("sqlglot")
    run = full_run(cat)
    for t in (run.tables[B1], run.tables[B2]):           # latency_buckets has no view
        sql = create_view_sql(DB, t, cat)
        assert sqlglot.parse_one(sql, read="clickhouse") is not None
        view_q, raw_q, counters = verify_sql(DB, t, cat)
        for q in (view_q, raw_q):
            assert sqlglot.parse_one(q.replace("{run:String}", "'x'"), read="clickhouse")
    _, raw_b1, _ = verify_sql(DB, run.tables[B1], cat)
    _, raw_b2, _ = verify_sql(DB, run.tables[B2], cat)
    assert "argMin" in raw_b1                            # since boot: last minus first
    assert "argMin" not in raw_b2                        # from zero: last


# ---- histogram views -------------------------------------------------------

def test_histogram_view_labels_each_bucket_by_its_position(cat):
    run = full_run(cat)
    sql = create_histogram_view_sql(DB, run, run.tables[B2], cat)
    assert sql.startswith(f"CREATE OR REPLACE VIEW `{DB}`.`{H2}` AS")
    assert f"FROM `{DB}`.`{B2}_interval` AS v" in sql   # made over the per-interval view
    # position p is option_index p; the last column, the overflow, is option_index n
    assert "('read device latency', 0, 16, v.`read_device_latency_latency_lh_opt1_delta`)" in sql
    assert "('write driver latency', 16, 16, v.`write_driver_latency_latency_overflow_delta`)" in sql
    assert f"max(`option_index`) AS n FROM `{DB}`.`{LB}`" in sql
    assert "multiIf(x.position = x.last, nb.n, x.position < nb.n, x.position, 65535)" in sql
    assert create_histogram_view_sql(DB, run, run.tables[B1], cat) is None   # B1 has none


def test_histogram_sql_parses_as_clickhouse(cat):
    sqlglot = pytest.importorskip("sqlglot")
    run = full_run(cat)
    assert sqlglot.parse_one(create_histogram_view_sql(DB, run, run.tables[B2], cat),
                             read="clickhouse")
    view_q, raw_q, names = verify_histogram_sql(DB, run, run.tables[B2], cat)
    for q in (view_q, raw_q):
        assert sqlglot.parse_one(q.replace("{run:String}", "'x'"), read="clickhouse")
    assert names[0] == "read device latency" and raw_q.count("sum(") == 4


def test_no_histograms_without_the_runs_thresholds(cat):
    """A run profiled without -lat-buckets has no latency_buckets table: its bucket
    counts cannot be labelled, so they are not offered at all, and the model says why."""
    db = FakeDB({DB: {B1: loader_columns(B1), B2: loader_columns(B2)}}, {DB: {RUN}})
    run = check_run(db, RUN, cat)
    assert usable_histograms(run, run.tables[B2], cat) == []
    assert create_histogram_view_sql(DB, run, run.tables[B2], cat) is None
    specs = {s.name: s for s in model_specs(run, cat)}
    assert set(specs) == {B1, B2}
    assert "cannot be shown" in specs[B2].about and "-lat-buckets" in specs[B2].about
    assert not any("latency_lh_opt1" in c.name for c in specs[B2].columns)


def test_views_are_made_in_order_and_none_for_a_run_level_table(cat):
    sqls = create_views_sql(full_run(cat), cat)
    made = [s.split("`")[3] for s in sqls]
    assert made == [f"{B1}_interval", f"{B2}_interval", H2]


# ---- WrenAI data models ----------------------------------------------------

def test_data_models_point_at_the_runs_views(cat):
    run = full_run(cat)
    specs = model_specs(run, cat)
    manifest = data_models(run, specs)
    models = {m["name"]: m for m in manifest["models"]}
    assert set(models) == {B1, B2, H2, LB}
    assert models[B1]["tableReference"] == {"catalog": "", "schema": DB, "table": f"{B1}_interval"}
    assert models[H2]["tableReference"]["table"] == H2
    assert models[LB]["tableReference"]["table"] == LB          # read as it is
    cols = {c["name"]: c for c in models[B1]["columns"]}
    assert cols["read_iops"]["isCalculated"] and cols["read_iops"]["expression"] == \
        "read_io_count_delta / interval_s"
    assert cols["read_io_count_delta"]["type"] == "BIGINT"
    assert "read_io_count" not in cols
    # the bucket counts are offered labelled, in the histogram model, not as 68 columns
    b2 = {c["name"] for c in models[B2]["columns"]}
    assert "read_4kb_delta" in b2 and not any("_latency_" in n for n in b2)
    h2 = {c["name"]: c["type"] for c in models[H2]["columns"]}
    assert h2["bucket_index"] == "USMALLINT" and h2["bucket"] == "TEXT"
    assert h2["io_count_delta"] == "BIGINT" and "ts" in h2
    assert {c["name"] for c in models[LB]["columns"]} == {"run_id", "option_index", "range",
                                                          "threshold_us"}
    assert all(not s.left_out for s in specs)


def test_histogram_model_lists_the_runs_buckets(cat):
    run = full_run(cat)
    listed = {LB: [(0, "< 1 µs"), (1, "1-10 µs"), (2, "> 10 µs")]}
    specs = {s.name: s for s in model_specs(run, cat, listed)}
    h2 = specs[H2]
    assert "This run's buckets (bucket_index: bucket): 0: < 1 µs, 1: 1-10 µs, 2: > 10 µs." in h2.about
    which = next(c for c in h2.columns if c.name == "histogram").meaning
    assert "'read device latency' - Read requests counted by their latency in the device; " in which
    assert which.endswith("in the driver layer.")
    assert f"To show a histogram, use {H2}" in specs[LB].about     # not the raw bucket columns
    # latency_buckets is there but holds no rows of this run: nothing to label with
    assert H2 not in {s.name for s in model_specs(run, cat, {LB: []})}


def test_metric_left_out_when_the_run_lacks_its_column(cat):
    def no_read_time(cols):
        del cols["read_time_ms"]
    run = full_run(cat, **{B1: no_read_time})
    specs = model_specs(run, cat)
    b1 = next(m for m in data_models(run, specs)["models"] if m["name"] == B1)
    assert "avg_read_latency_ms" not in {c["name"] for c in b1["columns"]}
    assert {s.name: s.left_out for s in specs if s.left_out} == {B1: ["avg_read_latency_ms"]}


def test_wren_type_names_still_match_wrenai():
    type_mapping = pytest.importorskip("wren.type_mapping")
    for ch_type, wren_type in WREN_TYPES.items():
        assert type_mapping.parse_type(ch_type, "clickhouse") == wren_type, ch_type


# ---- check C1 --------------------------------------------------------------

def test_c1_rules_for_our_llms_query():
    pytest.importorskip("sqlglot")
    from analysis.checks import model_query_problems
    models = {B1, B2}
    ok = (f"SELECT toStartOfMinute({B1}.ts) AS minute, avg({B1}.read_iops) AS iops FROM {B1} "
          f"WHERE {B1}.run_id = '{RUN}' GROUP BY minute ORDER BY minute")
    assert model_query_problems(ok, models, RUN) == []
    bad = {
        "WITH blocks": f"WITH a AS (SELECT 1 AS x) SELECT {B1}.ts FROM {B1}, a "
                       f"WHERE {B1}.run_id = '{RUN}'",
        "without a database": f"SELECT {B1}.ts FROM {DB}.{B1} WHERE {B1}.run_id = '{RUN}'",
        "not a data model": f"SELECT x.ts FROM x WHERE x.run_id = '{RUN}'",
        "table.read_iops": f"SELECT read_iops FROM {B1} WHERE {B1}.run_id = '{RUN}'",
        "filter to the run": f"SELECT {B1}.ts FROM {B1}",
        "read-only SELECT": f"INSERT INTO {B1} (run_id) VALUES ('x')",
        "table function": f"SELECT * FROM url('http://example.com/x.csv', CSV)",
    }
    for expected, sql in bad.items():
        problems = model_query_problems(sql, models, RUN)
        assert any(expected in p for p in problems), (expected, problems)


def test_c1_reads_only_the_runs_views():
    pytest.importorskip("sqlglot")
    from analysis.checks import planned_sql_problems
    allowed = {(DB, f"{B1}_interval")}
    good = (f"WITH {B1} AS (SELECT __source.ts AS ts FROM {DB}.{B1}_interval AS __source) "
            f"SELECT {B1}.ts FROM {B1}")
    assert planned_sql_problems(good, allowed) == []
    raw = f"WITH {B1} AS (SELECT 1) SELECT read_io_count FROM {DB}.{B1}"
    assert any("not what one of the run's data models reads" in p
               for p in planned_sql_problems(raw, allowed))
    assert planned_sql_problems("SELECT * FROM system.tables", allowed)


def _manifest(run, cat):
    manifest = data_models(run, model_specs(run, cat))
    return manifest, {(m["tableReference"]["schema"], m["tableReference"]["table"])
                      for m in manifest["models"]}


def test_wrenai_plans_over_the_view_and_c1_catches_its_gap(cat):
    pytest.importorskip("wren.engine")
    from analysis.checks import planned_sql_problems
    from analysis.wren_project import PlanError, plan
    run = full_run(cat)
    manifest, allowed = _manifest(run, cat)

    sql = plan(manifest, f"SELECT {B1}.ts, {B1}.read_iops FROM {B1} WHERE {B1}.run_id = '{RUN}'")
    assert f"{DB}.{B1}_interval" in sql and planned_sql_problems(sql, allowed) == []

    with pytest.raises(PlanError, match="read_ios"):          # a column that does not exist
        plan(manifest, f"SELECT {B1}.read_ios FROM {B1}")

    # the gap found on 2026-10-15: a database-qualified name reaches the raw table.
    # Whether WrenAI refuses it (a fixed version) or plans it, it must never pass.
    try:
        sql = plan(manifest, f"SELECT read_io_count FROM {DB}.{B1}")
    except PlanError:
        return
    assert planned_sql_problems(sql, allowed)


def test_wrenai_plans_a_latency_distribution_and_the_thresholds(cat):
    pytest.importorskip("wren.engine")
    from analysis.checks import model_query_problems, planned_sql_problems
    from analysis.wren_project import plan
    run = full_run(cat)
    manifest, allowed = _manifest(run, cat)
    models = {m["name"] for m in manifest["models"]}
    distribution = (f"SELECT {H2}.bucket AS \"latency range\", sum({H2}.io_count_delta) AS \"reads\" "
                    f"FROM {H2} WHERE {H2}.run_id = '{RUN}' AND {H2}.histogram = 'read device latency' "
                    f"GROUP BY {H2}.bucket_index, {H2}.bucket ORDER BY {H2}.bucket_index")
    thresholds = (f"SELECT {LB}.option_index AS \"bucket\", {LB}.range AS \"range\" FROM {LB} "
                  f"WHERE {LB}.run_id = '{RUN}' ORDER BY {LB}.option_index")
    for query, reads in ((distribution, f"{DB}.{H2}"), (thresholds, f"{DB}.{LB}")):
        assert model_query_problems(query, models, RUN) == []
        sql = plan(manifest, query)
        assert reads in sql and planned_sql_problems(sql, allowed) == []


def test_plan_file_command_needs_no_clickhouse(cat, tmp_path, capsys):
    pytest.importorskip("wren.engine")
    import json
    from analysis.__main__ import main
    manifest, _ = _manifest(full_run(cat), cat)
    path = tmp_path / "data_models.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    good = f"SELECT {B2}.ts, {B2}.avg_read_device_time_us FROM {B2} WHERE {B2}.run_id = '{RUN}'"
    assert main(["plan-file", str(path), good]) == 0
    assert "C1: passed" in capsys.readouterr().out
    assert main(["plan-file", str(path), f"SELECT read_io_count FROM {DB}.{B1}"]) == 1


# ---- prepare ---------------------------------------------------------------

class PrepareDB(FakeDB):
    """FakeDB that records the views made and answers prepare's checks: the counters
    add up, and 10 read device latency I/Os are in columns the run does not use."""

    def __init__(self):
        super().__init__({DB: run_db()}, {DB: {RUN}})
        self.made = []

    def execute(self, sql):
        self.made.append(sql.split("`")[3])

    def rows(self, sql, parameters=None):
        if "SELECT DISTINCT `option_index`, `range`" in sql:
            return [(0, "< 1 µs"), (1, "1-10 µs"), (2, "> 10 µs")]
        if f"`{H2}`" in sql:                      # per histogram, the I/Os in the view
            return [("read device latency", 90), ("read driver latency", 100),
                    ("write device latency", 100), ("write driver latency", 100)]
        if "sum(ifNull(" in sql:                  # ... and in all its bucket columns
            return [(100, 100, 100, 100)]
        return []                                 # the counter checks: no series, all equal


def test_prepare_makes_the_histogram_view_and_checks_its_buckets(monkeypatch, capsys):
    from analysis import clickhouse
    from analysis.__main__ import main
    fake = PrepareDB()
    monkeypatch.setattr(clickhouse.ClickHouse, "from_config",
                        classmethod(lambda cls, path=None: fake))
    assert main(["prepare", RUN]) == 0                  # a warning does not fail it
    out = capsys.readouterr().out
    assert fake.made == [f"{B1}_interval", f"{B2}_interval", H2]
    assert f"histograms, labelled by {LB}: read device latency" in out
    assert f"3 buckets, from {LB}: < 1 µs to > 10 µs" in out
    assert "read driver latency: all 100 I/Os are in its 3 buckets" in out
    # 2 thresholds: lh_opt1, bw_opt1_and_opt2 and overflow are used; the rest must be 0
    assert ("WARNING - read device latency: 10 of 100 I/Os are in columns this run does not "
            "use (read_device_latency_latency_bw_opt2_and_opt3 to "
            "read_device_latency_latency_bw_opt15_and_opt16; it set 2 thresholds)") in out
