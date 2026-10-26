"""ClickHouse access for the analysis: read-only, except for creating a run's views.

Host and port come from the loader's config/config.yaml (CH_HOST / CH_PORT
override), so the analysis always looks at the server the loader writes to.
"""
from __future__ import annotations

from pathlib import Path

from src.config import load_config

SYSTEM_DATABASES = ("system", "INFORMATION_SCHEMA", "information_schema")


def quote(name: str) -> str:
    """A ClickHouse identifier, back-quoted."""
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


class ClickHouse:
    """Implements runs.Database, and runs the SQL the analysis needs."""

    def __init__(self, host: str, port: int) -> None:
        import clickhouse_connect
        self._connect = lambda **kw: clickhouse_connect.get_client(host=host, port=port, **kw)
        # every read runs with readonly=2: no writes, no DDL, settings may still be set.
        # session_timezone=UTC: times come back as UTC whatever the server's own zone is
        self._read = self._connect(settings={"readonly": 2, "session_timezone": "UTC"})
        self._write = None

    @classmethod
    def from_config(cls, path: Path | None = None) -> "ClickHouse":
        store = load_config(path).store
        return cls(store.host, store.port)

    def rows(self, sql: str, parameters: dict | None = None) -> list[tuple]:
        return self._read.query(sql, parameters=parameters or {}).result_rows

    def query(self, sql: str, parameters: dict | None = None
              ) -> tuple[list[str], list[str], list[tuple]]:
        """(column names, ClickHouse types, rows) - read-only, like rows()."""
        r = self._read.query(sql, parameters=parameters or {})
        return list(r.column_names), [t.name for t in r.column_types], r.result_rows

    def time_span(self, database: str, table: str, run: str):
        """(first ts, last ts) of the run in this table, in UTC, or None if it has no rows."""
        rows = self.rows(
            f"SELECT min(ts), max(ts), count() FROM {quote(database)}.{quote(table)} "
            "WHERE run_id = {run:String}", {"run": run})
        return (rows[0][0], rows[0][1]) if rows and rows[0][2] else None

    def execute(self, sql: str) -> None:
        """For CREATE VIEW only - the one write the analysis makes."""
        if self._write is None:
            self._write = self._connect()
        self._write.command(sql)

    def close(self) -> None:
        for client in (self._read, self._write):
            if client is not None:
                client.close()

    # ---- runs.Database ------------------------------------------------------

    def find_tables(self, names: list[str]) -> list[tuple[str, str]]:
        return [tuple(r) for r in self.rows(
            "SELECT database, name FROM system.tables "
            "WHERE has({names:Array(String)}, name) AND engine != 'View' "
            "AND NOT has({skip:Array(String)}, database)",
            {"names": names, "skip": list(SYSTEM_DATABASES)})]

    def has_run(self, database: str, table: str, run: str) -> bool:
        return bool(self.rows(
            f"SELECT 1 FROM {quote(database)}.{quote(table)} WHERE run_id = {{run:String}} LIMIT 1",
            {"run": run}))

    def run_ids(self, database: str, table: str) -> list[str]:
        return [r[0] for r in self.rows(
            f"SELECT DISTINCT run_id FROM {quote(database)}.{quote(table)} ORDER BY run_id LIMIT 11")]

    def columns(self, database: str) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for table, name, ch_type in self.rows(
                "SELECT c.table, c.name, c.type FROM system.columns AS c "
                "INNER JOIN system.tables AS t ON t.database = c.database AND t.name = c.table "
                "WHERE c.database = {db:String} AND t.engine != 'View' ORDER BY c.table, c.position",
                {"db": database}):
            out.setdefault(table, {})[name] = ch_type
        return out
