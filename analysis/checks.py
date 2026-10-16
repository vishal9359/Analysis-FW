"""Check C1, before anything runs (docs/phase1-design.md, section 9).

Two halves, each returning a list of problems (empty = passed):

  model_query_problems   the query our LLM wrote on the WrenAI data models
  planned_sql_problems   the ClickHouse SQL WrenAI made from it

The second half does not trust WrenAI. On 2026-10-15 wrenai 0.15.0, in strict
mode, accepted `SELECT read_io_count FROM run_db.linux_block_1_stats` and planned
SQL that reads the raw table - a database-qualified name that matches a data
model slips through. So every table the final SQL reads is checked here.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp


def _parse(sql: str) -> tuple[exp.Expression | None, list[str]]:
    try:
        statements = [s for s in sqlglot.parse(sql, read="clickhouse") if s is not None]
    except sqlglot.errors.ParseError as exc:
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
    allowed (database, view) pairs - the run's per-interval views. WrenAI's own
    WITH blocks are fine; any other table, or a table function, is refused."""
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
            problems.append(f"reads {t.sql('clickhouse')}, which is not one of the run's "
                            f"per-interval views")
    return problems
