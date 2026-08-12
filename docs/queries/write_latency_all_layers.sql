-- Average WRITE latency across layers (syscall, block, nvme), per-second.
--
-- Latency = accumulated service time / IO count, both taken as per-second deltas
-- (Δtime / Δios). This is the "await" definition -- correct even when IOs overlap
-- (queue depth > 1), unlike 1/IOPS.
--
--   write_latency = Δwrite_time / Δwrite_ios
--
-- The lagged values are Nullable so the first row of each series is dropped by
-- `prev_ios IS NOT NULL` (no predecessor -> no delta). nullIf(Δios, 0) makes a
-- second with no writes NULL (a gap) instead of a divide-by-zero.
--
-- Column mapping per layer:
--   syscall : total_time / io_count for WRITE rows (io_type = 5) in the child table
--   block   : write_time_ms / write_ios
--   nvme    : write_time_ms / write_ios
-- (SSD omitted: SMART data has no per-command service time.)
--
-- UNITS -- read before trusting the syscall series:
--   block / nvme *_time_ms are milliseconds, so write_latency_ms is in ms.
--   syscall total_time unit is NOT ms in general (eBPF tracers usually emit ns).
--   Confirm the unit; if ns, wrap the syscall time with `/ 1e6` to get ms:
--     toInt64(total_time) / 1e6  (and the same on the lagged value).
--
-- io_type = 5 is plain write() only; if the workload uses writev / pwrite / io_uring
-- writes, sum io_count and total_time over io_type IN (5,6,7,8,16,17,18) first.
--
-- Grafana form (uses $__timeFilter). For clickhouse-client:
--   USE profile_fw;  drop the `profile_fw.` prefixes,
--   replace `$__timeFilter(ts)` with a real range (keep `AND io_type = 5`),
--   `ts AS time` -> `ts`, and the ORDER BY `time` -> `ts`.

SELECT ts AS time, layer,
    round(greatest(0, cur_time - prev_time) / nullIf(greatest(0, cur_ios - prev_ios), 0), 3) AS write_latency_ms
FROM
(
    -- SYSCALL: total_time / io_count for WRITE rows (io_type = 5)
    SELECT ts, 'syscall' AS layer,
        toInt64(total_time) AS cur_time,
        toInt64(io_count)   AS cur_ios,
        lagInFrame(toNullable(toInt64(total_time))) OVER w AS prev_time,
        lagInFrame(toNullable(toInt64(io_count)))   OVER w AS prev_ios
    FROM profile_fw.linux_syscall_1_stats_io_patterns
    WHERE $__timeFilter(ts) AND io_type = 5
    WINDOW w AS (PARTITION BY hostname ORDER BY ts)

    UNION ALL

    -- BLOCK: write_time_ms / write_ios
    SELECT ts, 'block' AS layer,
        toInt64(write_time_ms) AS cur_time,
        toInt64(write_ios)     AS cur_ios,
        lagInFrame(toNullable(toInt64(write_time_ms))) OVER w AS prev_time,
        lagInFrame(toNullable(toInt64(write_ios)))     OVER w AS prev_ios
    FROM profile_fw.linux_block_1_stats
    WHERE $__timeFilter(ts)
    WINDOW w AS (PARTITION BY hostname ORDER BY ts)

    UNION ALL

    -- NVMe: write_time_ms / write_ios
    SELECT ts, 'nvme' AS layer,
        toInt64(write_time_ms) AS cur_time,
        toInt64(write_ios)     AS cur_ios,
        lagInFrame(toNullable(toInt64(write_time_ms))) OVER w AS prev_time,
        lagInFrame(toNullable(toInt64(write_ios)))     OVER w AS prev_ios
    FROM profile_fw.linux_nvme_1_stats
    WHERE $__timeFilter(ts)
    WINDOW w AS (PARTITION BY hostname ORDER BY ts)
)
WHERE prev_ios IS NOT NULL      -- drops each series' first (deltaless) row
ORDER BY time, indexOf(['syscall', 'block', 'nvme'], layer)
