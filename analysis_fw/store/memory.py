"""In-memory store — proves the store seam and lets the whole pipeline run in
tests without a database. Not for production use.
"""
from __future__ import annotations

from ..registry import TableSchema
from typing import Sequence


class MemoryStore:
    def __init__(self) -> None:
        self.tables: dict[str, list[tuple]] = {}
        self.schemas: dict[str, TableSchema] = {}
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def ensure_table(self, schema: TableSchema) -> None:
        self.schemas[schema.stem] = schema
        self.tables.setdefault(schema.stem, [])

    def insert(self, table: str, columns: Sequence[str],
               rows: Sequence[Sequence]) -> None:
        self.tables.setdefault(table, []).extend(tuple(r) for r in rows)

    def count(self, table: str) -> int:
        return len(self.tables.get(table, []))

    def close(self) -> None:
        self.connected = False
