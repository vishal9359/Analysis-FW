"""Module 1 tests (wrapper format + repeated child tables).
Run:  python -m pytest tests/ -v

The full pipeline runs against MemoryStore, so no database is needed. The real
ClickHouse insert is validated separately on a server.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import shutil

from analysis_fw.config import Config, StoreConfig
from analysis_fw.discover import discover
from analysis_fw.errors import ExitCode, InputError, SchemaError
from analysis_fw.registry import build_schema, build_schema_from_descriptor, create_table_ddl
from analysis_fw.runner import find_runs, run_batch, run_load
from analysis_fw.store.memory import MemoryStore
from analysis_fw.worker import load_unit, parse_ts

FIXTURE = HERE / "_fixture" / "ProfileData-fixture-20260727-180221"

NVME = "linux_nvme_1_stats"
BLOCK1 = "linux_block_1_stats"


def _cfg(workers=1, batch=250):
    return Config("profile_fw",
                  StoreConfig("localhost", 8123, "profile_fw", batch, workers, False),
                  "INFO", "text")


@pytest.fixture(scope="session", autouse=True)
def build_fixture():
    if not FIXTURE.exists():
        subprocess.run([sys.executable, str(HERE / "make_fixture.py"),
                        str(HERE / "_fixture")], check=True)


def _units():
    return {u.stem: u for u in discover(FIXTURE)}


def _schema(stem):
    return build_schema(stem, _units()[stem].proto_path)


# ---- discover -----------------------------------------------------------

def test_discover_finds_units_and_splits():
    u = _units()
    assert set(u) == {BLOCK1, "linux_block_2_misc", NVME}
    assert len(u[BLOCK1].pb_parts) == 2       # split wrapper files


# ---- registry: generic detection (scalar payload) ----------------------

def test_detects_record_generic_payload_wrapper():
    s = _schema(BLOCK1)
    assert s.record_cls.DESCRIPTOR.name == "StatLog"
    assert s.wrapper_cls.DESCRIPTOR.name == "StatLogs"
    assert s.repeated_field == "stat_logs"
    assert s.generic_field == "generic_format"
    assert s.payload_field == "payload"


def test_block_has_no_record_id_or_children():
    """Scalar-only payload → unchanged: no record_id, no child tables."""
    s = _schema(BLOCK1)
    assert s.has_record_id is False
    assert s.children == []
    assert "record_id" not in [c.name for c in s.columns]


def test_contract_names_enforced():
    d = Path(tempfile.mkdtemp())
    p = d / "x.proto"
    p.write_text('''syntax="proto3";
message GenericFormat { optional string timestamp=1; optional string hostname=2;
  optional uint32 component=3; optional string tag=4; optional uint32 log_level=5; }
message P { optional uint64 m=1; }
message Rec { GenericFormat generic_format=1; P blk_payload=2; }
message Log { repeated Rec rows=1; }''')
    with pytest.raises(SchemaError, match="no 'payload' field|requires both"):
        build_schema("x", p)


def test_flat_columns_and_ddl():
    s = _schema(BLOCK1)
    cols = [c.name for c in s.columns]
    assert cols[:7] == ["run_id", "ts", "timestamp", "hostname",
                        "component", "tag", "log_level"]
    ddl = create_table_ddl("db", s.stem, s.columns, s.order_by)
    assert "PARTITION BY run_id" in ddl
    assert "ORDER BY (run_id, hostname, ts)" in ddl


# ---- registry: repeated child tables (NVMe) ----------------------------

def test_nvme_children_detected():
    s = _schema(NVME)
    assert s.has_record_id is True
    assert "record_id" in [c.name for c in s.columns]
    kids = {c.table: c for c in s.children}
    assert set(kids) == {f"{NVME}_per_queue", f"{NVME}_per_core"}

    q = kids[f"{NVME}_per_queue"]
    assert q.repeated_field == "per_queue"
    qcols = [c.name for c in q.columns]
    assert "record_id" in qcols and "queue_id" in qcols
    # child ORDER BY puts the key dimension early for fast filtering
    assert q.order_by == ["run_id", "hostname", "queue_id", "ts"]

    c = kids[f"{NVME}_per_core"]
    assert c.order_by == ["run_id", "hostname", "cpu_id", "ts"]


# ---- timestamp ----------------------------------------------------------

def test_parse_ts_formats():
    assert parse_ts("Mon Jul 27 18:02:21 2026").year == 2026
    assert parse_ts("2026-07-27T18:02:21").minute == 2
    dt = parse_ts("Monday July 27 18:01:46:233")     # full names, no year, ms
    assert dt is not None and (dt.month, dt.day, dt.second) == (7, 27, 46)
    assert parse_ts("") is None and parse_ts("garbage") is None


def test_iso8601_rfc3339():
    """ISO 8601 / RFC 3339 with fractional seconds and Z/offset."""
    from analysis_fw.worker import ts_for_db
    assert parse_ts("2026-07-31T16:33:53.005Z").hour == 16
    # +05:30 offset is converted to the real UTC instant
    assert ts_for_db("2026-07-31T16:33:53.005+05:30").hour == 11
    assert ts_for_db("2026-07-31T16:33:53.005Z").hour == 16


def test_malformed_z_offset_treated_as_utc():
    """Producer bug: 'Z' followed by an offset ('...Z00:00', '...Z05:30').
    'Z' means UTC, so the trailing offset is dropped and it parses as UTC —
    not the 1970 epoch fallback."""
    from analysis_fw.worker import ts_for_db
    t = ts_for_db("2026-08-03T07:58:57.876Z00:00")   # the real Profile FW string
    assert (t.year, t.month, t.day, t.hour, t.minute, t.second) == (2026, 8, 3, 7, 58, 57)
    assert ts_for_db("2026-07-31T16:33:53.005Z05:30").hour == 16   # Z wins -> UTC


def test_ts_for_db_never_out_of_range():
    import struct
    from analysis_fw.worker import ts_for_db
    for s in ["Monday July 27 18:01:46:233", "Mon Jul 27 18:02:21 2026",
              "", "garbage", "Sat Jun 27 14:40:48 1969"]:
        epoch = int(ts_for_db(s).timestamp())
        assert 0 <= epoch <= 4294967295
        struct.pack("I", epoch)


# ---- end to end ---------------------------------------------------------

def test_full_load_counts():
    store = MemoryStore(); store.connect()
    report = run_load(FIXTURE, _cfg(), store=store)
    assert report.status == "complete"
    assert report.records == 1700                        # 1000 + 400 + 300
    assert store.count(BLOCK1) == 1000                   # both split files
    assert store.count("linux_block_2_misc") == 400
    assert store.count(NVME) == 300                      # main = 1 per record


def test_nvme_additive_child_rows():
    """Child rows are additive (per_queue + per_core), never a cross-product."""
    store = MemoryStore(); store.connect()
    run_load(FIXTURE, _cfg(), store=store)
    q = store.count(f"{NVME}_per_queue")
    c = store.count(f"{NVME}_per_core")
    assert q > 300 and c > 300          # more than one per record (repeated)
    assert c > q                        # 8..12 cores > 3..6 queues
    # additive, not multiplicative: total is main + q + c, nowhere near q*c
    assert q < 300 * 6 + 1 and c < 300 * 12 + 1


def test_nvme_record_id_correlation():
    """record_id is unique per record in main, and every child row links back."""
    store = MemoryStore(); store.connect()
    run_load(FIXTURE, _cfg(), store=store)

    mcols = [c.name for c in store.columns[NVME]]
    mi = mcols.index("record_id")
    main_ids = sorted(r[mi] for r in store.tables[NVME])
    assert main_ids == list(range(300))                  # unique 0..299

    for child in (f"{NVME}_per_queue", f"{NVME}_per_core"):
        ccols = [c.name for c in store.columns[child]]
        ci = ccols.index("record_id")
        child_ids = {r[ci] for r in store.tables[child]}
        assert child_ids == set(range(300))              # all records linked


def test_row_matches_illustration():
    store = MemoryStore(); store.connect()
    run_load(FIXTURE, _cfg(), store=store)
    cols = [c.name for c in store.columns[BLOCK1]]
    row = dict(zip(cols, store.tables[BLOCK1][0]))
    assert row["run_id"] == FIXTURE.name
    assert row["hostname"] == "spark-e97e"
    assert row["timestamp"] == "2026-07-27T18:02:21.000Z"   # RFC 3339 UTC
    assert row["ts"].year == 2026
    assert row["device"] == "nvme0n1"


# ---- extensibility & seams ---------------------------------------------

def test_added_payload_field_needs_no_code_change():
    d = Path(tempfile.mkdtemp())
    p = d / "x.proto"
    p.write_text('''syntax="proto3";
message GenericFormat { optional string timestamp=1; optional string hostname=2;
  optional uint32 component=3; optional string tag=4; optional uint32 log_level=5; }
message Payload { optional uint64 old_metric=1; optional uint64 new_metric=2; }
message StatLog { GenericFormat generic_format=1; Payload payload=2; }
message StatLogs { repeated StatLog stat_logs=1; }''')
    s = build_schema("x", p)
    assert "new_metric" in [c.name for c in s.columns]


def test_descriptor_rebuild_matches():
    s = _schema(NVME)
    rebuilt = build_schema_from_descriptor(
        NVME, s.descriptor_bytes, source=f"{NVME}.proto")
    assert [c.name for c in rebuilt.columns] == [c.name for c in s.columns]
    assert [ch.table for ch in rebuilt.children] == [ch.table for ch in s.children]


# ---- batch loading ------------------------------------------------------

def _make_batch(tmp_path, good=("t1", "t2"), broken=("bad",)):
    parent = tmp_path / "runs"
    parent.mkdir()
    for tag in good:
        shutil.copytree(FIXTURE, parent / f"ProfileData-{tag}-20260727-180221")
    for tag in broken:
        d = parent / f"ProfileData-{tag}-20260727-180221" / "Linux" / "Block"
        d.mkdir(parents=True)
        (d / f"{BLOCK1}.proto").write_text(_units()[BLOCK1].proto_path.read_text())
        (d / f"{BLOCK1}.pb").write_bytes(b"\xff\xff not protobuf")
    return parent


def test_find_runs_and_autodetect(tmp_path):
    parent = _make_batch(tmp_path, good=("t1", "t2"), broken=())
    names = [d.name for d in find_runs(parent)]
    assert names == ["ProfileData-t1-20260727-180221", "ProfileData-t2-20260727-180221"]
    # a single run directory has no ProfileData-* children -> not batch
    assert find_runs(FIXTURE) == []


def test_batch_all_good(tmp_path):
    parent = _make_batch(tmp_path, good=("t1", "t2", "t3"), broken=())
    b = run_batch(parent, _cfg(), store=MemoryStore(), continue_on_error=False)
    assert b.status == "complete" and b.exit_code == ExitCode.OK
    assert [o.status for o in b.outcomes] == ["complete"] * 3


def test_batch_fail_fast_stops_and_skips(tmp_path):
    # "bad" sorts before "t1"/"t2", so it fails first and the rest are skipped
    parent = _make_batch(tmp_path, good=("t1", "t2"), broken=("bad",))
    b = run_batch(parent, _cfg(), store=MemoryStore(), continue_on_error=False)
    statuses = {o.run_id: o.status for o in b.outcomes}
    assert statuses["ProfileData-bad-20260727-180221"] == "failed"
    assert statuses["ProfileData-t1-20260727-180221"] == "skipped"
    assert statuses["ProfileData-t2-20260727-180221"] == "skipped"
    assert b.exit_code == ExitCode.INPUT      # the failed run's class code


def test_batch_continue_on_error(tmp_path):
    parent = _make_batch(tmp_path, good=("t1", "t2"), broken=("bad",))
    b = run_batch(parent, _cfg(), store=MemoryStore(), continue_on_error=True)
    counts = {s: sum(1 for o in b.outcomes if o.status == s)
              for s in ("complete", "failed", "skipped")}
    assert counts == {"complete": 2, "failed": 1, "skipped": 0}
    assert b.exit_code == ExitCode.INPUT      # first failing run's code


def test_batch_empty_parent_errors(tmp_path):
    empty = tmp_path / "empty"; empty.mkdir()
    with pytest.raises(InputError, match="no ProfileData"):
        run_batch(empty, _cfg(), store=MemoryStore())


# ---- failure path -------------------------------------------------------

def test_corrupt_pb_gives_clear_error(tmp_path):
    d = tmp_path / "ProfileData-bad-1" / "Linux" / "Block"
    d.mkdir(parents=True)
    (d / f"{BLOCK1}.proto").write_text(_units()[BLOCK1].proto_path.read_text())
    (d / f"{BLOCK1}.pb").write_bytes(b"\xff\xff not a protobuf \x00\x01")
    store = MemoryStore(); store.connect()
    with pytest.raises(InputError, match="does not match|truncated|could not parse"):
        run_load(tmp_path / "ProfileData-bad-1", _cfg(), store=store)
