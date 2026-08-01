"""Command-line entry point.

Usage:
    python -m analysis_fw <input_dir>

<input_dir> is the ProfileData-<tag>-<timestamp> directory to load. The config
is bundled inside the module and always used (see config.yaml). Airflow invokes
this exactly the same way, templating <input_dir> per run.

Exit codes (see errors.ExitCode) tell Airflow which failure class occurred and
whether a retry could help.
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
from .runner import run_load


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
        prog="analysis_fw",
        description="Load a ProfileData-* directory into ClickHouse.")
    parser.add_argument("input_dir", type=Path,
                        help="the ProfileData-<tag>-<timestamp> directory to load")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except AnalysisFWError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return exc.exit_code

    _setup_logging(cfg.log_level, cfg.log_format)
    log = logging.getLogger("analysis_fw")

    try:
        if not args.input_dir.is_dir():
            raise InputError(f"input path is not a directory: {args.input_dir}")

        log.info(f"loading {args.input_dir} -> {cfg.store.database} "
                 f"(workers={cfg.store.workers})")
        report = run_load(args.input_dir, cfg)

        # reconcile (MVP scope): main-table rows == records read, per unit
        bad = [u for u in report.units if not u.ok]
        if bad:
            report.status = "failed"
            for u in bad:
                log.error(f"{u.stem}: {u.records} records read but "
                          f"{u.table_rows.get(u.stem, 0)} rows in the main table")

        print(json.dumps(report.to_dict(), indent=2))

        if report.status != "complete":
            return ExitCode.INTEGRITY
        log.info(f"done: {report.rows} rows across "
                 f"{sum(len(u.table_rows) for u in report.units)} tables")
        return ExitCode.OK

    except AnalysisFWError as exc:
        log.error(str(exc))
        return exc.exit_code
    except Exception as exc:  # pragma: no cover
        log.exception(f"unexpected: {exc}")
        return ExitCode.UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
