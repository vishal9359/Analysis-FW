"""In-memory store — proves the store seam and lets the whole pipeline run in
tests without a database. Not for production use.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from ..registry import Column
from .base import WIDEST_TIMESTAMP_RANGE


class MemoryStore:
    def __init__(self) -> None:
        self.tables: dict[str, list[tuple]] = {}
        self.columns: dict[str, list[Column]] = {}   # table -> columns
        self.order_by: dict[str, list[str]] = {}
        self.connected = False

    @property
    def timestamp_range(self) -> tuple[datetime, datetime]:
        return WIDEST_TIMESTAMP_RANGE      # no storage limit

    def connect(self) -> None:
        self.connected = True

    def ensure_table(self, table: str, columns: Sequence[Column],
                     order_by: Sequence[str]) -> None:
        self.columns[table] = list(columns)
        self.order_by[table] = list(order_by)
        self.tables.setdefault(table, [])

    def insert(self, table: str, columns: Sequence[str],
               rows: Sequence[Sequence]) -> None:
        self.tables.setdefault(table, []).extend(tuple(r) for r in rows)

    def count(self, table: str) -> int:
        """Test helper — deliberately NOT part of the Store protocol. Row counts
        for reconciliation come from the loader (UnitResult.table_rows); a
        DB-side count belongs with the future reload coordinator (ADR-0006)."""
        return len(self.tables.get(table, []))

    def close(self) -> None:
        self.connected = False
