"""Load and validate the bundled config.

The config file ships inside the module (``config.yaml`` beside this file) and
is always loaded. Only host/port may be overridden by environment, so the same
bundled config runs against the dev box and the office server unchanged. No
credentials live in code or config (the target ClickHouse uses the default
user with no password).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import ConfigError

BUNDLED_CONFIG = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True)
class StoreConfig:
    host: str
    port: int
    database: str
    batch_size: int
    workers: int
    async_insert: bool


@dataclass(frozen=True)
class Config:
    producer_name: str
    store: StoreConfig
    log_level: str
    log_format: str


def _require(d: dict, key: str, section: str):
    if key not in d:
        raise ConfigError(f"config: missing '{section}.{key}'")
    return d[key]


def load_config(path: Path | None = None) -> Config:
    path = path or BUNDLED_CONFIG
    if not path.is_file():
        raise ConfigError(f"config: bundled config not found at {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config: cannot parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config: {path} must be a mapping")

    producer = _require(raw, "producer", "root")
    store = _require(raw, "store", "root")
    logging = raw.get("logging", {})

    try:
        host = os.environ.get("CH_HOST", str(_require(store, "host", "store")))
        port = int(os.environ.get("CH_PORT", _require(store, "port", "store")))
        workers = int(store.get("workers", 4))
        batch_size = int(store.get("batch_size", 100_000))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config: bad numeric value in 'store': {exc}") from exc

    if workers < 1:
        raise ConfigError("config: store.workers must be >= 1")
    if batch_size < 1:
        raise ConfigError("config: store.batch_size must be >= 1")

    return Config(
        producer_name=str(_require(producer, "name", "producer")),
        store=StoreConfig(
            host=host,
            port=port,
            database=str(_require(producer, "database", "producer")),
            batch_size=batch_size,
            workers=workers,
            async_insert=bool(store.get("async_insert", False)),
        ),
        log_level=str(logging.get("level", "INFO")).upper(),
        log_format=str(logging.get("format", "json")).lower(),
    )
