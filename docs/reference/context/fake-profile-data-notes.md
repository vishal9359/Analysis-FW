# Fake Profile FW data generator

Stands in for the Go Profile FW writer until it exists. Produces a
`ProfileData-<tag>-<timestamp>` tree exactly as `profile-log-format-details.txt`
§2.1 describes, with each `.log` file holding length-delimited protobuf records
in the §3 / §4 format.

The point is to unblock the Analysis FW side — schema v0, loader CLI, query
catalog, Grafana, the billion-row scale test — without waiting on Profile FW.

---

## Quickstart

```bash
pip install -r requirements.txt

python generate_profile_data.py                 # 1000 records per component
python inspect_profile_data.py out/ProfileData-fio-4k-randread-20260627-144048
```

Protos compile themselves on first run (via the `protoc` bundled in
`grpcio-tools` — no system protoc needed) and recompile whenever a `.proto`
changes.

## What it produces

```
out/ProfileData-fio-4k-randread-20260627-144048/
    Config/run_config.log          <- empty (0 bytes), per the current decision
    Linux/Block/block.log          <- 1000 LinuxBlock records
    Linux/NVMe/nvme.log            <- 1000 LinuxNvme records
    Linux/Syscall/syscall.log      <- 1000 LinuxSyscall records
    Linux/Filesystem/filesystem.log
    Linux/Memory/memory.log
    SSD/ssd.log
```

Nothing else is written into the run directory — the tree matches the spec and
only the spec. `--manifest path.json` writes a JSON summary *outside* the tree.

## Changing things

Everything lives in `config.yaml`; nothing is hard-coded. The common ones:

| Want to change | Where |
|---|---|
| 1000 -> any number | `run.entries`, or `--entries N` |
| Per-component counts | `components.<name>.entries` (null = inherit) |
| Directory name | `output.dir_template`, `run.tag_name`, or `--tag` |
| File names / subdirs | `components.<name>.filename` / `.dir` |
| Which components | `components.<name>.enabled`, or `--components block,nvme` |
| Hostname | `run.hostname` |
| Timestamp format | `timestamp.format` |
| Run start / length | `timestamp.start`, `timeline.span_seconds` |
| Realistic event rate | `timeline.mode: rate` + `rate_per_second` |
| IO mix, sizes, latency | `components.<name>.payload.*` |
| Devices / processes / files | `environment.*` |
| Log rotation | `rotation.enabled` + `max_entries_per_file` |
| Framing | `encoding.framing` |

CLI flags override the config for one run; the config overrides the built-in
defaults. `--dry-run` prints the plan without writing. `--help` lists the rest.

## Decisions baked in

Three things the spec left ambiguous or conflicting, and what was chosen:

**Tag is numeric in the proto, string in the folder.** §4's sample says
`uint32 tag`, but §3.4 and the folder name imply a free-form string. Both exist
and mean different things: `run.tag_id` (uint32, in every record) and
`run.tag_name` (string, only in the folder name). Keep a real tag_id ↔ tag_name
mapping somewhere once this stops being fake data.

**Header timestamp is the §3.1 string, verbatim.** It is 1-second resolution,
so nanosecond timing lives in the *payloads* (`issue_ns`, `complete_ns`,
`device_latency_ns`, …) where latency math actually needs it. See "Open issues".

**Framing is varint length-delimited.** Protobuf is not self-delimiting, so
concatenated messages are unreadable without framing. `[varint len][message]`
is exactly what Go's `protodelim.MarshalTo` writes, so the Profile FW side is a
one-liner and this reader matches byte-for-byte. `encoding.framing: uint32be`
is also supported.

## What is invented and must be replaced

**Every payload.** Spec §3.6–§3.11 are all "TBD", so the payload of each
message was designed here, shaped around what eBPF can actually read at that
layer:

- **block** — `block:block_rq_insert/issue/complete`; carries the queue-vs-device
  latency split and the blk-mq `request_tag`.
- **nvme** — `nvme:nvme_setup_cmd/nvme_complete_rq`; qid/cid/opcode, and
  `device_latency_ns` = the number the host-vs-device overhead split needs.
- **syscall** — the only layer that knows the real application and path.
  Includes `mmap` and `io_uring_enter` because a read/write-only view misses
  mmap'd model loading entirely.
- **filesystem** — page cache hit/miss, readahead, ext4 extents, jbd2 commits;
  explains *why* a syscall did or did not become a block IO.
- **memory** — sampled gauge + vmstat deltas, deliberately a different shape
  from the event layers so the schema work exercises both.
- **ssd** — NVMe SMART log page 0x02 + OCP page 0xC0. Host-visible only:
  everything below the driver is not eBPF-visible. Counters are cumulative, so
  rates come from differencing consecutive samples at query time.

Treat the field *shapes* as the useful part, not the field list. When Profile FW
fills in §3.6–§3.11, replace the `Payload` blocks and regenerate.

## Known limitations

**Layers are not cross-correlated.** Each component is generated independently.
A block record and an NVMe record with the same `request_tag` are *not* the same
IO — `nvme.block_request_tag` is written as 0. Real stitching
(fd+offset → bio → request → NVMe cid) is the open technical problem and is
deferred past POC 1; the field exists so the schema and the ASOF JOIN query can
be written against it now. All layers do draw from the same
`environment` pools, so devices/pids/paths are consistent across files.

**Throughput is ~72K records/s single-process** (measured, 1.2M records in
16.6s). A billion rows single-process is ~3.8 h. For the billion-row scale test,
either run several processes in parallel with different `--tag`/`--out`, or —
cheaper — generate ~50M rows once and load them repeatedly into ClickHouse under
different `run_id` partitions. That test is about query performance, not about
how the bytes were made.

**Record size is ~179 B average**, not the ~80 B/event the sizing math assumes.
See "Open issues".

## Open issues to settle with the team

1. **~179 B/record vs the assumed ~80 B.** Measured across 1.2M generated
   records (block 167 B, nvme 140 B, syscall 171 B, filesystem 211 B). At 1M
   ev/s that is ~179 MB/s raw per SUT rather than 80 MB/s, and ~770 MB/s
   aggregate at 30 SUTs after ~7x zstd rather than ~450 MB/s. Still fine on
   25GbE, but it moves the storage/retention math by ~2x. Caveat: these payloads
   are invented and likely richer than the final ones, and proto3 omits
   zero-valued fields, so a sparser real payload will be smaller. Worth
   re-measuring once §3.6–§3.11 are real.

2. **1-second header timestamps.** §3.1's format cannot order events inside a
   second, and it is the intended ClickHouse `ORDER BY` key. The payload `*_ns`
   fields cover it for now, but the header itself should probably grow a
   `uint64 timestamp_ns`. Wire-compatible to add later; cheaper to decide now.

3. **`uint32 component` / `uint32 loglevel` vs enums.** `common.proto` carries
   the integer registry as enums. proto3 enum fields are varint, identical on
   the wire to uint32 — so switching later is wire-compatible and old `.log`
   files stay readable. Worth doing for readability on the Go side.

4. **`platform.proto`** is listed in spec §4 (CPU/GPU/memory utilization) but has
   no file in the §2.1 hierarchy. Not generated. Where does it land?

5. **`run_config.log` contents.** Currently empty by design.
   `proto/run_config.proto` proposes a shape — notably `seed` + `config_sha256`,
   which would make a run reproducible from the log folder alone, and
   `events_emitted` / `events_dropped` for the loss reconciliation the
   architecture requires. Set `run_config.mode: message` to try it.

---

# Loading into ClickHouse

## One-time setup on the Linux box

```bash
# 1. copy this folder over (from Windows)
scp -r fake-profile-data user@linuxbox:~/

# 2. on the Linux box
cd ~/fake-profile-data
python3 -m pip install -r requirements.txt

# 3. ClickHouse must be reachable on the HTTP port (8123), not just 9000.
#    If your container was started without -p 8123:8123, recreate it:
docker run -d --name clickhouse -p 8123:8123 -p 9000:9000 \
    -v ch_data:/var/lib/clickhouse clickhouse/clickhouse-server

curl http://localhost:8123/ping     # -> Ok.
```

## Every run

```bash
python3 generate_profile_data.py                       # writes out/ProfileData-...
python3 load_to_clickhouse.py out/ProfileData-fio-4k-randread-20260627-144048 \
        --host localhost --create-schema
```

`--create-schema` applies `schema/*.sql` (idempotent — `CREATE ... IF NOT
EXISTS`), so it is safe every time. Drop it once the tables exist.

To reload the same run after regenerating:

```bash
python3 load_to_clickhouse.py out/ProfileData-... --replace
```

Without `--replace` the loader refuses rather than silently double-loading.

## Why reload is clean

`PARTITION BY run_id` means one run = one partition, so `--replace` is
`ALTER TABLE ... DROP PARTITION` — an O(1) metadata operation, not a
delete-by-predicate. Load the same tree twice and you get exactly one copy.
This is the whole reason run_id is the partition key.

## What the loader checks

It counts records decoded, rows sent, and rows actually in the table
afterwards, and exits non-zero if they disagree. "Events emitted vs rows in DB"
is the reconciliation the real pipeline has to answer — it starts here.

It also parses the §3.1 header string into `ts_header` alongside the
nanosecond-derived `ts`, so the two can be cross-checked:

```sql
SELECT count() FROM profile.block
WHERE run_id = '<run_id>' AND toStartOfSecond(ts) != ts_header;   -- expect 0
```

That check is not decoration. Real eBPF `bpf_ktime_get_ns()` is **boot-relative,
not epoch**. This generator emits epoch nanoseconds, so the two agree today. If
Profile FW ships monotonic ns, `ts` silently becomes "1970 + uptime" and this
query is what catches it. **Worth settling with the Go side: are payload ns
fields epoch, or monotonic + a boot-time offset in run_config?**

## Verify the load

```sql
SELECT count() FROM profile.block WHERE run_id = '<run_id>';

-- latency by operation; note quantile, not avg -- averages hide the tail
SELECT op, count() AS ios,
       round(quantile(0.50)(device_latency_ns)/1000, 1) AS p50_us,
       round(quantile(0.99)(device_latency_ns)/1000, 1) AS p99_us,
       round(max(device_latency_ns)/1000, 1) AS max_us
FROM profile.block WHERE run_id = '<run_id>' GROUP BY op ORDER BY ios DESC;

-- host vs device overhead, the split this framework exists to measure
SELECT round(avg(queue_latency_ns)/1000, 1) AS host_queue_us,
       round(avg(device_latency_ns)/1000, 1) AS device_us
FROM profile.block WHERE run_id = '<run_id>';

-- compression actually achieved, per column
SELECT name, formatReadableSize(data_compressed_bytes) AS comp,
       formatReadableSize(data_uncompressed_bytes) AS uncomp,
       round(data_uncompressed_bytes / data_compressed_bytes, 1) AS ratio
FROM system.columns WHERE database = 'profile' AND table = 'block'
ORDER BY data_compressed_bytes DESC LIMIT 10;
```

## Two things to know before you add rollups

**Materialized views only fire on INSERT.** If you create a rollup MV *after*
loading, it stays empty for the data already there — you must backfill it by
hand. Create MVs before the first load of a run, or plan the backfill.

**Rollups must carry min/max/quantile state, never averages alone.** An average
erases the spike that made you look. `AggregatingMergeTree` with
`quantileTDigestState` / `minState` / `maxState` is the shape to use.

## Files

| File | What |
|---|---|
| `config.yaml` | every knob; the only file you normally edit |
| `generate_profile_data.py` | the generator |
| `inspect_profile_data.py` | reads a tree back, verifies it, dumps records |
| `load_to_clickhouse.py` | loads a tree into ClickHouse, reconciles counts |
| `schema/10_tables.sql` | schema v0 DDL — the hard-to-change decisions live here |
| `proto/*.proto` | the shared contract — hand these to the Profile FW engineer |
| `pb/` | generated `*_pb2.py`, rebuilt automatically; not worth committing |
| `out/` | generated data |

## inspect_profile_data.py

Verifies rather than trusts: it decodes every record, checks `event_seq` is a
dense 1..N run (a gap means dropped records), and reports the level mix and
time range per component.

```bash
python inspect_profile_data.py <run_dir>              # summary + verification
python inspect_profile_data.py <run_dir> --dump 3     # print records in full
python inspect_profile_data.py <run_dir> --component block --dump 5
python inspect_profile_data.py <run_dir> --json       # machine-readable
```

It exits non-zero on any decode failure or seq gap, so it works in CI. Its
`iter_records()` decode loop is the loop the Analysis FW loader needs — copy it.
Note what the framing buys: a truncated file (crashed writer, partial flush)
raises at a known record boundary instead of silently yielding garbage.
