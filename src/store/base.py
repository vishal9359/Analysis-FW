"""The store interface — the only boundary the database lives behind.

No SQL, no database-specific type, and no database-specific limit appears
outside an implementation of this protocol. The schema layer produces neutral
`Column`s (see registry.ColumnType); each adapter maps those to its own SQL
types, writes its own DDL, and declares the timestamp range it can store.

Swapping databases is a new adapter and one line in `factory.make_store`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, Sequence, runtime_checkable

from ..registry import Column

# The widest range an adapter may claim (used by stores with no real limit).
WIDEST_TIMESTAMP_RANGE = (
    datetime(1, 1, 1, tzinfo=timezone.utc),
    datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
)


@runtime_checkable
class Store(Protocol):
    @property
    def timestamp_range(self) -> tuple[datetime, datetime]:
        """The (min, max) timestamp this store can represent. The loader clamps
        `ts` to it so a bad producer timestamp can never fail an insert."""

    def connect(self) -> None:
        """Open the connection and ensure the target database exists."""

    def ensure_table(self, table: str, columns: Sequence[Column],
                     order_by: Sequence[str]) -> None:
        """Create the table (if absent) with these columns and ordering.
        Called once per table — main and each child table."""

    def insert(self, table: str, columns: Sequence[str],
               rows: Sequence[Sequence]) -> None:
        """Insert a batch of rows (each a sequence in `columns` order)."""

    def close(self) -> None:
        ...
