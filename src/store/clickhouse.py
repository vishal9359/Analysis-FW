"""ClickHouse store adapter — the only place ClickHouse SQL, ClickHouse types,
and the ClickHouse driver live.

Everything ClickHouse-specific is here: the neutral-type -> ClickHouse-type map,
the CREATE TABLE DDL (MergeTree / PARTITION BY), the representable timestamp
range, and the client. `async_insert` is forced off by default so row counts are
truthful immediately after insert.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from ..errors import DatabaseError
from ..registry import Column, ColumnType

# neutral column type -> ClickHouse SQL type
_TO_CH = {
    ColumnType.STRING: "String",
    ColumnType.BOOL: "Bool",
    ColumnType.INT32: "Int32",
    ColumnType.INT64: "Int64",
    ColumnType.UINT32: "UInt32",
    ColumnType.UINT64: "UInt64",
    ColumnType.FLOAT32: "Float32",
    ColumnType.FLOAT64: "Float64",
    ColumnType.DATETIME: "DateTime",
}

# ClickHouse DateTime is an unsigned 32-bit epoch: 1970-01-01 .. 2106-02-07 UTC.
_UTC = timezone.utc
TIMESTAMP_RANGE = (datetime(1970, 1, 1, tzinfo=_UTC),
                   datetime(2106, 2, 7, tzinfo=_UTC))


def create_table_ddl(database: str, table: str, columns: Sequence[Column],
                     order_by: Sequence[str]) -> str:
    """The ClickHouse CREATE TABLE for a neutral column list."""
    cols = ",\n    ".join(f"`{c.name}` {_TO_CH[c.type]}" for c in columns)
    order = ", ".join(order_by)
    return (
        f"CREATE TABLE IF NOT EXISTS `{database}`.`{table}`\n"
        f"(\n    {cols}\n)\n"
        f"ENGINE = MergeTree\n"
        f"PARTITION BY run_id\n"
        f"ORDER BY ({order})"
    )


class ClickHouseStore:
    def __init__(self, host: str, port: int, database: str,
                 async_insert: bool = False) -> None:
        self.host = host
        self.port = port
        self.database = database
        self.async_insert = async_insert
        self._client = None

    @property
    def timestamp_range(self) -> tuple[datetime, datetime]:
        return TIMESTAMP_RANGE

    def connect(self) -> None:
        try:
            import clickhouse_connect
        except ImportError as exc:  # pragma: no cover
            raise DatabaseError("clickhouse-connect not installed") from exc
        try:
            # connect without a database first, so we can CREATE it
            client = clickhouse_connect.get_client(
                host=self.host, port=self.port,
                settings={"async_insert": 1 if self.async_insert else 0},
            )
            client.command(f"CREATE DATABASE IF NOT EXISTS `{self.database}`")
            client.close()
            self._client = clickhouse_connect.get_client(
                host=self.host, port=self.port, database=self.database,
                settings={"async_insert": 1 if self.async_insert else 0},
            )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"cannot connect to ClickHouse at {self.host}:{self.port} - {exc}"
            ) from exc

    def ensure_table(self, table: str, columns: Sequence[Column],
                     order_by: Sequence[str]) -> None:
        try:
            self._client.command(
                create_table_ddl(self.database, table, columns, order_by))
        except Exception as exc:
            raise DatabaseError(f"failed creating table {table}: {exc}") from exc

    def insert(self, table: str, columns: Sequence[str],
               rows: Sequence[Sequence]) -> None:
        try:
            self._client.insert(table, rows, column_names=list(columns))
        except Exception as exc:
            raise DatabaseError(f"insert into {table} failed: {exc}") from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
