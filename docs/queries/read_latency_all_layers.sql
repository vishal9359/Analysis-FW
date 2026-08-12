-- Average READ latency across layers (syscall, block, nvme), per-second.
--
-- Latency = accumulated service time / IO count, both taken as per-second deltas
-- (Δtime / Δios). This is the "await" definition -- correct even when IOs overlap
-- (queue depth > 1), unlike 1/IOPS.
--
--   read_latency = Δread_time / Δread_ios
--
-- One CTE per layer computes the deltas within its own window; `ORDER BY time
-- OFFSET 1` drops that series' first (deltaless) row -- its lag is 0, so its raw
-- delta would be the whole counter (a false spike). nullIf(Δios, 0) makes a
-- second with no reads NULL (a gap) instead of a divide-by-zero.
--
-- Column mapping per layer:
--   syscall : total_time / io_count for READ rows (io_type = 1) in the child table
--   block   : read_time_ms / read_ios
--   nvme    : read_time_ms / read_ios
-- (SSD omitted: SMART data has no per-command service time.)
--
-- OFFSET 1 drops exactly ONE row per CTE, so it assumes a single series per layer
-- (one run_id / host / device). With multiple devices or hosts, each series' first
-- row must be dropped instead -- use a `prev IS NOT NULL` guard per partition.
--
-- UNITS -- read before trusting the syscall series:
--   block / nvme *_time_ms are milliseconds, so read_latency_ms is in ms.
--   syscall total_time unit is NOT ms in general (eBPF tracers usually emit ns).
--   Confirm the unit; if ns, wrap the syscall time with `/ 1e6` to get ms.
--
-- Grafana form (uses $__timeFilter). For clickhouse-client:
--   USE profile_fw;  drop the `profile_fw.` prefixes,
--   replace `$__timeFilter(ts)` with a real range (keep `AND io_type = 1`),
--   `ts AS time` -> `ts`, and the ORDER BYs `time` -> `ts`.

WITH
syscall_delta AS
(
    SELECT ts AS time,
        greatest(0, toInt64(total_time) - toInt64(lagInFrame(total_time) OVER w)) AS d_time,
        greatest(0, toInt64(io_count)   - toInt64(lagInFrame(io_count)   OVER w)) AS d_ios
    FROM profile_fw.linux_syscall_1_stats_io_patterns
    WHERE $__timeFilter(ts) AND io_type = 1
    WINDOW w AS (PARTITION BY run_id, hostname ORDER BY ts)
    ORDER BY time
    OFFSET 1
),
block_delta AS
(
    SELECT ts AS time,
        greatest(0, toInt64(read_time_ms) - toInt64(lagInFrame(read_time_ms) OVER w)) AS d_time,
        greatest(0, toInt64(read_ios)     - toInt64(lagInFrame(read_ios)     OVER w)) AS d_ios
    FROM profile_fw.linux_block_1_stats
    WHERE $__timeFilter(ts)
    WINDOW w AS (PARTITION BY run_id, hostname, device ORDER BY ts)
    ORDER BY time
    OFFSET 1
),
nvme_delta AS
(
    SELECT ts AS time,
        greatest(0, toInt64(read_time_ms) - toInt64(lagInFrame(read_time_ms) OVER w)) AS d_time,
        greatest(0, toInt64(read_ios)     - toInt64(lagInFrame(read_ios)     OVER w)) AS d_ios
    FROM profile_fw.linux_nvme_1_stats
    WHERE $__timeFilter(ts)
    WINDOW w AS (PARTITION BY run_id, hostname, device ORDER BY ts)
    ORDER BY time
    OFFSET 1
)
SELECT time, 'syscall' AS layer, round(d_time / nullIf(d_ios, 0), 3) AS read_latency_ms FROM syscall_delta
UNION ALL
SELECT time, 'block'   AS layer, round(d_time / nullIf(d_ios, 0), 3) AS read_latency_ms FROM block_delta
UNION ALL
SELECT time, 'nvme'    AS layer, round(d_time / nullIf(d_ios, 0), 3) AS read_latency_ms FROM nvme_delta
ORDER BY time, indexOf(['syscall', 'block', 'nvme'], layer)
