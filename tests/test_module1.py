"""Module 1 tests. Run:  python -m pytest tests/ -v   (from analysis-fw/)

The full pipeline is exercised against MemoryStore, so no database is needed.
The real ClickHouse insert is validated separately on a server.
"""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from analysis_fw.config import Config, StoreConfig
from analysis_fw.discover import discover
from analysis_fw.errors import InputError, SchemaError
from analysis_fw.registry import build_schema, create_table_ddl
from analysis_fw.runner import run_load
from analysis_fw.store.memory import MemoryStore
from analysis_fw.worker import load_unit, parse_ts

FIXTURE = HERE / "_fixture" / "ProfileData-fixture-20260627-144048"


def _cfg(workers=1, batch=250):
    return Config("profile_fw",
                  StoreConfig("localhost", 8123, "profile_fw", batch, workers, False),
                  "INFO", "text")


@pytest.fixture(scope="session", autouse=True)
def build_fixture():
    if not FIXTURE.exists():
        subprocess.run([sys.executable, str(HERE / "make_fixture.py"),
                        str(HERE / "_fixture")], check=True)


# ---- discover -----------------------------------------------------------

def test_discover_finds_all_units():
    units = {u.stem: u for u in discover(FIXTURE)}
    assert set(units) == {"block_type1", "block_type2", "nvme_type1", "ssd_type1"}
    assert len(units["block_type1"].pb_parts) == 2      # split
    assert units["block_type1"].pb_parts[0].name.endswith(".000.pb")  # ordered


def test_discover_ignores_config():
    for u in discover(FIXTURE):
        assert "Config" not in u.layer_dir


def test_discover_missing_proto(tmp_path):
    d = tmp_path / "ProfileData-x-1" / "Linux" / "Block"
    d.mkdir(parents=True)
    (d / "block_type1.pb").write_bytes(b"")
    with pytest.raises(InputError, match="no schema"):
        discover(tmp_path / "ProfileData-x-1")


# ---- registry -----------------------------------------------------------

def test_schema_columns_and_types():
    u = {x.stem: x for x in discover(FIXTURE)}["block_type2"]
    s = build_schema(u.stem, u.proto_path)
    cols = {c.name: c.ch_type for c in s.columns}
    assert cols["run_id"] == "String" and cols["ts"] == "DateTime"
    assert cols["util_pct"] == "Float64" and cols["throttled"] == "Bool"
    assert "PARTITION BY (run_id, sut_id)" in create_table_ddl("db", s)


def test_schema_rejects_missing_header(tmp_path):
    p = tmp_path / "bad.proto"
    p.write_text('syntax="proto3"; package b; '
                 'message Rec { string hostname = 2; message Payload { uint32 x = 1; } '
                 'Payload payload = 6; }')
    with pytest.raises(SchemaError, match="missing contract header"):
        build_schema("bad", p)


def test_schema_rejects_repeated_payload(tmp_path):
    p = tmp_path / "rep.proto"
    p.write_text('syntax="proto3"; package r; message Rec { '
                 'string timestamp=1; string hostname=2; uint32 component=3; '
                 'uint32 tag=4; uint32 loglevel=5; '
                 'message Payload { repeated uint64 xs = 1; } Payload payload=6; }')
    with pytest.raises(SchemaError, match="repeated"):
        build_schema("rep", p)


# ---- timestamp ----------------------------------------------------------

def test_parse_ts_with_and_without_brackets():
    a = parse_ts("Sat Jun 27 14:40:48 2026")
    b = parse_ts("[Sat Jun 27 14:40:48 2026]")
    assert a == b
    assert (a.year, a.month, a.day, a.hour) == (2026, 6, 27, 14)


# ---- end to end ---------------------------------------------------------

def test_full_load_counts():
    store = MemoryStore(); store.connect()
    report = run_load(FIXTURE, _cfg(), store=store)
    assert report.status == "complete"
    assert report.read == report.inserted == 2050
    assert store.count("block_type1") == 1000   # both split parts
    assert store.count("ssd_type1") == 50


def test_row_shape():
    store = MemoryStore(); store.connect()
    run_load(FIXTURE, _cfg(), store=store)
    cols = [c.name for c in store.schemas["block_type1"].columns]
    row = store.tables["block_type1"][0]
    assert len(row) == len(cols)
    d = dict(zip(cols, row))
    assert d["run_id"] == FIXTURE.name
    assert d["sut_id"] == "dgx-spark-01"


# ---- failure paths ------------------------------------------------------

def test_truncated_file_detected(tmp_path):
    # a valid fixture file with its last 20 bytes chopped off
    src = discover(FIXTURE)[0]
    part = src.pb_parts[0]
    d = tmp_path / "ProfileData-trunc-1" / "Linux" / "Block"
    d.mkdir(parents=True)
    (d / "block_type1.proto").write_text(src.proto_path.read_text())
    data = part.read_bytes()
    (d / "block_type1.pb").write_bytes(data[:-20])
    store = MemoryStore(); store.connect()
    with pytest.raises(InputError, match="truncated"):
        run_load(tmp_path / "ProfileData-trunc-1", _cfg(), store=store)


def test_second_hostname_rejected(tmp_path):
    # two records with different hostnames in one file
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    from grpc_tools import protoc
    import grpc_tools, tempfile
    src = discover(FIXTURE)[0]
    proto_txt = src.proto_path.read_text()
    td = Path(tempfile.mkdtemp()); (td / "s.proto").write_text(proto_txt)
    inc = Path(grpc_tools.__file__).parent / "_proto"
    out = td / "d.desc"
    protoc.main(["protoc", f"-I{td}", f"-I{inc}", f"--descriptor_set_out={out}",
                 "--include_imports", str(td / "s.proto")])
    pool = descriptor_pool.DescriptorPool()
    fds = descriptor_pb2.FileDescriptorSet(); fds.ParseFromString(out.read_bytes())
    for f in fds.file: pool.Add(f)
    pkg = fds.file[-1].package; name = fds.file[-1].message_type[0].name
    Cls = message_factory.GetMessageClass(pool.FindMessageTypeByName(f"{pkg}.{name}"))

    def framed(host):
        m = Cls(); m.timestamp = "Sat Jun 27 14:40:48 2026"; m.hostname = host
        m.component = 1; m.tag = 7; m.loglevel = 3; m.payload.event_seq = 1
        b = m.SerializeToString()
        return struct.pack("B", len(b)) + b if len(b) < 128 else None

    d = tmp_path / "ProfileData-multi-1" / "Linux" / "Block"
    d.mkdir(parents=True)
    (d / "block_type1.proto").write_text(proto_txt)
    (d / "block_type1.pb").write_bytes(framed("host-a") + framed("host-b"))
    store = MemoryStore(); store.connect()
    with pytest.raises(InputError, match="one SUT"):
        run_load(tmp_path / "ProfileData-multi-1", _cfg(), store=store)
