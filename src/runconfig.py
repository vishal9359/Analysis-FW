"""Run-level sidecar config the profiler writes next to the data.

A `.pb` file carries measurements; a few things are properties of the *run* and
are emitted once, as a file in the workload folder. This module reads those and
turns them into rows.

Today that is the latency-bucket thresholds. The profiler is invoked with

    ./bin/profile_fw ... -lat-buckets 1,10,100,1000

and writes the resulting thresholds to `latency_bucket_options.yaml`. The
measurements themselves land in the layer's own table as the `LatencyBuckets`
sub-message (flattened to `<field>_latency_lh_opt1`,
`<field>_latency_bw_opt1_and_opt2`, ...) — counts with no units attached. Those
counts are meaningless without the thresholds, and the thresholds are chosen per
run, so they must be stored per run or the buckets can never be labelled.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .errors import InputError
from .registry import Column

FILENAME = "latency_bucket_options.yaml"
SECTION = "latency_bucket_thresholds_us"
_KEY_RE = re.compile(r"^lat_option_us_(\d+)$")

TABLE = "latency_buckets"
COLUMNS = [
    Column("run_id", "String"),
    Column("option_index", "UInt16"),      # bucket position, 0..n (see bucket_rows)
    Column("range", "String"),             # "1-10 ms", ready for a chart legend
    Column("threshold_us", "UInt64"),      # the threshold opening the bucket, 0 if none
    Column("_loaded_at", "DateTime"),
]
ORDER_BY = ["run_id", "option_index"]

# A Grafana legend has little room, and a raw microsecond threshold spends it
# badly: "50000000" says much less at a glance than "50 s". So a threshold is
# rendered in the largest unit that keeps its number small.
_UNITS = [
    (1, "µs"),              # microseconds
    (1_000, "ms"),
    (1_000_000, "s"),
    (60_000_000, "min"),
    (3_600_000_000, "h"),
]


def _scale(us: int) -> tuple[int, str]:
    """The largest unit whose value is still at least 1, as (divisor, suffix)."""
    chosen = _UNITS[0]
    for unit in _UNITS:
        if us >= unit[0]:
            chosen = unit
    return chosen


def _number(us: int, divisor: int) -> str:
    """The scaled number with no trailing noise: 1000/1000 -> '1',
    1500/1000 -> '1.5'."""
    if us % divisor == 0:
        return str(us // divisor)
    return f"{us / divisor:.3f}".rstrip("0").rstrip(".")


def format_us(us: int) -> str:
    """Microseconds as a short, readable time.

    1 -> '1 µs', 1000 -> '1 ms', 10_000_000 -> '10 s', 50_000_000 -> '50 s'.
    """
    divisor, suffix = _scale(us)
    return f"{_number(us, divisor)} {suffix}"


def _range_label(lo: int | None, hi: int | None) -> str:
    """One bucket's range, for the `range` column.

    An open end reads '< x' or '> x'. When both bounds fall in the same unit it
    is written once ('1-10 ms'); when they differ, both carry it
    ('100 µs - 1 ms'). Keeping the shared case short is the point of the
    column, so it is not spelled out twice.
    """
    if lo is None:
        return f"< {format_us(hi)}"
    if hi is None:
        return f"> {format_us(lo)}"
    lo_div, lo_unit = _scale(lo)
    hi_div, hi_unit = _scale(hi)
    if lo_unit == hi_unit:
        return f"{_number(lo, lo_div)}-{_number(hi, hi_div)} {lo_unit}"
    return f"{format_us(lo)} - {format_us(hi)}"


def bucket_rows(thresholds: list[tuple[int, int]]) -> list[tuple[int, str, int]]:
    """`[(option_index, range, threshold_us)]` — one row per **bucket**, so n
    thresholds give n+1 rows.

    `option_index` is the bucket's position, which lines up with the proto's
    positional bucket fields: 0 is `latency_lh_opt1` (below the first
    threshold), i is `latency_bw_opt<i>_and_opt<i+1>`, and n is
    `latency_overflow` (above the last threshold).

    `threshold_us` carries the threshold that opens the bucket. The two
    open-ended buckets get 0: neither is named by a single threshold, and 0
    marks them as the unbounded ends rather than implying a boundary. Their
    bound is still readable in `range`.
    """
    if not thresholds:
        return []
    vals = [us for _, us in thresholds]
    rows = [(0, _range_label(None, vals[0]), 0)]
    rows += [(i, _range_label(lo, hi), lo)
             for i, (lo, hi) in enumerate(zip(vals, vals[1:]), start=1)]
    rows.append((len(vals), _range_label(vals[-1], None), 0))
    return rows


def find_file(run_dir: Path) -> Path | None:
    """The thresholds file for a run, or None when the profiler didn't emit one
    (`-lat-buckets` is optional, so its absence is normal, not an error).

    The profiler writes it into the workload folder itself; `Config/` is also
    accepted because that is where the run's other metadata (`run_config.log`)
    lives.
    """
    for p in (run_dir / FILENAME, run_dir / "Config" / FILENAME):
        if p.is_file():
            return p
    return None


def read_thresholds(run_dir: Path) -> list[tuple[int, int]]:
    """`[(option_index, threshold_us)]` in index order, or `[]` if there is no
    thresholds file.

    Raises InputError on a file that exists but cannot be trusted. A bad
    thresholds file is worth failing the load for: the bucket counts would be
    stored with no way to say what they counted, and nothing downstream could
    detect that.
    """
    path = find_file(run_dir)
    if path is None:
        return []

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InputError(f"{path}: cannot read latency buckets - {exc}") from exc

    if not isinstance(doc, dict):
        raise InputError(f"{path}: expected a YAML mapping at the top level")
    section = doc.get(SECTION)
    if section is None:
        raise InputError(f"{path}: missing '{SECTION}:' section")
    if not isinstance(section, dict):
        raise InputError(f"{path}: '{SECTION}' must be a mapping of "
                         f"lat_option_us_<n>: <microseconds>")

    opts: dict[int, int] = {}
    for key, val in section.items():
        m = _KEY_RE.match(str(key))
        if not m:
            raise InputError(
                f"{path}: unexpected key '{key}' in '{SECTION}' - "
                f"expected lat_option_us_<n>")
        if isinstance(val, bool) or not isinstance(val, int):
            raise InputError(
                f"{path}: {key} must be a whole number of microseconds, "
                f"got {val!r}")
        if val < 0:
            raise InputError(f"{path}: {key} is negative ({val})")
        opts[int(m.group(1))] = val

    if not opts:
        raise InputError(f"{path}: '{SECTION}' lists no thresholds")

    # Indices must run 1..n with no gaps. The proto's buckets are positional
    # (latency_bw_opt3_and_opt4 means "between the 3rd and 4th option"), so a
    # gap silently shifts every bucket after it onto the wrong range.
    expected = list(range(1, len(opts) + 1))
    if sorted(opts) != expected:
        raise InputError(
            f"{path}: option indices must run 1..{len(opts)} with no gaps, "
            f"got {sorted(opts)}")

    ordered = [(i, opts[i]) for i in expected]

    # Strictly increasing, or the "between opt_i and opt_i+1" buckets describe
    # an empty or inverted range and no IO can ever fall in them.
    for (i, lo), (j, hi) in zip(ordered, ordered[1:]):
        if hi <= lo:
            raise InputError(
                f"{path}: thresholds must strictly increase, but "
                f"lat_option_us_{i}={lo} and lat_option_us_{j}={hi}")

    return ordered
