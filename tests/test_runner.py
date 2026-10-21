"""Phase 1 tests: the request runner, `python -m analysis ask` (design section 7).
Run:  python -m pytest tests/ -v

No ClickHouse, LLM or Grafana needed: a fake database returns the loader's columns
and canned results, a scripted LLM gives the answers a test needs, and a fake Grafana
records what would be drawn. WrenAI plans the queries for real, so the tests that need
it are skipped when wrenai is not installed.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from analysis import catalog as catalog_mod  # noqa: E402
from analysis.checks import explanation_problems  # noqa: E402
from analysis.grafana import choose_panel, dashboard_uid, panel  # noqa: E402
from analysis.llm import json_object  # noqa: E402
from analysis.runner import Request, Runner, load_request  # noqa: E402
from analysis.runs import RunError  # noqa: E402
from analysis.steps import StepError, bounds, parse_meaning  # noqa: E402
from src import runconfig  # noqa: E402
from src.registry import build_schema  # noqa: E402

B1, B2 = "linux_block_1_stats", "linux_block_2_misc"
LB, H2 = runconfig.TABLE, "linux_block_2_misc_histograms"
RUN, DB = "ProfileData-fio-20261015-082730", "fio_run_db"
BUCKETS = [(0, "< 1 µs"), (1, "1-10 µs"), (2, "> 10 µs")]
START, END = datetime(2026, 10, 15, 8, 27, 30), datetime(2026, 10, 15, 8, 31, 13)
SERIES = (["ts", "read IOPS"], ["DateTime", "Nullable(Float64)"],
          [(START, None), (datetime(2026, 10, 15, 8, 27, 31), 166000.0),
           (datetime(2026, 10, 15, 8, 27, 32), 167000.0)])


def loader_columns(stem: str) -> dict[str, str]:
    s = build_schema(stem, HERE / "sample_protos" / f"{stem}.proto")
    return {c.name: c.ch_type for c in s.columns}


class RunDB:
    """What the runner needs from ClickHouse, with canned query results. With
    buckets=True the run also has latency_buckets, as a run profiled with -lat-buckets."""

    def __init__(self, results: list, buckets: bool = False):
        self.results, self.queries, self.views = list(results), [], []
        self.tables = {B1: loader_columns(B1), B2: loader_columns(B2)}
        if buckets:
            self.tables[LB] = {c.name: c.ch_type for c in runconfig.COLUMNS}

    def find_tables(self, names):
        return [(DB, t) for t in self.tables if t in names]

    def has_run(self, database, table, run):
        return run == RUN

    def run_ids(self, database, table):
        return [RUN]

    def columns(self, database):
        return self.tables

    def time_span(self, database, table, run):
        assert table != LB, "a run-level table has no ts"
        return START, END

    def execute(self, sql):
        self.views.append(sql)

    def query(self, sql):
        self.queries.append(sql)
        return self.results.pop(0)

    def rows(self, sql, parameters=None):
        if "SELECT DISTINCT `option_index`, `range`" in sql:
            return BUCKETS               # the run's buckets
        return [(222,)]                  # samples in the time range (for an empty result)

    def close(self):
        pass


class ScriptedLlm:
    """Answers per step (1, 3, 7) from lists; an answer may be a function of the prompt."""

    def __init__(self, meanings=(), queries=(), explanations=()):
        self.answers = {"1": list(meanings), "3": list(queries), "7": list(explanations)}
        self.prompts, self.calls = [], []

    def ask_json(self, step, system, user):
        self.prompts.append((step, user))
        answer = self.answers[step[0]].pop(0)
        return answer(user) if callable(answer) else answer


class FakeGrafana:
    def __init__(self):
        self.published = []

    def publish(self, **kw):
        self.published.append(kw)
        return "http://grafana.test/d/afw-x"


IOPS_MEANING = {"answerable": True, "summary": "read IOPS, per second",
                "measures": [{"table": B1, "name": "read_iops", "words": "read IOPS"}],
                "layers": ["block layer - completed requests (linux_block_1_stats)"],
                "time": {"kind": "whole_run"}, "chart": "timeseries",
                "assumptions": ["block layer means completed requests"], "not_available": []}
GOOD_SQL = (f"SELECT {B1}.ts AS ts, {B1}.read_iops AS \"read IOPS\" FROM {B1} "
            f"WHERE {B1}.run_id = '{RUN}' ORDER BY ts")


def query(sql, chart="timeseries", units=None):
    return {"sql": sql, "chart": chart, "units": units or {"read IOPS": "I/O per second"}}


@pytest.fixture(scope="module")
def cat():
    return catalog_mod.load()


def ask(cat, tmp_path, llm, results, grafana=None, confirm=lambda shown: True,
        text="Show read IOPS", buckets=False):
    db = RunDB(results, buckets)
    said = []
    runner = Runner(db, cat, llm, grafana, confirm=confirm, say=said.append, out=tmp_path)
    outcome = runner.ask(Request(run=RUN, text=text))
    record = json.loads(outcome.record.read_text(encoding="utf-8"))
    return outcome, record, db, "\n".join(said)


# ---- the whole request ------------------------------------------------------------------

def test_a_request_becomes_a_checked_graph(cat, tmp_path):
    pytest.importorskip("wren.engine")
    llm = ScriptedLlm([IOPS_MEANING], [query(GOOD_SQL)],
                      [{"explanation": "Read IOPS at the block layer peaked at 167,000."}])
    grafana = FakeGrafana()
    outcome, record, db, said = ask(cat, tmp_path, llm, [SERIES], grafana)
    assert outcome.status == "drawn" and outcome.link == "http://grafana.test/d/afw-x"
    drawn = grafana.published[0]
    assert drawn["kind"] == "timeseries" and drawn["run"] == RUN
    assert f"{DB}.{B1}_interval" in drawn["sql"]              # the panel reads the run's view
    assert drawn["sql"] == db.queries[0]                      # ... and is the SQL that was run
    # the dashboard shows the result's own times, even where the run goes on longer
    assert (drawn["start"], drawn["end"]) == (START, datetime(2026, 10, 15, 8, 27, 32))
    assert "167,000" in drawn["description"] and "1 of 3 rows have no value" in drawn["description"]
    assert len(db.views) == 2                                 # both block tables' views made
    assert record["outcome"]["status"] == "drawn"
    assert record["steps"]["3_queries"][0]["c1"] == "passed"
    assert record["steps"]["7_explanations"][0]["c3"] == "passed"


def test_c1_and_c2_send_the_query_back_with_the_reason(cat, tmp_path):
    pytest.importorskip("wren.engine")
    raw = f"SELECT read_io_count FROM {DB}.{B1} WHERE {B1}.run_id = '{RUN}'"
    negative = (["ts", "read IOPS"], ["DateTime", "Float64"], [(START, -5.0), (END, 3.0)])
    llm = ScriptedLlm([IOPS_MEANING], [query(raw), query(GOOD_SQL), query(GOOD_SQL)],
                      [{"explanation": "Read IOPS at the block layer, per second."}])
    outcome, record, db, said = ask(cat, tmp_path, llm, [negative, SERIES], FakeGrafana())
    tries = record["steps"]["3_queries"]
    assert any("without a database" in p for p in tries[0]["c1"])
    assert any("negative values" in p for p in tries[1]["c2"])
    assert tries[2]["c2"]["passed"] and outcome.status == "drawn"
    third_prompt = [u for s, u in llm.prompts if s.startswith("3")][2]
    assert "This query was refused" in third_prompt and "negative values" in third_prompt


def test_no_wrong_graph_after_three_refused_queries(cat, tmp_path):
    pytest.importorskip("wren.engine")
    raw = f"SELECT read_io_count FROM {DB}.{B1} WHERE {B1}.run_id = '{RUN}'"
    grafana = FakeGrafana()
    outcome, record, _, _ = ask(cat, tmp_path, ScriptedLlm([IOPS_MEANING], [query(raw)] * 3),
                                [], grafana)
    assert outcome.status == "stopped" and "3 tries" in outcome.message
    assert grafana.published == []


def test_a_request_nothing_can_answer_is_refused(cat, tmp_path):
    meaning = {"answerable": False, "summary": "", "measures": [],
               "not_available": [{"part": "SSD temperature", "reason": "not described yet"}]}
    llm = ScriptedLlm([meaning])
    outcome, record, db, _ = ask(cat, tmp_path, llm, [], FakeGrafana())
    assert outcome.status == "refused" and "SSD temperature - not described yet" in outcome.message
    assert db.queries == [] and all(s.startswith("1") for s, _ in llm.prompts)


def test_no_lets_you_rephrase_and_starts_again(cat, tmp_path):
    answers = iter(["Show write IOPS instead", False])
    llm = ScriptedLlm([IOPS_MEANING, IOPS_MEANING])
    outcome, record, db, _ = ask(cat, tmp_path, llm, [], confirm=lambda shown: next(answers))
    assert outcome.status == "declined" and db.queries == []
    assert [u.rsplit("Request: ", 1)[1] for s, u in llm.prompts] == ["Show read IOPS",
                                                                     "Show write IOPS instead"]
    assert record["request"]["rephrased"] == "Show write IOPS instead"


def test_an_empty_result_is_explained_not_drawn(cat, tmp_path):
    pytest.importorskip("wren.engine")
    grafana = FakeGrafana()
    empty = (["ts", "read IOPS"], ["DateTime", "Float64"], [])
    outcome, _, _, _ = ask(cat, tmp_path, ScriptedLlm([IOPS_MEANING], [query(GOOD_SQL)]),
                           [empty], grafana)
    assert outcome.status == "stopped" and "no rows" in outcome.message
    assert "222 sample(s)" in outcome.message and grafana.published == []


def test_an_explanation_with_invented_numbers_is_replaced(cat, tmp_path):
    pytest.importorskip("wren.engine")
    made_up = {"explanation": "Read IOPS peaked at 250,000 during checkpointing."}
    llm = ScriptedLlm([IOPS_MEANING], [query(GOOD_SQL)], [made_up, made_up])
    outcome, record, _, said = ask(cat, tmp_path, llm, [SERIES], FakeGrafana())
    assert outcome.status == "drawn" and "250,000" not in outcome.message
    assert "from 166,000 to 167,000" in outcome.message      # written by code from the result
    assert record["steps"]["7_explanations"][-1] == {"fallback": "written by code from the result summary"}
    assert "C3 refused the explanation" in said


def test_a_time_range_needs_a_utc_time_filter(cat, tmp_path):
    pytest.importorskip("wren.engine")
    last = dict(IOPS_MEANING, time={"kind": "last", "seconds": 60})
    timed = GOOD_SQL.replace(" ORDER BY", f" AND {B1}.ts >= toDateTime('2026-10-15 08:30:13', 'UTC')"
                             f" AND {B1}.ts <= toDateTime('2026-10-15 08:31:13', 'UTC') ORDER BY")
    llm = ScriptedLlm([last], [query(GOOD_SQL), query(timed)],
                      [{"explanation": "Read IOPS in the last minute of the run."}])
    outcome, record, _, _ = ask(cat, tmp_path, llm, [SERIES], FakeGrafana())
    assert any("must filter ts" in p for p in record["steps"]["3_queries"][0]["c1"])
    assert outcome.status == "drawn"
    assert record["steps"]["time_range"]["from"] == "2026-10-15 08:30:13"


DISTRIBUTION = {"answerable": True, "summary": "read latency in the device, by range",
                "measures": [{"table": H2, "name": "io_count_delta", "words": "reads per range"}],
                "layers": ["block layer - time on the device (linux_block_2_misc)"],
                "time": {"kind": "whole_run"}, "chart": "bar",
                "assumptions": ["read latency means read device latency"], "not_available": []}
DISTRIBUTION_SQL = (f"SELECT {H2}.bucket AS \"latency range\", sum({H2}.io_count_delta) AS \"reads\" "
                    f"FROM {H2} WHERE {H2}.run_id = '{RUN}' AND {H2}.histogram = "
                    f"'read device latency' GROUP BY {H2}.bucket_index, {H2}.bucket "
                    f"ORDER BY {H2}.bucket_index")


def test_a_latency_distribution_becomes_a_labelled_bar_chart(cat, tmp_path):
    pytest.importorskip("wren.engine")
    result = (["latency range", "reads"], ["String", "Nullable(Int64)"],
              [("< 1 µs", 10), ("1-10 µs", 200), ("> 10 µs", 5)])
    llm = ScriptedLlm([DISTRIBUTION], [query(DISTRIBUTION_SQL, "bar", {"reads": "count"})],
                      [{"explanation": "Most reads, 200, took 1-10 µs on the device."}])
    grafana = FakeGrafana()
    outcome, record, db, _ = ask(cat, tmp_path, llm, [result], grafana, buckets=True)
    assert outcome.status == "drawn", outcome.message
    drawn = grafana.published[0]
    assert drawn["kind"] == "bar" and f"{DB}.{H2}" in drawn["sql"]
    assert [v.split("`")[3] for v in db.views] == [f"{B1}_interval", f"{B2}_interval", H2]
    assert record["steps"]["7_explanations"][0]["c3"] == "passed"   # 10 is in a label
    assert H2 in record["data_models"] and LB in record["data_models"]
    step1 = next(u for s, u in llm.prompts if s.startswith("1"))
    assert "0: < 1 µs, 1: 1-10 µs, 2: > 10 µs" in step1              # the run's own buckets
    assert "read_device_latency_latency_lh_opt1_delta" not in step1    # never unlabelled


def test_run_level_data_has_no_time_range(cat, tmp_path):
    pytest.importorskip("wren.engine")
    meaning = {"answerable": True, "summary": "the run's latency buckets",
               "measures": [{"table": LB, "name": "range", "words": "bucket ranges"}],
               "layers": ["the run's settings (latency_buckets)"],
               "time": {"kind": "last", "seconds": 60}, "chart": "table",
               "assumptions": [], "not_available": []}
    sql = (f"SELECT {LB}.option_index AS \"bucket\", {LB}.range AS \"range\" FROM {LB} "
           f"WHERE {LB}.run_id = '{RUN}' ORDER BY {LB}.option_index")
    result = (["bucket", "range"], ["UInt16", "String"], [(i, r) for i, r in BUCKETS])
    llm = ScriptedLlm([meaning], [query(sql, "table", {})],
                      [{"explanation": "The run's 3 latency buckets."}])
    outcome, record, _, _ = ask(cat, tmp_path, llm, [result], FakeGrafana(), buckets=True)
    assert outcome.status == "drawn", outcome.message
    assert record["steps"]["time_range"]["whole_run"] is True        # no ts filter asked for
    assert record["steps"]["3_queries"][0]["c1"] == "passed"


# ---- the pieces --------------------------------------------------------------------------

def test_time_words_are_within_the_run():
    span = (START, END)
    assert bounds({"kind": "whole_run"}, span).whole
    last = bounds({"kind": "last", "seconds": 60}, span)
    assert (last.start, last.end, last.whole) == (datetime(2026, 10, 15, 8, 30, 13), END, False)
    assert bounds({"kind": "last", "seconds": 99999}, span).whole      # longer than the run
    first = bounds({"kind": "between", "from_s": 30, "to_s": 90}, span)
    assert first.start == datetime(2026, 10, 15, 8, 28) and first.end == datetime(2026, 10, 15, 8, 29)
    assert bounds({"kind": "between", "from_s": 90, "to_s": 30}, span).whole  # nonsense: whole


def test_a_meaning_may_only_use_this_runs_data():
    names = {B1: {"read_iops", "ts"}}
    with pytest.raises(StepError, match="not a data model"):
        parse_meaning({"measures": [{"table": "linux_nvme_1_stats", "name": "read_iops"}]}, names)
    with pytest.raises(StepError, match="not a metric or column"):
        parse_meaning({"measures": [{"table": B1, "name": "read_ios"}]}, names)
    m = parse_meaning({"measures": [{"table": B1, "name": "read_iops"}], "chart": "pie"}, names)
    assert m.answerable and m.chart == "timeseries"


def test_the_panel_follows_the_shape_of_the_result():
    assert choose_panel("timeseries", ["DateTime", "Float64"], 200) == ("timeseries", None)
    assert choose_panel("stat", ["Float64"], 1)[0] == "stat"
    assert choose_panel("bar", ["String", "UInt64"], 14)[0] == "bar"
    kind, note = choose_panel("bar", ["DateTime", "Float64"], 200)
    assert kind == "timeseries" and "rather than bar" in note
    assert choose_panel("table", ["DateTime", "Float64"], 200)[0] == "table"


def test_the_grafana_panel_runs_the_checked_sql():
    ds = {"type": "grafana-clickhouse-datasource", "uid": "ch1"}
    p = panel("timeseries", "read IOPS", "what it shows", "SELECT 1", ds, ["ts", "read IOPS"],
              ["DateTime", "Float64"], {"read IOPS": "I/O per second"})
    assert p["type"] == "timeseries" and p["description"] == "what it shows"
    assert p["targets"][0]["rawSql"] == "SELECT 1" and p["targets"][0]["datasource"] == ds
    assert p["fieldConfig"]["defaults"]["unit"] == "iops"
    assert p["transformations"] == []
    bar = panel("bar", "sizes", "", "SELECT 1", ds, ["size", "bios"], ["String", "UInt64"], {})
    assert bar["type"] == "barchart" and bar["options"]["xField"] == "size"
    long = panel("timeseries", "reads per bucket", "", "SELECT 1", ds, ["ts", "bucket", "reads"],
                 ["DateTime", "String", "Int64"], {})
    assert long["transformations"] == [{"id": "prepareTimeSeries", "options": {"format": "multi"}}]
    assert dashboard_uid(RUN, "Show read IOPS") == dashboard_uid(RUN, "Show read IOPS")
    assert dashboard_uid(RUN, "Show read IOPS") != dashboard_uid(RUN, "Show write IOPS")
    assert len(dashboard_uid(RUN, "x")) <= 40


def test_c3_checks_names_not_plain_words():
    names = {"read_iops", "write_iops", "device", "range", "bucket"}
    sql = "SELECT linux_block_1_stats.read_iops FROM linux_block_1_stats"
    ok = "Read IOPS (read_iops) on the device, over the time range of the run, per bucket."
    assert explanation_problems(ok, [], "", names, sql) == []
    assert explanation_problems("Also write_iops.", [], "", names, sql) == \
        ["write_iops is mentioned, but the query does not use it"]


def test_a_reply_may_wrap_its_json():
    assert json_object('{"a": 1}') == {"a": 1}
    assert json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert json_object('Here it is: {"sql": "SELECT {x}"} - done') == {"sql": "SELECT {x}"}
    assert json_object("no json here") is None


def test_the_request_file(tmp_path):
    f = tmp_path / "request.yaml"
    f.write_text(f"run: /data/runs/{RUN}\nrequest: |\n  Show read IOPS\n  over time\n", encoding="utf-8")
    r = load_request(f)
    assert (r.run, r.text, r.db) == (f"/data/runs/{RUN}", "Show read IOPS over time", None)
    r = load_request(f, run="ProfileData-other", db="other_db")      # ask --run, --db
    assert (r.run, r.db) == ("ProfileData-other", "other_db")
    f.write_text("request: Show read IOPS\n", encoding="utf-8")
    with pytest.raises(RunError, match="needs 'run:'"):
        load_request(f)
    assert load_request(f, run=RUN).run == RUN                       # --run is enough


def test_the_example_requests_load():
    files = sorted((ROOT / "requests").glob("*.yaml"))
    assert len(files) >= 6                       # the five of the requirements, and more
    for f in files:
        r = load_request(f)
        assert r.run and r.text.endswith(".")
