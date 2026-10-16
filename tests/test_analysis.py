"""Phase 1 tests: step 0, the per-run views, the WrenAI data models, check C1.
Run:  python -m pytest tests/ -v

No ClickHouse needed: a fake database holds what the loader would create from
tests/sample_protos/. The tests that call WrenAI or sqlglot are skipped when
wrenai is not installed (pip install -r requirements.txt).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from analysis import catalog as catalog_mod  # noqa: E402
from analysis.runs import RunError, check_run, find_database  # noqa: E402
from analysis.views import create_view_sql, partition_keys, verify_sql, view_columns  # noqa: E402
from analysis.wren_project import WREN_TYPES, data_models  # noqa: E402
from src.registry import build_schema  # noqa: E402

B1, B2, NVME = "linux_block_1_stats", "linux_block_2_misc", "linux_nvme_1_stats"
RUN, DB = "ProfileData-fio-20261015-101500", "profile_fw_fio_20261015"


def loader_columns(stem: str) -> dict[str, str]:
    """{column: ClickHouse type} exactly as the loader would create the table."""
    s = build_schema(stem, HERE / "sample_protos" / f"{stem}.proto")
    return {c.name: c.ch_type for c in s.columns}


class FakeDB:
    """runs.Database over plain dicts: {database: {table: {column: type}}}."""

    def __init__(self, databases: dict, runs: dict[str, set[str]]):
        self.databases, self.runs = databases, runs

    def find_tables(self, names):
        return [(d, t) for d, tables in self.databases.items() for t in tables if t in names]

    def has_run(self, database, table, run):
        return run in self.runs.get(database, set())

    def columns(self, database):
        return self.databases[database]


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


def run_db(**changes) -> dict:
    tables = {t: loader_columns(t) for t in (B1, B2, NVME)}
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
    assert len(cat.tables[B2].columns) == 196          # groups count once per column
    assert cat.tables[B2].columns["read_4kb"].kind == "counter"
    assert cat.tables[B2].columns["lat_option_us_1"].kind == "setting"
    assert cat.tables[B1].counts_since == "boot"
    assert cat.tables[B2].counts_since == "profiler start"
    assert {m.name for m in cat.tables[B1].metrics} >= {"read_iops", "avg_read_latency_ms"}


# ---- step 0 ----------------------------------------------------------------

def test_step0_finds_the_database_that_holds_the_run(cat):
    run = full_run(cat)
    assert run.database == DB
    assert set(run.tables) == {B1, B2}
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


def test_step0_older_run_and_proto_changes(cat):
    def older_block2(cols):                 # a run from before the thresholds were added
        for i in range(1, 17):
            del cols[f"lat_option_us_{i}"]
        cols["brand_new_field"] = "UInt64"

    def retyped_block1(cols):
        cols["read_io_count"] = "UInt32"

    run = full_run(cat, **{B2: older_block2, B1: retyped_block1})
    b2 = run.tables[B2]
    assert b2.not_collected == [f"lat_option_us_{i}" for i in range(1, 17)]
    assert b2.not_described == ["brand_new_field"]
    assert run.problems == ["linux_block_1_stats.read_io_count: catalog UInt64, run UInt32"]


def test_step0_table_missing_from_run(cat):
    db = FakeDB({DB: {B1: loader_columns(B1)}}, {DB: {RUN}})
    run = check_run(db, RUN, cat)
    assert set(run.tables) == {B1} and run.missing_tables == [B2]


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
    assert {"read_bios_delta", "lat_option_us_1", "read_4kb_delta"} <= names
    assert "lat_option_us_1_delta" not in names         # a setting is never differenced
    sql = create_view_sql(DB, b2, cat)
    assert "if(__rn = 1, toInt64(`read_bios`), " in sql


def test_view_has_only_the_columns_the_run_has(cat):
    def no_flush(cols):
        del cols["flush_io_count"], cols["flush_time_ms"]
    b1 = full_run(cat, **{B1: no_flush}).tables[B1]
    names = {c.name for c in view_columns(b1, cat)}
    assert "flush_io_count_delta" not in names and "read_io_count_delta" in names


def test_view_sql_parses_as_clickhouse(cat):
    sqlglot = pytest.importorskip("sqlglot")
    run = full_run(cat)
    for t in run.tables.values():
        sql = create_view_sql(DB, t, cat)
        assert sqlglot.parse_one(sql, read="clickhouse") is not None
        view_q, raw_q, counters = verify_sql(DB, t, cat)
        for q in (view_q, raw_q):
            assert sqlglot.parse_one(q.replace("{run:String}", "'x'"), read="clickhouse")
    _, raw_b1, _ = verify_sql(DB, run.tables[B1], cat)
    _, raw_b2, _ = verify_sql(DB, run.tables[B2], cat)
    assert "argMin" in raw_b1                            # since boot: last minus first
    assert "argMin" not in raw_b2                        # from zero: last


# ---- WrenAI data models ----------------------------------------------------

def test_data_models_point_at_the_runs_views(cat):
    manifest, left_out = data_models(full_run(cat), cat)
    models = {m["name"]: m for m in manifest["models"]}
    assert set(models) == {B1, B2}
    assert models[B1]["tableReference"] == {"catalog": "", "schema": DB, "table": f"{B1}_interval"}
    cols = {c["name"]: c for c in models[B1]["columns"]}
    assert cols["read_iops"]["isCalculated"] and cols["read_iops"]["expression"] == \
        "read_io_count_delta / interval_s"
    assert cols["read_io_count_delta"]["type"] == "BIGINT"
    assert "read_io_count" not in cols
    assert left_out == {}


def test_metric_left_out_when_the_run_lacks_its_column(cat):
    def no_read_time(cols):
        del cols["read_time_ms"]
    manifest, left_out = data_models(full_run(cat, **{B1: no_read_time}), cat)
    b1 = next(m for m in manifest["models"] if m["name"] == B1)
    assert "avg_read_latency_ms" not in {c["name"] for c in b1["columns"]}
    assert left_out == {B1: ["avg_read_latency_ms"]}


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
    assert any("not one of the run's per-interval views" in p
               for p in planned_sql_problems(raw, allowed))
    assert planned_sql_problems("SELECT * FROM system.tables", allowed)


def test_wrenai_plans_over_the_view_and_c1_catches_its_gap(cat):
    pytest.importorskip("wren.engine")
    from analysis.checks import planned_sql_problems
    from analysis.wren_project import PlanError, plan
    run = full_run(cat)
    manifest, _ = data_models(run, cat)
    allowed = {(run.database, f"{t}_interval") for t in run.tables}

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


def test_plan_file_command_needs_no_clickhouse(cat, tmp_path, capsys):
    pytest.importorskip("wren.engine")
    import json
    from analysis.__main__ import main
    manifest, _ = data_models(full_run(cat), cat)
    path = tmp_path / "data_models.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    good = f"SELECT {B2}.ts, {B2}.avg_read_device_time_us FROM {B2} WHERE {B2}.run_id = '{RUN}'"
    assert main(["plan-file", str(path), good]) == 0
    assert "C1: passed" in capsys.readouterr().out
    assert main(["plan-file", str(path), f"SELECT read_io_count FROM {DB}.{B1}"]) == 1
