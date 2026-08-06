# 0004 — `src/` layout, editable `config/config.yaml`, no packaging

**Status:** Accepted.

## Context

The repo is a run-in-place tool that the team clones and runs on an office Linux box.
It is not (yet) distributed as an installable package. We wanted a clearer,
production-shaped directory structure without adding install friction.

## Decision

- The package lives in **`src/`** (no inner package directory). Run it with
  **`python -m src <run_dir>`** from the repo root. The package's internal imports are
  all relative, so the move required no code changes inside it.
- **No `pyproject.toml`** — deliberately not made installable; deps stay in
  `requirements.txt`. (This is why the import name is `src` and the command is
  `python -m src`.)
- **Config lives at `config/config.yaml`** (repo root), *outside* the code, so an
  operator can edit it in place. `src/config.py` resolves it relative to the source
  tree (works from any CWD); `--config PATH` overrides it; `CH_HOST`/`CH_PORT` override
  host/port via env.

## Consequences

- Trade-off accepted: the import name is `src` (fine for a run-in-place tool; you
  cannot meaningfully `import` this as a library from another project). If that ever
  matters, revisit with a real package name under `src/` + a `pyproject.toml`.
- Docs and tests reference `src`; the run command everywhere is `python -m src`.
- Rationale and the alternatives considered are in [../design.md](../design.md) and the
  README.
