-- Average WRITE latency across layers (syscall, block, nvme), per-second.
-- Output is WIDE: one column per layer (`syscall write`, `block write`, `nvme write`),
-- pivoted with max(if(metric=...)) so a Grafana time series shows one line per layer.
--
-- Latency = accumulated service time / IO count, both as per-second deltas
-- (Δtime / Δios) -- the "await" definition, correct even at queue depth > 1.
--
-- UNITS: values are in MICROSECONDS (µs). Source units differ per layer, so each
-- is converted to µs:
--   block / nvme : *_time_ms is milliseconds -> µs is  d_time * 1000 / d_ios
--   syscall      : total_time is nanoseconds -> µs is  d_time / (d_ios * 1000)
-- Confirm each source unit on the box; if one differs, fix that layer's factor:
--   ms->µs = *1000, ns->µs = /1000, s->µs = *1e6, already-µs = *1.
--
-- One CTE per layer computes the deltas in its own window; `ORDER BY time OFFSET 1`
-- drops that series' first (deltaless) row. if(d_ios = 0, NULL, ...) makes an idle
-- second a gap instead of a divide-by-zero.
--
-- OFFSET 1 drops exactly ONE row per CTE, so it assumes a single series per layer
-- (one run_id / host / device). With multiple devices or hosts, drop each series'
-- first row with a `prev IS NOT NULL` guard per partition instead.
--
-- io_type = 5 is plain write() only; if the workload uses writev / pwrite / io_uring
-- writes, sum io_count and total_time over io_type IN (5,6,7,8,16,17,18) first.
--
-- Grafana form (uses $__timeFilter). For clickhouse-client:
--   USE profile_fw;  drop the `profile_fw.` prefixes,
--   replace `$__timeFilter(ts)` with a real range (keep `AND io_type = 5`),
--   `ts AS time` -> `ts`, and the ORDER BYs `time` -> `ts`.

WITH
syscall_delta AS
(
    SELECT ts AS time,
        greatest(0, toInt64(total_time) - toInt64(lagInFrame(total_time) OVER w)) AS d_time,
        greatest(0, toInt64(io_count)   - toInt64(lagInFrame(io_count)   OVER w)) AS d_ios
    FROM profile_fw.linux_syscall_1_stats_io_patterns
    WHERE $__timeFilter(ts) AND io_type = 5
    WINDOW w AS (PARTITION BY run_id, hostname ORDER BY ts)
    ORDER BY time
    OFFSET 1
),
block_delta AS
(
    SELECT ts AS time,
        greatest(0, toInt64(write_time_ms) - toInt64(lagInFrame(write_time_ms) OVER w)) AS d_time,
        greatest(0, toInt64(write_ios)     - toInt64(lagInFrame(write_ios)     OVER w)) AS d_ios
    FROM profile_fw.linux_block_1_stats
    WHERE $__timeFilter(ts)
    WINDOW w AS (PARTITION BY run_id, hostname, device ORDER BY ts)
    ORDER BY time
    OFFSET 1
),
nvme_delta AS
(
    SELECT ts AS time,
        greatest(0, toInt64(write_time_ms) - toInt64(lagInFrame(write_time_ms) OVER w)) AS d_time,
        greatest(0, toInt64(write_ios)     - toInt64(lagInFrame(write_ios)     OVER w)) AS d_ios
    FROM profile_fw.linux_nvme_1_stats
    WHERE $__timeFilter(ts)
    WINDOW w AS (PARTITION BY run_id, hostname, device ORDER BY ts)
    ORDER BY time
    OFFSET 1
)
SELECT
    time,
    max(if(metric = 'syscall write', value, NULL)) AS `syscall write`,
    max(if(metric = 'block write',   value, NULL)) AS `block write`,
    max(if(metric = 'nvme write',    value, NULL)) AS `nvme write`
FROM
(
    SELECT time, 'syscall write' AS metric, if(d_ios = 0, NULL, round(d_time / (d_ios * 1000), 3)) AS value FROM syscall_delta
    UNION ALL
    SELECT time, 'block write'   AS metric, if(d_ios = 0, NULL, round(d_time * 1000 / d_ios,   3)) AS value FROM block_delta
    UNION ALL
    SELECT time, 'nvme write'    AS metric, if(d_ios = 0, NULL, round(d_time * 1000 / d_ios,   3)) AS value FROM nvme_delta
)
GROUP BY time
ORDER BY time
