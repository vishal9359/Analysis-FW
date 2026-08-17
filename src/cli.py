"""Command-line entry point.

Usage (run from the repo root):
    python -m src <input_dir>              # one ProfileData-* run
    python -m src <parent_dir>            # batch: many runs (auto-detect)
    python -m src <parent_dir> --batch    # batch: force
    python -m src <parent_dir> --batch --continue-on-error

A single run directory holds one ProfileData-<tag>-<timestamp> run. A parent
directory holds several ProfileData-* runs; batch mode loads each sequentially
(each run still parallelizes its own units). Default is fail-fast; add
--continue-on-error to load every run and report failures at the end.

Exit codes (see errors.ExitCode) tell the caller which failure class occurred.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .errors import AnalysisFWError, ExitCode, InputError
from .runner import find_runs, run_batch, run_load


def _setup_logging(level: str, fmt: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(logging.Formatter(
            '{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'))
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        handlers=[handler], force=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src",
        description="Load a ProfileData-* directory into the configured database.")
    parser.add_argument("input_dir", type=Path,
                        help="a ProfileData-* run directory, or a parent of several")
    parser.add_argument("--batch", action="store_true",
                        help="treat input_dir as a parent of ProfileData-* runs "
                             "(auto-detected if not set)")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="batch: keep loading remaining runs after one fails "
                             "(default: stop at the first failure)")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to a config.yaml (default: config/config.yaml)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except AnalysisFWError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return exc.exit_code

    _setup_logging(cfg.log_level, cfg.log_format)
    log = logging.getLogger("analysis_fw")

    try:
        if not args.input_dir.is_dir():
            raise InputError(f"input path is not a directory: {args.input_dir}")

        is_batch = args.batch or bool(find_runs(args.input_dir))
        if is_batch:
            return _run_batch(args, cfg, log)
        return _run_single(args, cfg, log)

    except AnalysisFWError as exc:
        log.error(str(exc))
        return exc.exit_code
    except Exception as exc:  # pragma: no cover
        log.exception(f"unexpected: {exc}")
        return ExitCode.UNEXPECTED


def _run_single(args, cfg, log) -> int:
    log.info(f"loading {args.input_dir} -> {cfg.store.database} "
             f"(workers={cfg.store.workers})")
    report = run_load(args.input_dir, cfg)          # reconciliation sets status
    print(json.dumps(report.to_dict(), indent=2))
    if report.status != "complete":
        for u in report.units:
            if not u.ok:
                log.error(f"{u.stem}: {u.records} records read but "
                          f"{u.table_rows.get(u.stem, 0)} rows in the main table")
        return ExitCode.INTEGRITY
    log.info(f"done: {report.rows} rows across "
             f"{sum(len(u.table_rows) for u in report.units)} tables")
    return ExitCode.OK


def _run_batch(args, cfg, log) -> int:
    runs = find_runs(args.input_dir)
    log.info(f"batch: {len(runs)} run(s) under {args.input_dir} -> "
             f"{cfg.store.database} (continue_on_error={args.continue_on_error})")
    batch = run_batch(args.input_dir, cfg, continue_on_error=args.continue_on_error)
    print(json.dumps(batch.to_dict(), indent=2))
    t = batch.to_dict()["totals"]
    if batch.exit_code != ExitCode.OK:
        log.error(f"batch finished with failures: {t}")
    else:
        log.info(f"batch complete: {t}")
    return batch.exit_code


if __name__ == "__main__":
    sys.exit(main())
