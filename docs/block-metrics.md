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

`mkfixture` generates a per-second time series with monotonic
counters, so these queries work end-to-end without waiting for real profiling —
see the README's "Generate simulated data" section.
