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
    Column("option_index", "UInt16"),      # the N in lat_option_us_N, 1-based
    Column("threshold_us", "UInt64"),      # the threshold, in microseconds
    Column("_loaded_at", "DateTime"),
]
ORDER_BY = ["run_id", "option_index"]


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
