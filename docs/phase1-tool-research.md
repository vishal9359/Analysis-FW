# Phase 1 — Open-source tool research

**Date:** 2026-10-09. **Purpose:** choose existing open-source or free tools for
Phase 1 — a plain-language request becomes a ClickHouse query and a Grafana graph
(see [goals-and-requirements.md](goals-and-requirements.md), Phase 1).

Facts below were checked on the web on this date; the sources are listed at the
end. Open-source projects change quickly — one of the best-known tools in this
area was archived this year — so re-check before committing.

> **Correction (2026-10-09).** An earlier version of this page said WrenAI's
> open-source part cannot produce charts. That was wrong: it can, through
> `wren genbi`, with an AI coding agent writing the dashboard app. What is paid is
> Wren's own UI and its built-in app-building harness. Sections 3 and 4 are updated.

---

## 1. What we need from a tool

1. **Hold our meanings** — tables, columns, relationships, calculation rules and
   confirmed examples (Goal B). This is the most important point.
2. **Work with ClickHouse.**
3. **Use our own deployed LLM API.**
4. **Produce real ClickHouse SQL**, because a Grafana panel needs a query it can
   run again by itself.
5. **Work from code or the command line**, without its own UI (no UI in Phase 1).
6. **A permissive licence** (Apache-2.0 or MIT).
7. **Actively maintained.**

## 2. Short answer

**No single open-source product does the whole job.** The complete "chat with
your data" products are either archived (Vanna), paid in their chat and UI layer
(WrenAI, Cube), tied to a vendor's cloud (Grafana Assistant), or large general
platforms (DB-GPT).

The parts that *are* open, maintained and permissively licensed are the building
blocks: a **semantic layer** that holds what the data means, and **connectors**
for ClickHouse and Grafana. So the recommendation is to **assemble those, plus a
thin program of our own** for the steps no tool covers: reading `request.yaml`,
the yes/no confirmation, the checks, and the explanation.

## 3. Comparison

| Tool | What it is | Licence | ClickHouse | Our own LLM | Status (Oct 2026) | Fit for us |
|---|---|---|---|---|---|---|
| **WrenAI** (open-source core) | A semantic layer built for AI agents. Holds models, columns, relationships, metrics, instructions and question-and-SQL examples as reviewable files. `wren dry-plan` turns the agent's query into final database SQL without running it. **No built-in chat or AI agent** — "Wren is a layer, not another chat UI to adopt." **Charts are possible** through `wren genbi`: an AI coding agent that we supply writes a small dashboard web app, which can be previewed locally (`wren genbi open`) or deployed to Vercel or Cloudflare. Wren's own UI and its built-in app-building harness are paid. | Apache-2.0 for core, SDK and skills. The licence file says AGPL modules may be added later. | Yes (`pip install wrenai[clickhouse]`) | Yes — it does not call an LLM itself; our program does | Active. Rebuilt in May 2026; version 0.15.0 (Sept 2026), marked **beta** | **Best fit for the knowledge layer.** Main risk: young |
| **WrenAI legacy v1** ("Wren GenBI Classic") | The earlier WrenAI product: chat UI, text-to-SQL **and charts**, run with Docker | **AGPL-3.0** | Yes | Yes | **Frozen** on the `legacy/v1` branch: "no new features or security fixes" | Not usable: AGPL is not approved, and it receives no security fixes |
| **Cube Core** | Mature semantic layer: measures, dimensions, joins. Serves queries over SQL, REST, GraphQL and MCP; can return the SQL it generates. | Apache-2.0 | Yes (official driver) | Yes — no LLM inside the open-source part | Mature, widely used. Its AI chat is in the paid product | **Fallback** if WrenAI disappoints. Heavier to run (a separate Node.js service), and less built around AI agents |
| **Vanna 2.0** | Text-to-SQL agent framework: memory of question-and-SQL pairs, Plotly charts, summaries | MIT | Yes | Yes | **Repository archived on 29 March 2026 — read-only** | Not for a new build: no fixes or updates |
| **DB-GPT** | Broad data-agent platform with its own UI: SQL, Python analysis, charts, reports, knowledge bases | MIT | Not confirmed | Yes (OpenAI-compatible and local models) | Active (about 20k stars) | Much more than we need, and built around its own UI |
| **Grafana Assistant** | Grafana Labs' AI assistant: builds queries and dashboards from plain language | Grafana Cloud service (free tier) | "SQL table discovery" is not available in self-managed setups | **No** — uses Grafana Cloud's LLM | Self-hosted use since April 2026, but needs **Grafana 13** and a **Grafana Cloud connection**; prompts and query context are processed in Grafana Cloud | **Conflicts** with using our own LLM, and sends request context outside |
| **mcp-grafana** | Grafana Labs' MCP server: create and update dashboards, run ClickHouse SQL through Grafana | Apache-2.0 | Yes (`query_sql`) | — (a tool our program calls) | Active (about 3.5k stars). Works with Grafana 9.0+; has a read-only mode | **Use** for creating graphs |
| **Grafana HTTP API** | Grafana's own API: a dashboard is posted as JSON and a link comes back | Part of Grafana | — | — | Stable | **Alternative** to mcp-grafana |
| **mcp-clickhouse** | ClickHouse's MCP server: list databases and tables, run read-only queries | Apache-2.0 | — | — | Active | Optional, for exploring the schema |
| **MetricFlow** | dbt's metric engine | Apache-2.0 | — | — | Active | Built around dbt projects, which we do not use |

## 4. Recommendation

**Knowledge layer — WrenAI's open-source core.**

- It is built for exactly our pattern: an AI agent writes SQL against *described*
  models instead of raw tables, and the engine turns that into the real
  database's SQL.
- It holds our kinds of knowledge — meanings, relationships, rules
  (`instructions.md`) and confirmed examples (`queries.yml`) — as reviewable
  files in Git.
- `wren dry-plan` gives the final ClickHouse SQL **without running it**. That one
  feature serves two needs: the SQL that goes into the Grafana panel, and our
  check before a query runs (C1).
- Python, Apache-2.0, supports ClickHouse.

**Its risk is age:** rebuilt in May 2026, still beta. Two protections:

1. **Requirement B6** — our descriptions stay in our own files and format, and
   the WrenAI model files are *generated* from them. If WrenAI changes or stops,
   we generate for another tool instead. Vanna's archiving this year is the
   reason this matters.
2. **A short trial first** (section 6), with **Cube Core as the fallback.**

**Graphs — two possible paths, decided by one test.**

- **Path 1 — WrenAI GenBI (open source, Apache-2.0).** An AI coding agent runs
  `wren genbi build`, which returns build instructions; the agent then **writes a
  small dashboard web app from scratch**, choosing the charts, and it is previewed
  locally with `wren genbi open`. Query results are exported to Parquet and
  bundled into the app, which suits finished runs. It needs an LLM API with
  **tool calling**, strong enough to write a web app for every request, used
  through a coding agent that can talk to an OpenAI-compatible API. **Never use
  `wren genbi deploy`**: it uploads the app — and the data bundled in it — to
  Vercel or Cloudflare.
- **Path 2 — Grafana.** Our program asks the LLM only for the SQL and the kind of
  chart, and builds the Grafana panel itself, through mcp-grafana or the Grafana
  HTTP API. It needs no tool calling, the LLM fills in small pieces that are easy
  to check, and Grafana is where Phase 1 graphs must end up anyway.

**The tool-calling test decides.** If the LLM API handles tool calling well, try
Path 1 first, as the team proposed; otherwise go straight to Path 2. Either way,
the main question of the trial — does WrenAI plus our LLM produce correct SQL
from our descriptions? — is the same, and that work carries over unchanged.

**Our own thin program** — `request.yaml` → plain-words interpretation → yes/no
→ query → checks → run → graph → explanation. No tool covers this flow, and it is
small.

**One database change, whichever tool we pick — per-interval views.** Add one
ClickHouse view per table that turns each running total into the difference
between consecutive samples, with the reset guard already in place. After that,
"total over a run" is a plain sum, and "per second" is the value itself. The
biggest trap — adding up running totals — then disappears for every tool and
every LLM, instead of relying on the AI to remember it every time.

## 5. Evidence that a semantic layer is the right base

dbt Labs' 2026 benchmark compared an LLM writing SQL directly against an LLM
working through a semantic layer, on the same modelled data:

| Model | Direct text-to-SQL | Through a semantic layer |
|---|---|---|
| Claude Sonnet 4.6 | 90.0% | 98.2% |
| GPT-5.3 Codex | 84.1% | 100.0% |

Their conclusion: the semantic layer "never returns silently wrong data" — it
either answers correctly or fails clearly. Direct text-to-SQL remains useful for
one-off exploration. Our agreed decision, "meanings may be predefined", is what
makes the semantic-layer approach possible.

## 6. Trial plan (1–2 weeks, on the office box)

1. Run `python tools/check_phase1_env.py` on the office box. It checks the LLM
   API (including whether it supports tool calling, which decides the graph path
   in section 4), ClickHouse, Grafana and the machine, and writes
   `phase1_check_report.json`.
2. Install `wrenai[clickhouse]` and point it at `profile_fw`.
3. Create per-interval views for `linux_block_1_stats`, `linux_block_2_misc` and
   `linux_nvme_1_stats`.
4. Describe those tables in our catalog format; generate the WrenAI model files
   from it.
5. Run the five example requests, plus five of the existing Grafana graphs
   described in plain language, through LLM → WrenAI → `dry-plan` → graph:
   a local GenBI preview (Path 1) or a Grafana panel in a folder named after the
   run (Path 2).
6. Compare each result with the existing hand-built panel.

**Decide at the end:** keep WrenAI, or switch to Cube Core.

**Needed before the trial:** the LLM API configuration, a Grafana service-account
token, and the existing dashboards exported as JSON — see the Phase 1 open points
in [goals-and-requirements.md](goals-and-requirements.md).

## Sources

- WrenAI — [repository](https://github.com/Canner/WrenAI) ·
  [README](https://raw.githubusercontent.com/Canner/WrenAI/main/README.md) ·
  [licence](https://github.com/Canner/WrenAI/blob/main/LICENSE) ·
  [PyPI releases](https://pypi.org/project/wrenai/) ·
  [`dry-plan` usage](https://skills.sh/canner/wrenai/wren-usage) ·
  [legacy v1 branch](https://github.com/Canner/WrenAI/tree/legacy/v1) ·
  [legacy v1 licence (AGPL-3.0)](https://github.com/Canner/WrenAI/blob/legacy/v1/LICENSE) ·
  [GenBI app guide (open source)](https://docs.getwren.ai/oss/guides/genbi) ·
  [GenBI skill](https://claudskills.com/skills/genbi/SKILL.md) ·
  [GenBI skill registry entry](https://tessl.io/registry/skills/github/Canner/WrenAI/genbi) ·
  [`wren-core-wasm` SDK](https://docs.getwren.ai/oss/sdk/wasm)
- Vanna — [repository (archived)](https://github.com/vanna-ai/vanna) ·
  [archive date](https://gittrend.io/repo/vanna-ai/vanna) ·
  [docs](https://vanna.ai/docs/)
- [DB-GPT repository](https://github.com/eosphoros-ai/DB-GPT)
- Cube — [open-source semantic layers compared](https://cube.dev/articles/open-source-semantic-layer) ·
  [ClickHouse driver](https://docs.cube.dev/admin/connect-to-data/data-sources/clickhouse)
- Grafana Assistant — [on-prem announcement](https://grafana.com/whats-new/2026-04-21-grafana-assistant-becomes-available-on-prem/) ·
  [self-managed requirements](https://grafana.com/docs/grafana-cloud/machine-learning/assistant/self-managed.md) ·
  [customisation](https://grafana.com/blog/grafana-assistant-everywhere)
- [mcp-grafana](https://github.com/grafana/mcp-grafana) ·
  [mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse)
- [dbt — Semantic layer vs text-to-SQL, 2026 benchmark](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026)
- [Bruin — Best text-to-SQL tools 2026](https://getbruin.com/blog/best-text-to-sql-tools-2026/)
