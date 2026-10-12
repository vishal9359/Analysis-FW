# 0011 — Run-level config sidecars become their own tables

**Status:** Accepted.

## Context

Profile FW gained latency bucketization. The operator chooses the thresholds at
profiling time:

```
./bin/profile_fw -tag baseline -i 1 -d 1500 -dev nvme1 -lat-buckets 1,10,100,1000
```

and the profiler writes them to `latency_bucket_options.yaml` in the workload folder:

```yaml
latency_bucket_thresholds_us:
  lat_option_us_1: 1
  lat_option_us_2: 10
  lat_option_us_3: 100
  lat_option_us_4: 1000
```

The measurements land in the layer's own table. `linux_block_2_misc.proto` has four
`LatencyBuckets` sub-messages (`read_device_latency`, `write_device_latency`,
`read_driver_latency`, `write_driver_latency`), each flattened by
[0010](0010-payload-field-shapes-to-tables.md) into 17 prefixed columns —
`read_device_latency_latency_lh_opt1`,
`read_device_latency_latency_bw_opt1_and_opt2`, … `_latency_overflow`.

Those columns are **counts with no units attached**. `latency_bw_opt2_and_opt3` means
"IOs between the 2nd and 3rd threshold" — which is 10–100 µs for this run and could be
anything for the next one. The thresholds are not in the `.pb` data and not in the
`.proto`; they exist only in the sidecar file. Without storing them, the bucket columns
are permanently unlabelable, and a chart built across two runs with different
`-lat-buckets` values would silently compare different ranges.

This is the first case of a **run-level property** — one value per run, not per record
— so it needed a general answer, not a one-off.

## Decision

A run-level config sidecar is read once per run and written to **its own table, keyed
by `run_id`**, separate from the measurement tables. `src/runconfig.py` owns reading
and validating these files; `runner.run_load` writes them once per run, in both the
sequential and the process-pool path (the parent does it — a child only knows its own
unit).

For latency buckets: table `latency_buckets`, one row per threshold.

| column | type | meaning |
|---|---|---|
| `run_id` | String | the run directory name |
| `option_index` | UInt16 | the *N* in `lat_option_us_N`, 1-based |
| `threshold_us` | UInt64 | the threshold, microseconds |
| `_loaded_at` | DateTime | ingest time |

`ENGINE = MergeTree`, `PARTITION BY run_id`, `ORDER BY (run_id, option_index)` — the
same shape as every other table.

**Tall, not wide.** One row per threshold, not columns `lat_option_us_1..n`. The
operator picks *n* per run, so a wide table would need its schema changed whenever a run
used more thresholds than the last one.

**The file is optional; a malformed file is fatal.** `-lat-buckets` need not be used, so
a missing file is normal and simply produces no table. But a file that exists and cannot
be trusted fails the load (`InputError`), because the alternative is storing bucket
counts that can never be interpreted, with nothing downstream able to detect it.
Validated: indices contiguous from 1 (the proto's buckets are positional, so a gap
shifts every later bucket onto the wrong range), thresholds strictly increasing (an
inverted or empty range can never receive an IO), values whole and non-negative.

**Looked for in the workload folder, and in `Config/`.** The profiler writes it to the
workload folder; `Config/` is accepted too, since that is where the run's other
metadata (`run_config.log`) lives.

## Consequences

- Bucket columns become labelable: join `latency_buckets` on `run_id` and read
  `option_index` *i* and *i+1* to get the range `latency_bw_opt<i>_and_opt<i+1>`
  covers. Recipe in [../block-metrics.md](../block-metrics.md).
- **n thresholds describe n+1 ranges.** `-lat-buckets 1,10,100,1000` gives 5: `<1`,
  `1–10`, `10–100`, `100–1000`, `>1000`. The table stores the 4 boundaries; the first
  range is `latency_lh_opt1` and the last is `latency_overflow`.
- **The proto caps at 16 thresholds** (`latency_bw_opt15_and_opt16` is the last pair,
  then `latency_overflow`). The loader does **not** enforce that — it would mean
  hardcoding a producer's field names, which [0002](0002-runtime-schema-from-proto.md)
  forbids. Passing more than 16 thresholds to `-lat-buckets` is a producer-side contract
  violation: the extra IOs land in `latency_overflow` and the stored thresholds would
  claim otherwise.
- The loader deliberately does **not** generate bucket *labels* as columns. That would
  require knowing the `LatencyBuckets` field-naming convention in generic code. Labels
  belong in a query or a view.
- Ingest stays append-only ([0006](0006-mvp-append-only-ingest.md)), so re-loading a run
  duplicates these rows too. They are identical, so a joining query should use
  `SELECT DISTINCT` (or drop the run's partition before reloading). With 4–16 rows per
  run the duplication is harmless; it is the join that needs the care.
- Row counts in the report keep config and measurements separate: `totals.rows` counts
  loaded data, `totals.latency_buckets` counts threshold rows.
- The next run-level sidecar extends `src/runconfig.py` with another reader and table;
  it does not need a new place in the pipeline.
