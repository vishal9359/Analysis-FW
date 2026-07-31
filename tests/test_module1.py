"""Module 1 tests (wrapper format). Run:  python -m pytest tests/ -v

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

from analysis_fw.config import Config, StoreConfig
from analysis_fw.discover import discover
from analysis_fw.errors import InputError, SchemaError
from analysis_fw.registry import build_schema, build_schema_from_descriptor, create_table_ddl
from analysis_fw.runner import run_load
from analysis_fw.store.memory import MemoryStore
from analysis_fw.worker import load_unit, parse_ts

FIXTURE = HERE / "_fixture" / "ProfileData-fixture-20260727-180221"


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


# ---- discover -----------------------------------------------------------

def test_discover_finds_units_and_splits():
    u = _units()
    assert set(u) == {"linux_block_1_stats", "linux_block_2_misc"}
    assert len(u["linux_block_1_stats"].pb_parts) == 2       # split wrapper files
    assert u["linux_block_2_misc"].pb_parts[0].name.endswith(".pb")


# ---- registry: generic detection ---------------------------------------

def test_detects_record_generic_payload_wrapper():
    s = build_schema("linux_block_1_stats", _units()["linux_block_1_stats"].proto_path)
    # standardized message names (agreed contract): StatLog / StatLogs
    assert s.record_cls.DESCRIPTOR.name == "StatLog"
    assert s.wrapper_cls.DESCRIPTOR.name == "StatLogs"
    assert s.repeated_field == "stat_logs"
    assert s.generic_field == "generic_format"      # agreed contract name
    assert s.payload_field == "payload"             # agreed contract name


def test_contract_names_enforced():
    """A record whose payload field is not named `payload` is rejected."""
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


def test_flat_columns_and_types():
    s = build_schema("linux_block_1_stats", _units()["linux_block_1_stats"].proto_path)
    cols = [c.name for c in s.columns]
    assert cols[:7] == ["run_id", "ts", "timestamp", "hostname",
                        "component", "tag", "log_level"]
    assert cols[-1] == "_loaded_at"
    types = {c.name: c.ch_type for c in s.columns}
    assert types["tag"] == "String"          # tag is a string now
    assert types["component"] == "UInt32"
    assert types["read_ios"] == "UInt64"
    assert "PARTITION BY run_id" in create_table_ddl("db", s)


def test_rejects_proto_without_generic_format(tmp_path):
    p = tmp_path / "bad.proto"
    p.write_text('syntax="proto3"; message Foo { uint64 x = 1; } '
                 'message Bar { repeated Foo items = 1; }')
    with pytest.raises(SchemaError, match="no record message"):
        build_schema("bad", p)


# ---- timestamp ----------------------------------------------------------

def test_parse_ts_formats():
    assert parse_ts("Mon Jul 27 18:02:21 2026").year == 2026
    assert parse_ts("[Mon Jul 27 18:02:21 2026]").hour == 18
    assert parse_ts("2026-07-27T18:02:21").minute == 2
    # full names, NO year, trailing ms (the real block_1 shape)
    dt = parse_ts("Monday July 27 18:01:46:233")
    assert dt is not None and (dt.month, dt.day, dt.second) == (7, 27, 46)
    assert parse_ts("") is None and parse_ts("garbage") is None


def test_ts_for_db_never_out_of_range():
    """The `ts` value must always pack into ClickHouse's unsigned DateTime
    (0..4294967295) — this is the insert crash that was fixed."""
    import struct
    from analysis_fw.worker import ts_for_db
    for s in ["Monday July 27 18:01:46:233", "Mon Jul 27 18:02:21 2026",
              "", "garbage", "Sat Jun 27 14:40:48 1969"]:
        epoch = int(ts_for_db(s).timestamp())
        assert 0 <= epoch <= 4294967295
        struct.pack("I", epoch)          # must not raise


# ---- end to end ---------------------------------------------------------

def test_full_load_flat_rows():
    store = MemoryStore(); store.connect()
    report = run_load(FIXTURE, _cfg(), store=store)
    assert report.status == "complete"
    assert report.read == report.inserted == 1400
    assert store.count("linux_block_1_stats") == 1000     # both split files
    assert store.count("linux_block_2_misc") == 400


def test_row_matches_illustration():
    store = MemoryStore(); store.connect()
    run_load(FIXTURE, _cfg(), store=store)
    s = store.schemas["linux_block_1_stats"]
    cols = [c.name for c in s.columns]
    row = dict(zip(cols, store.tables["linux_block_1_stats"][0]))
    assert row["run_id"] == FIXTURE.name
    assert row["hostname"] == "spark-e97e"
    assert row["tag"] == "test1"
    assert row["timestamp"] == "Mon Jul 27 18:02:21 2026"
    assert row["ts"].year == 2026            # parsed DateTime
    assert row["device"] == "nvme0n1"


# ---- extensibility: add a field with no code change --------------------

def test_added_payload_field_needs_no_code_change():
    """A new payload field appears as a new column automatically."""
    proto = tmp_proto_with_extra_field()
    s = build_schema("x", proto)
    assert "new_metric" in [c.name for c in s.columns]


def tmp_proto_with_extra_field() -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "x.proto"
    p.write_text('''syntax="proto3";
message GenericFormat { optional string timestamp=1; optional string hostname=2;
  optional uint32 component=3; optional string tag=4; optional uint32 log_level=5; }
message P { optional uint64 old_metric=1; optional uint64 new_metric=2; }
message Rec { GenericFormat generic_format=1; P payload=2; }
message Log { repeated Rec rows=1; }''')
    return p


# ---- failure path: .pb doesn't match .proto ----------------------------

def test_corrupt_pb_gives_clear_error(tmp_path):
    d = tmp_path / "ProfileData-bad-1" / "Linux" / "Block"
    d.mkdir(parents=True)
    src = _units()["linux_block_1_stats"]
    (d / "linux_block_1_stats.proto").write_text(src.proto_path.read_text())
    (d / "linux_block_1_stats.pb").write_bytes(b"\xff\xff not a protobuf \x00\x01")
    store = MemoryStore(); store.connect()
    with pytest.raises(InputError, match="does not match|truncated|could not parse"):
        run_load(tmp_path / "ProfileData-bad-1", _cfg(), store=store)


# ---- seam: child-process schema rebuild matches parent -----------------

def test_descriptor_rebuild_matches():
    s = build_schema("linux_block_1_stats", _units()["linux_block_1_stats"].proto_path)
    rebuilt = build_schema_from_descriptor(
        "linux_block_1_stats", s.descriptor_bytes, source="linux_block_1_stats.proto")
    assert [c.name for c in rebuilt.columns] == [c.name for c in s.columns]
    assert rebuilt.payload_field == s.payload_field
    assert rebuilt.repeated_field == s.repeated_field
