"""Step 6: the Grafana writer (docs/phase1-design.md, section 8).

One folder per run, one dashboard per request, one panel that our code builds from a
template chosen by the shape of the result - our LLM only proposes the chart kind. The
panel's query is the exact SQL that check C1 passed (requirement C4); the explanation
is the panel's description; the dashboard's time range is the data's own, because runs
are loaded after they finish and Grafana's default "last 6 hours" would show nothing.
Asking the same request again replaces its dashboard.

Settings come from tools/config.json ("grafana": url, and token - or user and
password). The API calls and the query shape are the ones the office box's readiness
check passed (tools/check_phase1_env.py). The token is never printed.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime

from tools import llm_config

from .steps import is_number, is_time

PANEL_TYPES = {"timeseries": "timeseries", "bar": "barchart", "stat": "stat", "table": "table"}
# catalog units -> Grafana's unit ids
UNITS = {"i/o per second": "iops", "iops": "iops", "mib/s": "MiBs", "mb/s": "MBs",
         "ms": "ms", "milliseconds": "ms", "microseconds": "µs", "µs": "µs", "us": "µs",
         "ns": "ns", "s": "s", "seconds": "s", "percent": "percent", "%": "percent",
         "bytes": "bytes", "kib": "kbytes", "mib": "mbytes", "gib": "gbytes",
         "count": "short", "requests": "short", "i/o": "short"}


class GrafanaError(Exception):
    """Grafana refused or could not be reached; the message says why."""


def choose_panel(proposed: str, types: list[str], rows: int) -> tuple[str, str | None]:
    """The panel for a result (design section 8): a time column and numbers make a time
    series; one row of numbers a stat; categories and numbers a bar chart; anything else
    a table. Our LLM's proposal is kept when the shape allows it. Returns the kind, and
    a note when the proposal was not followed."""
    has_time = any(is_time(t) for t in types)
    numbers = sum(is_number(t) for t in types)
    texts = sum(not is_time(t) and not is_number(t) for t in types)
    if proposed == "table":
        return "table", None
    if has_time and numbers:
        kind = "timeseries"
    elif rows == 1 and numbers and not texts:
        kind = "stat"
    elif texts and numbers:
        kind = "bar"
    else:
        kind = "table"
    note = None if kind == proposed else (f"shown as {kind} rather than {proposed}, to suit "
                                          f"the shape of the result")
    return kind, note


def grafana_unit(unit: str) -> str | None:
    return UNITS.get(unit.strip().lower())


def dashboard_uid(run: str, request: str) -> str:
    return "afw-" + hashlib.sha1(f"{run}\n{request}".encode("utf-8")).hexdigest()[:20]


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def panel(kind: str, title: str, description: str, sql: str, datasource: dict,
          names: list[str], types: list[str], units: dict[str, str]) -> dict:
    """The panel, from a template. The query shape (format 1, "table") is the one the
    readiness check ran through Grafana; time-series panels draw it as series."""
    ids = {name: grafana_unit(unit) for name, unit in units.items() if grafana_unit(unit)}
    common = max(set(ids.values()), key=list(ids.values()).count) if ids else None
    overrides = [{"matcher": {"id": "byName", "options": name},
                  "properties": [{"id": "unit", "value": unit}]}
                 for name, unit in ids.items() if unit != common]
    options: dict = {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                     "tooltip": {"mode": "multi", "sort": "none"}}
    transformations = []
    if kind == "timeseries" and any(not is_number(t) and not is_time(t) for t in types):
        # rows of (time, label, value): one series per label, e.g. per latency bucket
        transformations = [{"id": "prepareTimeSeries", "options": {"format": "multi"}}]
    if kind == "bar":
        text = next((n for n, t in zip(names, types) if not is_number(t) and not is_time(t)), None)
        options.update({"xField": text, "orientation": "auto", "showValue": "auto"})
    elif kind == "stat":
        options = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                   "colorMode": "none", "graphMode": "none", "textMode": "value_and_name"}
    elif kind == "table":
        options = {"showHeader": True}
    return {"id": 1, "type": PANEL_TYPES[kind], "title": title, "description": description,
            "gridPos": {"h": 14, "w": 24, "x": 0, "y": 0},
            "datasource": datasource,
            "targets": [{"refId": "A", "datasource": datasource, "editorType": "sql",
                         "rawSql": sql, "format": 1, "queryType": "table"}],
            "fieldConfig": {"defaults": {"unit": common} if common else {},
                            "overrides": overrides},
            "transformations": transformations,
            "options": options}


class Grafana:
    def __init__(self, url: str, headers: dict, datasource: str | None = None,
                 verify: bool = True) -> None:
        self.url = url.rstrip("/")
        self._headers = {"Content-Type": "application/json", **headers}
        self._datasource_name = datasource
        self._verify = verify
        self._datasource = None

    @classmethod
    def from_config(cls, settings: dict) -> "Grafana":
        g = settings.get("grafana") or {}
        url = str(g.get("url") or "http://localhost:3000")
        if g.get("token"):
            headers = {"Authorization": f"Bearer {g['token']}"}
        elif g.get("user"):
            pair = f"{g['user']}:{g.get('password') or ''}".encode("utf-8")
            headers = {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
        else:
            raise GrafanaError("tools/config.json has no Grafana login: set grafana.token (a "
                               "service-account token, Editor role), or grafana.user and "
                               "grafana.password")
        return cls(url, headers, g.get("datasource"), verify=g.get("verifyTls", True) is not False)

    def _call(self, method: str, path: str, body: dict | None = None):
        status, text, _, error = llm_config.send(method, self.url + path, headers=self._headers,
                                                 json_body=body, timeout=30, verify=self._verify)
        if error:
            raise GrafanaError(f"cannot reach Grafana at {self.url}: {error}")
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        if status is None or status >= 300:
            message = data.get("message") if isinstance(data, dict) else " ".join(text.split())[:200]
            raise GrafanaError(f"Grafana refused {method} {path} (HTTP {status}): {message}")
        return data

    def datasource(self) -> dict:
        """The ClickHouse data source: the one named in grafana.datasource, else the first."""
        if self._datasource is None:
            sources = [d for d in self._call("GET", "/api/datasources") or []
                       if "clickhouse" in str(d.get("type", "")).lower()]
            if self._datasource_name:
                sources = [d for d in sources
                           if self._datasource_name in (d.get("name"), d.get("uid"))]
            if not sources:
                raise GrafanaError("Grafana has no ClickHouse data source"
                                   + (f" named {self._datasource_name!r}"
                                      if self._datasource_name else ""))
            self._datasource = {"type": sources[0]["type"], "uid": sources[0]["uid"]}
        return self._datasource

    def folder(self, title: str) -> str:
        """The uid of the folder with this title, made if it does not exist."""
        for f in self._call("GET", "/api/folders?limit=1000") or []:
            if f.get("title") == title:
                return f["uid"]
        return self._call("POST", "/api/folders", {"title": title})["uid"]

    def publish(self, *, run: str, request: str, title: str, description: str, sql: str,
                kind: str, names: list[str], types: list[str], units: dict[str, str],
                start: datetime, end: datetime) -> str:
        """Make or replace the request's dashboard in the run's folder; its link."""
        ds = self.datasource()
        dashboard = {"uid": dashboard_uid(run, request), "title": request[:200],
                     "tags": ["analysis-fw", "phase1"], "timezone": "utc", "editable": True,
                     "time": {"from": iso(start), "to": iso(end)},
                     "description": f"python -m analysis ask - run {run}",
                     "panels": [panel(kind, title, description, sql, ds, names, types, units)],
                     "schemaVersion": 39}
        r = self._call("POST", "/api/dashboards/db",
                       {"dashboard": dashboard, "folderUid": self.folder(run), "overwrite": True,
                        "message": "python -m analysis ask"})
        return self.url + str(r.get("url", ""))
