-- Read IOPS across all layers (syscall, block, nvme, ssd), per-second delta.
--
-- Each layer's counter is cumulative, so per-second read IOPS = value(t) - value(t-1),
-- computed per host with lagInFrame. The lagged value is made Nullable so the first
-- row of each series is NULL and dropped by `WHERE prev IS NOT NULL` (that first row
-- has no predecessor, so its raw delta would be the whole counter -- a false spike).
--
-- Column mapping per layer:
--   syscall : io_count of READ rows (io_type = 1) in the io_patterns child table
--   block   : read_ios
--   nvme    : read_ios
--   ssd     : host_read_commands
--
-- Grafana form (uses $__timeFilter). For clickhouse-client:
--   USE profile_fw;  drop the `profile_fw.` prefixes,
--   replace `$__timeFilter(ts)` with a real range (keep `AND io_type = 1`),
--   `ts AS time` -> `ts`, and the ORDER BY `time` -> `ts`.

SELECT ts AS time, layer, greatest(0, cur - prev) AS read_iops
FROM
(
    -- BLOCK: read_ios counter
    SELECT ts, 'block' AS layer,
        toInt64(read_ios) AS cur,
        lagInFrame(toNullable(toInt64(read_ios))) OVER w AS prev
    FROM profile_fw.linux_block_1_stats
    WHERE $__timeFilter(ts)
    WINDOW w AS (PARTITION BY hostname ORDER BY ts)

    UNION ALL

    -- NVMe: read_ios counter
    SELECT ts, 'nvme' AS layer,
        toInt64(read_ios) AS cur,
        lagInFrame(toNullable(toInt64(read_ios))) OVER w AS prev
    FROM profile_fw.linux_nvme_1_stats
    WHERE $__timeFilter(ts)
    WINDOW w AS (PARTITION BY hostname ORDER BY ts)

    UNION ALL

    -- SSD: host_read_commands counter
    SELECT ts, 'ssd' AS layer,
        toInt64(host_read_commands) AS cur,
        lagInFrame(toNullable(toInt64(host_read_commands))) OVER w AS prev
    FROM profile_fw.linux_ssd_1_stats
    WHERE $__timeFilter(ts)
    WINDOW w AS (PARTITION BY hostname ORDER BY ts)

    UNION ALL

    -- SYSCALL: io_count of the READ rows (io_type = 1) in the child table
    SELECT ts, 'syscall' AS layer,
        toInt64(io_count) AS cur,
        lagInFrame(toNullable(toInt64(io_count))) OVER w AS prev
    FROM profile_fw.linux_syscall_1_stats_io_patterns
    WHERE $__timeFilter(ts) AND io_type = 1
    WINDOW w AS (PARTITION BY hostname ORDER BY ts)
)
WHERE prev IS NOT NULL      -- drops each series' first (deltaless) row
ORDER BY time, indexOf(['syscall', 'block', 'nvme', 'ssd'], layer)
