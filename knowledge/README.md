# knowledge/ — what the analysis system knows

The analysis never guesses what a column means from its name: it reads it here.
Engineers write and correct these files; no code change is needed. Design:
[docs/phase1-design.md](../docs/phase1-design.md).

```
knowledge/
└── catalog/
    ├── _common.yaml               columns every table has (run_id, ts, hostname, ...)
    ├── linux_block_1_stats.yaml   one file per table (per .proto)
    ├── linux_block_2_misc.yaml
    └── latency_buckets.yaml       a table the loader writes from a run's sidecar file
```

## How to review a catalog file

1. **Answer the questions at the top** — replace each `answer: TODO`. They come first
   because they decide how the data checks and metrics are written.
2. **Check each entry**: is the meaning right, the unit right, the kind right? Fix it,
   and fill in every `TODO`.
3. **Set `status: confirmed`** on each entry you have checked.
4. **Run the check:** `python tools/catalog.py check`

Never rename a `column` or a name inside `columns: [...]`: they must match ClickHouse
exactly. They come from the loader's own schema code, not from a hand-written list.

## What each entry holds

| Field | Meaning |
|---|---|
| `meaning` | What the value is, in plain words |
| `unit` | count · bytes · sectors (512 bytes) · ms · ns · percent · name ... |
| `kind` | how the value may be used — see below |
| `status` | `todo` (something missing) · `draft` (pre-filled — please check) · `confirmed` (checked by an engineer) |
| `basis` | where a pre-filled description came from, so you know how far to trust it |
| `note` | anything else; usually "See Qn" |

A **group** describes many columns at once — for example the 14 exact-size buckets —
so a family of similar columns is reviewed once.

A group can also say **`labels: <table>`**: its columns are the buckets of one
histogram, in position order with the overflow bucket last, and the rows of that
run-level table give each bucket's range (the latency groups of `linux_block_2_misc`
say `labels: latency_buckets`). The analysis then shows the histogram with every
bucket's range, such as `100 µs - 1 ms`. The histogram is named after the group,
without "buckets": `Read device latency buckets` becomes `read device latency`.

### Keys at the top of a file

| Key | Meaning |
|---|---|
| `proto` | the `.proto` the table comes from (in `tests/sample_protos/`) |
| `source: src/runconfig.py` | instead of `proto`: the loader writes this table itself, from a file the profiler leaves in the run folder (`latency_buckets`, ADR-0011) |
| `schema_fingerprint` | what the check compares, to tell when the proto or the loader's columns change |
| `level: run` | the table holds rows for the whole run, with no `ts`, such as one row per latency bucket. It gets no per-interval view and is read as it is. Without `level`, a table has one row per sample |
| `counts_since` | `boot` or `profiler start`: decides a counter's first sample in a run |
| `observes` | what the table shows, in plain words — sent to our LLM with the columns |

### Kinds

| Kind | What it is | How it may be used |
|---|---|---|
| `counter` | a running total that only grows | use its change between samples (`<column>_delta`); **never add up its samples** |
| `gauge` | a reading at the moment of sampling | use as is (max, average); **never subtract samples** |
| `interval` | a value for one window only | use as is |
| `dimension` | an identifier to group by (queue, CPU core, enum) | group by it |
| `label` | text describing the row (device, hostname) | filter or show it |
| `timestamp` | a time | order and filter by it |
| `setting` | a value chosen when the profiler was started, fixed for the whole run (such as a latency bucket's threshold, `threshold_us` in `latency_buckets`) | show it, e.g. as a bucket's label; **never add up or subtract** |

### Metrics

`metrics:` defines values the analysis computes from the columns, once, so every
graph uses the same formula. In a `formula`:

- `<column>_delta` — the change of a counter since the previous sample;
- `interval_s` — seconds since the previous sample;
- anything else is a ClickHouse function, e.g. `nullIf(x, 0)` gives "no value" instead of
  dividing by zero.

## When a proto changes

`python tools/catalog.py check` fails and says what changed: new columns, removed
columns, or that the proto's fingerprint differs. Update the file, review the
descriptions, then copy the new `schema_fingerprint` into it. It also checks every
`labels:` group: the table it names must be described with `level: run`, and the
group's columns must be counters.

## A new table

`python tools/catalog.py skeleton <proto name without .proto>` writes a file with every
column marked `todo`. It never overwrites an existing file.
