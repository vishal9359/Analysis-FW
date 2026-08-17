"""Store construction — the one place that names a concrete adapter.

Adding a database is: write an adapter implementing `store.base.Store`, then add
one line here. Nothing above the store seam changes.
"""
from __future__ import annotations

from ..errors import ConfigError
from .base import Store


def make_store(kind: str, host: str, port: int, database: str,
               options: dict | None = None) -> Store:
    """Build the configured store. `options` carries adapter-specific settings
    (e.g. ClickHouse's `async_insert`); unknown keys are the adapter's business."""
    opts = options or {}
    if kind == "clickhouse":
        from .clickhouse import ClickHouseStore
        return ClickHouseStore(host, port, database,
                               bool(opts.get("async_insert", False)))
    if kind == "memory":
        from .memory import MemoryStore
        return MemoryStore()
    raise ConfigError(
        f"config: unknown store.kind '{kind}' (known: clickhouse, memory)")
