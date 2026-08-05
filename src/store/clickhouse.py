"""ClickHouse store adapter — the only place SQL and the CH driver live.

Connects with host/port only (the target uses the default user, no password).
`async_insert` is forced off so row counts are truthful immediately after
insert.
"""
from __future__ import annotations

from typing import Sequence

from ..errors import DatabaseError
from ..registry import Column, create_table_ddl


class ClickHouseStore:
    def __init__(self, host: str, port: int, database: str,
                 async_insert: bool = False) -> None:
        self.host = host
        self.port = port
        self.database = database
        self.async_insert = async_insert
        self._client = None

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
                create_table_ddl(self.database, table, list(columns), list(order_by)))
        except Exception as exc:
            raise DatabaseError(f"failed creating table {table}: {exc}") from exc

    def insert(self, table: str, columns: Sequence[str],
               rows: Sequence[Sequence]) -> None:
        try:
            self._client.insert(table, rows, column_names=list(columns))
        except Exception as exc:
            raise DatabaseError(f"insert into {table} failed: {exc}") from exc

    def count(self, table: str, run_id: str, sut_id: str) -> int:
        try:
            return int(self._client.command(
                f"SELECT count() FROM `{table}` "
                f"WHERE run_id = {{r:String}} AND sut_id = {{s:String}}",
                parameters={"r": run_id, "s": sut_id}))
        except Exception as exc:
            raise DatabaseError(f"count on {table} failed: {exc}") from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
