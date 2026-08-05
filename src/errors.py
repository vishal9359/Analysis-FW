"""Typed errors mapped to process exit codes.

The exit code tells Airflow which failure class occurred, and therefore whether
a retry could possibly help. See Analysis-FW-Module1-MVP-Design.md.
"""
from __future__ import annotations


class ExitCode:
    OK = 0
    UNEXPECTED = 1
    CONFIG = 2       # bad config — fix config, retry pointless
    INPUT = 3        # missing path, framing/decode, non-conforming — fix data
    SCHEMA = 4       # proto won't compile, contract violation — fix schema
    DATABASE = 5     # connect/insert/DDL failure — often transient, retry may help
    INTEGRITY = 6    # read != inserted — investigate


class AnalysisFWError(Exception):
    """Base error carrying an exit code."""
    exit_code = ExitCode.UNEXPECTED


class ConfigError(AnalysisFWError):
    exit_code = ExitCode.CONFIG


class InputError(AnalysisFWError):
    exit_code = ExitCode.INPUT


class SchemaError(AnalysisFWError):
    exit_code = ExitCode.SCHEMA


class DatabaseError(AnalysisFWError):
    exit_code = ExitCode.DATABASE


class IntegrityError(AnalysisFWError):
    exit_code = ExitCode.INTEGRITY
