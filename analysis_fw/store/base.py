"""The store interface — the only boundary the database lives behind.

No SQL and no database-specific type appears outside an implementation of this
protocol. Swapping databases is a new adapter and nothing else.
"""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from ..registry import TableSchema


@runtime_checkable
class Store(Protocol):
    def connect(self) -> None:
        """Open the connection and ensure the target database exists."""

    def ensure_table(self, schema: TableSchema) -> None:
        """Create the table for this schema if it does not exist."""

    def insert(self, table: str, columns: Sequence[str],
               rows: Sequence[Sequence]) -> None:
        """Insert a batch of rows (each a sequence in `columns` order)."""

    def close(self) -> None:
        ...
