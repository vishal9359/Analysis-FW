"""Discover the load units in a ProfileData-* directory.

A *unit* is one ``<layer>_<type>`` stem: its ``.proto`` schema plus its ordered
``.pb`` data parts. Naming convention (Analysis-FW-Module1-MVP-Design.md):

    <layer>_<type>.proto        schema (one message)
    <layer>_<type>.pb           data, single file
    <layer>_<type>.<NNN>.pb     data, split parts in order 000, 001, ...

``Config/`` is ignored — ``run_config.log`` is not a data layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import InputError

# <stem>.pb  or  <stem>.<NNN>.pb   (NNN = split index)
_SPLIT_RE = re.compile(r"^(?P<stem>.+?)(?:\.(?P<idx>\d+))?\.pb$")


@dataclass(frozen=True)
class Unit:
    stem: str            # e.g. "block_type1" — becomes the table name
    layer_dir: str       # e.g. "Linux/Block" — for diagnostics
    proto_path: Path
    pb_parts: list[Path]  # ordered: 000, 001, ...  (or a single file)


def _split_index(pb: Path) -> int:
    m = _SPLIT_RE.match(pb.name)
    idx = m.group("idx") if m else None
    return int(idx) if idx is not None else -1  # single-file (-1) sorts first


def discover(run_dir: Path) -> list[Unit]:
    if not run_dir.is_dir():
        raise InputError(f"input path is not a directory: {run_dir}")

    # group .pb files by stem, remembering their directory
    groups: dict[str, dict] = {}
    for pb in run_dir.rglob("*.pb"):
        if "Config" in pb.relative_to(run_dir).parts:
            continue
        m = _SPLIT_RE.match(pb.name)
        if not m:
            raise InputError(f"unexpected .pb name (not <stem>[.NNN].pb): {pb.name}")
        stem = m.group("stem")
        g = groups.setdefault(stem, {"parts": [], "dir": pb.parent})
        g["parts"].append(pb)

    if not groups:
        raise InputError(f"no .pb data files found under {run_dir}")

    units: list[Unit] = []
    for stem, g in sorted(groups.items()):
        parts = sorted(g["parts"], key=_split_index)
        proto = g["dir"] / f"{stem}.proto"
        if not proto.is_file():
            raise InputError(
                f"{stem}: found data files but no schema '{stem}.proto' beside them"
            )
        # split indices must be a dense 0..N-1 run (a gap means a missing part)
        idxs = [_split_index(p) for p in parts]
        if len(parts) > 1:
            expected = list(range(len(parts)))
            if sorted(idxs) != expected:
                raise InputError(
                    f"{stem}: split parts are not contiguous 000..{len(parts)-1:03d} "
                    f"(got {sorted(idxs)})"
                )
        layer_dir = str(g["dir"].relative_to(run_dir)).replace("\\", "/")
        units.append(Unit(stem=stem, layer_dir=layer_dir,
                          proto_path=proto, pb_parts=parts))
    return units
