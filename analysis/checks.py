"""The checks (docs/phase1-design.md, section 9). Each returns a list of problems;
an empty list means passed.

C1, before anything runs:
  model_query_problems   the query our LLM wrote on the WrenAI data models
  time_problems          ... and its time filter, when the request is not for the whole run
  planned_sql_problems   the ClickHouse SQL WrenAI made from it
C2, after running:       result_problems
C3, on the explanation:  explanation_problems

planned_sql_problems does not trust WrenAI. On 2026-10-15 wrenai 0.15.0, in strict
mode, accepted `SELECT read_io_count FROM run_db.linux_block_1_stats` and planned
SQL that reads the raw table - a database-qualified name that matches a data
model slips through. So every table the final SQL reads is checked here.
"""
from __future__ import annotations

import math
import re

import sqlglot
from sqlglot import exp


def _parse(sql: str) -> tuple[exp.Expression | None, list[str]]:
    try:
        statements = [s for s in sqlglot.parse(sql, read="clickhouse") if s is not None]
    except sqlglot.errors.SqlglotError as exc:    # a parse error, or one in tokenizing
        return None, [f"the SQL does not parse: {' '.join(str(exc).split())}"]
    if len(statements) != 1:
        return None, [f"expected one statement, got {len(statements)}"]
    ast = statements[0]
    if not isinstance(ast, (exp.Select, exp.SetOperation)):
        return None, [f"only a read-only SELECT is allowed, got {ast.key.upper()}"]
    return ast, []


def _table_function(t: exp.Table) -> bool:
    """FROM url(...), file(...), remote(...) ...: reads data from elsewhere."""
    return not isinstance(t.this, exp.Identifier)


def model_query_problems(sql: str, models: set[str], run: str | None = None) -> list[str]:
    """The rules for our LLM's query: one SELECT on the data models, by their bare
    names; no WITH blocks; every column as table.column (or a name given in the
    SELECT); and, when given, a filter to the run."""
    ast, problems = _parse(sql)
    if ast is None:
        return problems
    if any(True for _ in ast.find_all(exp.With)):
        problems.append("WITH blocks are not allowed (WrenAI can miss a wrong column inside one)")
    for t in ast.find_all(exp.Table):
        if _table_function(t):
            problems.append(f"reads from a table function: {t.sql('clickhouse')}")
        elif t.db or t.catalog:
            problems.append(f"{t.sql('clickhouse')}: use the data model's name alone, "
                            f"without a database")
        elif t.name not in models:
            problems.append(f"{t.name} is not a data model (known: {', '.join(sorted(models))})")
    aliases = {s.alias for s in ast.find_all(exp.Alias)}
    for c in ast.find_all(exp.Column):
        if not c.table and c.name not in aliases:
            problems.append(f"column {c.name} must be written as table.{c.name}")
    if run is not None and not any(_is_run_filter(e, run) for e in ast.find_all(exp.EQ)):
        problems.append(f"the query must filter to the run: <model>.run_id = '{run}'")
    return problems


def _is_run_filter(eq: exp.EQ, run: str) -> bool:
    sides = (eq.left, eq.right)
    return (any(isinstance(s, exp.Column) and s.name == "run_id" for s in sides)
            and any(isinstance(s, exp.Literal) and s.is_string and s.this == run for s in sides))


def planned_sql_problems(sql: str, allowed: set[tuple[str, str]]) -> list[str]:
    """WrenAI's ClickHouse SQL: one read-only SELECT whose only tables are the
    allowed (database, table) pairs - what the run's data models read: its views,
    and its run-level tables. WrenAI's own WITH blocks are fine; any other table, or
    a table function, is refused."""
    ast, problems = _parse(sql)
    if ast is None:
        return problems
    ctes = {cte.alias for cte in ast.find_all(exp.CTE)}
    for t in ast.find_all(exp.Table):
        if _table_function(t):
            problems.append(f"reads from a table function: {t.sql('clickhouse')}")
        elif not t.db and t.name in ctes:
            continue                      # one of WrenAI's WITH blocks
        elif (t.db, t.name) not in allowed:
            problems.append(f"reads {t.sql('clickhouse')}, which is not what one of the "
                            f"run's data models reads")
    return problems


def time_problems(sql: str, start: str, end: str) -> list[str]:
    """C1, for a request that is not for the whole run: the query filters ts with the
    two times the request means (start and end, as 'YYYY-MM-DD HH:MM:SS' in UTC)."""
    ast, problems = _parse(sql)
    if ast is None:
        return problems
    compares = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)
    if not any(c.find_ancestor(*compares) for c in ast.find_all(exp.Column) if c.name == "ts"):
        problems.append("the query must filter ts to the time range asked for")
    for t in (start, end):
        if not re.search(rf"toDateTime\(\s*'{re.escape(t)}'\s*,\s*'UTC'\s*\)", sql, re.I):
            problems.append(f"the time filter must use exactly toDateTime('{t}', 'UTC')")
    return problems


# ---- C2: after running ---------------------------------------------------------

_SIGNED = ("difference", "minus")      # an output column that may rightly be negative


def result_problems(names: list[str], numeric: list[bool], rows: list[tuple]) -> tuple[list[str], list[str]]:
    """C2: values that cannot be true stop the answer (problems); gaps are said in it
    (notes). The measures of the block layer - rates, counts, sizes, times, shares - are
    never negative; a negative one means the query is wrong."""
    problems = []
    if not rows:
        return ["the query returned no rows"], []
    gaps: dict[int, list[str]] = {}          # rows without a value -> the columns with that many
    for i, name in enumerate(names):
        if not numeric[i]:
            continue
        vals = [r[i] for r in rows]
        present = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
        if not present:
            problems.append(f'"{name}" has no value in any row')
            continue
        low = min(present)
        if low < 0 and not any(w in name.lower() for w in _SIGNED):
            problems.append(f'"{name}" has negative values (lowest {low}), which a rate, count, '
                            f"size or time cannot have")
        if len(present) < len(vals):
            gaps.setdefault(len(vals) - len(present), []).append(f'"{name}"')
    notes = [f"{', '.join(cols)}: {n} of {len(rows)} rows have no value - for example the "
             f"first sample, or a second with no I/O" for n, cols in sorted(gaps.items())]
    return problems, notes


# ---- C3: on the explanation ------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\w])")
ALWAYS = (0.0, 1.0)                    # "every 1 second", "1 KiB": no need to cite these


def numbers_in(text: str) -> list[tuple[str, float]]:
    """(as written, value) for every number in a text; digits inside names such as
    linux_block_1_stats or nvme1n1 are not numbers."""
    out = []
    for m in _NUMBER.finditer(text):
        whole = m.group(1).replace(",", "")
        written = m.group(0)
        out.append((written, float(f"{whole}.{m.group(2)}" if m.group(2) else whole)))
    return out


def explanation_problems(text: str, results: list[float], context: str,
                         names: set[str], used: str) -> list[str]:
    """C3: every number in the explanation is one the graph's data or definition gives:
    a result number (within 1 %, for rounding), or a figure written in the context - the
    run's name, its times, the request, the query, the catalog's formulas, the result's
    labels. And every metric or column it names is one the query uses; a name that is
    also a plain word (device, range, bucket) is not checked, as the word means itself."""
    known = {v for _, v in numbers_in(context)} | set(ALWAYS)
    problems = []
    for written, value in numbers_in(text):
        if value in known:
            continue
        if any(math.isclose(value, r, rel_tol=0.01, abs_tol=1e-9) or
               math.isclose(value, abs(r), rel_tol=0.01, abs_tol=1e-9) for r in results):
            continue
        problems.append(f"{written} is not in the result or the query")
    for name in sorted(n for n in names if "_" in n):
        if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text) and name not in used:
            problems.append(f"{name} is mentioned, but the query does not use it")
    return problems
