# Block metrics: IOPS, bandwidth, latency

`linux_block_1_stats` stores `/proc/diskstats` **cumulative counters** (totals
since boot). Per-second metrics are the **difference between consecutive
samples**, computed within one series with a window function.

## Fields

| Field | Meaning |
|---|---|
| `read_ios` / `write_ios` | total count of reads / writes |
| `sectors_read` / `sectors_written` | total sectors — **each sector = 512 bytes** (always) |
| `read_time_ms` / `write_time_ms` | total ms spent servicing reads / writes |

## Formulas (per 1-second sample; `Δ` = value at t minus value at t-1)

```
read_iops     = Δread_ios                                   (reads per second)
write_iops    = Δwrite_ios

read_MiB/s    = Δsectors_read    × 512 / 1048576            (bytes → MiB)
write_MiB/s   = Δsectors_written × 512 / 1048576

read_lat_ms   = Δread_time_ms  / Δread_ios                  (ms per read = r_await)
write_lat_ms  = Δwrite_time_ms / Δwrite_ios                 (ms per write = w_await)
```

Latency uses accumulated *service time*, not `1 / IOPS` — so it is correct even
when IOs overlap (queue depth > 1). If `Δios = 0` (no IOs that second) latency is
undefined → `NULL`.

## Worked example (3 cumulative samples)

```
ts    read_ios  sectors_read  read_time_ms | write_ios  sectors_written  write_time_ms
t1     200       8192          500          |  600        16384            1200
t2     250      12288          600          |  700        32768            1500
t3     350      20480          850          |  900        65536            2100
```

Take deltas and apply the formulas:

**t2** — Δread_ios 50, Δsectors_read 4096, Δread_time 100; Δwrite_ios 100, Δsectors_written 16384, Δwrite_time 300

```
read_iops    = 50                         write_iops   = 100
read_MiB/s   = 4096×512/1048576 = 2.0     write_MiB/s  = 16384×512/1048576 = 8.0
read_lat_ms  = 100/50  = 2.0              write_lat_ms = 300/100 = 3.0
```

**t3** — Δread_ios 100, Δsectors_read 8192, Δread_time 250; Δwrite_ios 200, Δsectors_written 32768, Δwrite_time 600

```
read_iops    = 100                        write_iops   = 200
read_MiB/s   = 8192×512/1048576 = 4.0     write_MiB/s  = 32768×512/1048576 = 16.0
read_lat_ms  = 250/100 = 2.5              write_lat_ms = 600/200 = 3.0
```

## Queries (one panel each)

Three separate panels — IOPS, bandwidth, and latency have very different scales,
so keeping them apart avoids one flattening the others.

Each is shown in **Grafana** form (dashboard variables `$run_id`, `$device`).
For **clickhouse-client**, make three edits: `$__timeFilter(ts)` +
`'$run_id'`/`'$device'` → literal values, `ts AS time` → `ts`, and
`ORDER BY time` → `ORDER BY ts`.

All three share the same window and the `rn > 1` guard that drops the first
sample of each series.

### 1. IOPS

```sql
SELECT ts AS time,
    d_read_ios  AS "read IOPS",
    d_write_ios AS "write IOPS"
FROM
(
    SELECT ts,
        row_number() OVER w AS rn,
        greatest(0, toInt64(read_ios)  - toInt64(lagInFrame(read_ios)  OVER w)) AS d_read_ios,
        greatest(0, toInt64(write_ios) - toInt64(lagInFrame(write_ios) OVER w)) AS d_write_ios
    FROM profile_fw.linux_block_1_stats
    WHERE $__timeFilter(ts) AND run_id = '$run_id' AND device = '$device'
    WINDOW w AS (PARTITION BY run_id, hostname, device ORDER BY ts)
)
WHERE rn > 1
ORDER BY time
```

### 2. Bandwidth (MiB/s)

```sql
SELECT ts AS time,
    round(d_sec_read    * 512 / 1048576, 3) AS "read MiB/s",
    round(d_sec_written * 512 / 1048576, 3) AS "write MiB/s"
FROM
(
    SELECT ts,
        row_number() OVER w AS rn,
        greatest(0, toInt64(sectors_read)    - toInt64(lagInFrame(sectors_read)    OVER w)) AS d_sec_read,
        greatest(0, toInt64(sectors_written) - toInt64(lagInFrame(sectors_written) OVER w)) AS d_sec_written
    FROM profile_fw.linux_block_1_stats
    WHERE $__timeFilter(ts) AND run_id = '$run_id' AND device = '$device'
    WINDOW w AS (PARTITION BY run_id, hostname, device ORDER BY ts)
)
WHERE rn > 1
ORDER BY time
```

### 3. Average latency (ms)

```sql
SELECT ts AS time,
    round(d_read_time  / nullIf(d_read_ios,  0), 3) AS "read latency ms",
    round(d_write_time / nullIf(d_write_ios, 0), 3) AS "write latency ms"
FROM
(
    SELECT ts,
        row_number() OVER w AS rn,
        greatest(0, toInt64(read_ios)      - toInt64(lagInFrame(read_ios)      OVER w)) AS d_read_ios,
        greatest(0, toInt64(write_ios)     - toInt64(lagInFrame(write_ios)     OVER w)) AS d_write_ios,
        greatest(0, toInt64(read_time_ms)  - toInt64(lagInFrame(read_time_ms)  OVER w)) AS d_read_time,
        greatest(0, toInt64(write_time_ms) - toInt64(lagInFrame(write_time_ms) OVER w)) AS d_write_time
    FROM profile_fw.linux_block_1_stats
    WHERE $__timeFilter(ts) AND run_id = '$run_id' AND device = '$device'
    WINDOW w AS (PARTITION BY run_id, hostname, device ORDER BY ts)
)
WHERE rn > 1
ORDER BY time
```

## Notes

- **Partition by `(run_id, hostname, device)`** so deltas never cross a device or
  run boundary.
- **`greatest(0, toInt64(x) - toInt64(lag))`** handles counter resets. The
  `toInt64` casts matter: `read_ios` is `UInt64`, and unsigned subtraction
  *underflows* to a huge number on a reset instead of going negative.
- **`nullIf(Δios, 0)`** makes latency `NULL` (a gap) when there were no IOs that
  second, rather than a misleading `0`.
- Assumes **one sample per second per device** (so Δ ≈ per-second rate). If
  intervals drift, divide by `dateDiff('second', prev_ts, ts)`.
- Latency here is the **average** over the second (`await`); percentiles are not
  derivable from diskstats (they need per-IO data).

## Try it on simulated data

`tests/make_fixture.py` generates a per-second time series with monotonic
counters, so these queries work end-to-end without waiting for real profiling —
see the README's "Generate simulated data" section.

## Latency buckets — labelling the counts

`linux_block_2_misc` stores latency distributions as counts per bucket, flattened from
the proto's `LatencyBuckets` sub-messages (see
[ADR-0010](decisions/0010-payload-field-shapes-to-tables.md)):

```
read_device_latency_latency_lh_opt1            IOs faster than threshold 1
read_device_latency_latency_bw_opt1_and_opt2   IOs between thresholds 1 and 2
...
read_device_latency_latency_bw_opt15_and_opt16
read_device_latency_latency_overflow           IOs slower than the last threshold
```

The columns say *which* bucket, never *what range* — the operator chooses that per run
with `-lat-buckets`. The ranges live in the `latency_buckets` table
([ADR-0011](decisions/0011-run-level-config-sidecars.md)), **one row per bucket**, with
the label already rendered:

```sql
SELECT option_index, `range`, threshold_us
FROM latency_buckets
WHERE run_id = {run:String}
ORDER BY option_index;
```

For `-lat-buckets 1,10,100,1000,10000,100000,1000000,10000000`:

| option_index | range | threshold_us |
|---|---|---|
| 0 | `< 1 µs` | 0 |
| 1 | `1-10 µs` | 1 |
| 2 | `10-100 µs` | 10 |
| 3 | `100 µs - 1 ms` | 100 |
| 4 | `1-10 ms` | 1000 |
| 5 | `10-100 ms` | 10000 |
| 6 | `100 ms - 1 s` | 100000 |
| 7 | `1-10 s` | 1000000 |
| 8 | `> 10 s` | 0 |

**n thresholds give n+1 buckets.** `option_index` is the bucket's position and lines up
with the proto's positional fields: 0 is `latency_lh_opt1`, *i* is
`latency_bw_opt<i>_and_opt<i+1>`, and *n* is `latency_overflow`. `threshold_us` is the
threshold that opens the bucket; the two open-ended buckets carry 0, and their bound is
readable in `range`.

> `range` is also a ClickHouse function name, so quote it as `` `range` `` in queries.

### Using the label in a panel

The label is pre-rendered precisely so a panel doesn't have to build it. Join on
`run_id` and the legend reads `100 µs - 1 ms` instead of `100000`. `DISTINCT` guards
against the duplicate rows an append-only re-load leaves behind
([ADR-0006](decisions/0006-mvp-append-only-ingest.md)):

```sql
SELECT DISTINCT option_index, `range`
FROM latency_buckets
WHERE run_id = {run:String}
ORDER BY option_index;
```

Map bucket *i* to its column with the proto's naming — `latency_lh_opt1` for 0,
`latency_bw_opt<i>_and_opt<i+1>` for the middle, `latency_overflow` for *n*.

### Bucket counts as a distribution

The bucket columns are **cumulative counters**, like everything else in these layers, so
take the per-second delta before charting. For one bucket:

```sql
SELECT
    ts,
    greatest(0, toInt64(read_device_latency_latency_bw_opt2_and_opt3)
              - toInt64(lagInFrame(toInt64(read_device_latency_latency_bw_opt2_and_opt3))
                        OVER (PARTITION BY run_id, hostname ORDER BY ts))) AS ios_10_to_100us
FROM linux_block_2_misc
WHERE run_id = {run:String}
ORDER BY ts;
```

For a Grafana heatmap, emit one series per bucket column and label each series from the
`latency_buckets` thresholds.

> **Comparing runs:** two runs profiled with different `-lat-buckets` values have
> different ranges behind identically-named columns. Always join through
> `latency_buckets` on `run_id`; never compare `..._bw_opt2_and_opt3` across runs
> without checking the thresholds match.
