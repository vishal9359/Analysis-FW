-- Total read/write IOs per layer (syscall, block, nvme, ssd) over the window.
--
-- For a Grafana bar chart: returns exactly 4 rows (layer, read_ios, write_ios),
-- x-axis = layer, two series = read_ios / write_ios, ordered syscall -> block ->
-- nvme -> ssd.
--
-- Total over the window = last cumulative value - first cumulative value, i.e.
-- argMax(x, ts) - argMin(x, ts). (max/min would match for a monotonic counter,
-- but argMax/argMin explicitly take the value at the latest / earliest ts.)
--
-- Column mapping per layer:
--   syscall : io_count of READ (io_type = 1) / WRITE (io_type = 5) rows in the
--             io_patterns child table, via argMaxIf / argMinIf in one pass.
--   block   : read_ios / write_ios
--   nvme    : read_ios / write_ios
--   ssd     : host_read_commands / host_write_commands
--
-- Caveats (fine for a single-host, no-reset run):
--   * Multiple hosts: argMax over the whole table mixes hosts. Group per host and
--     sum:  SELECT layer, sum(r), sum(w) FROM (... GROUP BY hostname, layer ...).
--   * Counter reset mid-window: last-first undercounts. If that's a risk, sum the
--     per-second positive deltas (greatest(0, cur-prev)) instead.
--
-- Grafana form (uses $__timeFilter). For clickhouse-client:
--   USE profile_fw;  drop the `profile_fw.` prefixes,
--   replace `$__timeFilter(ts)` with a real range.

SELECT layer, read_ios, write_ios
FROM
(
    -- SYSCALL: io_count totals, split by io_type (READ=1, WRITE=5)
    SELECT 'syscall' AS layer,
        toInt64(argMaxIf(io_count, ts, io_type = 1)) - toInt64(argMinIf(io_count, ts, io_type = 1)) AS read_ios,
        toInt64(argMaxIf(io_count, ts, io_type = 5)) - toInt64(argMinIf(io_count, ts, io_type = 5)) AS write_ios
    FROM profile_fw.linux_syscall_1_stats_io_patterns
    WHERE $__timeFilter(ts)

    UNION ALL

    -- BLOCK
    SELECT 'block' AS layer,
        toInt64(argMax(read_ios,  ts)) - toInt64(argMin(read_ios,  ts)) AS read_ios,
        toInt64(argMax(write_ios, ts)) - toInt64(argMin(write_ios, ts)) AS write_ios
    FROM profile_fw.linux_block_1_stats
    WHERE $__timeFilter(ts)

    UNION ALL

    -- NVMe
    SELECT 'nvme' AS layer,
        toInt64(argMax(read_ios,  ts)) - toInt64(argMin(read_ios,  ts)) AS read_ios,
        toInt64(argMax(write_ios, ts)) - toInt64(argMin(write_ios, ts)) AS write_ios
    FROM profile_fw.linux_nvme_1_stats
    WHERE $__timeFilter(ts)

    UNION ALL

    -- SSD: host_read_commands / host_write_commands
    SELECT 'ssd' AS layer,
        toInt64(argMax(host_read_commands,  ts)) - toInt64(argMin(host_read_commands,  ts)) AS read_ios,
        toInt64(argMax(host_write_commands, ts)) - toInt64(argMin(host_write_commands, ts)) AS write_ios
    FROM profile_fw.linux_ssd_1_stats
    WHERE $__timeFilter(ts)
)
ORDER BY indexOf(['syscall', 'block', 'nvme', 'ssd'], layer)
