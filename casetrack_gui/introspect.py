"""Runtime schema introspection — every project picks its own columns.

The GUI cannot hardcode `dorado_basecaller_done` etc. — different cohorts
declare different analyses (`pileup_5mC_bedmethyl` for the drug-screen
project, `*_done` timestamps for project-17424). These helpers walk
`pragma_table_info` to discover:

- which level (patient / specimen / assay) carries the most analyses → the
  natural row-unit for the cohort heatmap;
- the analysis names per level (anything ending in ``_done``);
- the natural primary key per level (first INTEGER PRIMARY KEY or first
  declared column).

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

LEVELS: tuple[str, ...] = ("patients", "specimens", "assays")
DONE_SUFFIX = "_done"


@dataclass
class LevelInfo:
    table: str
    columns: list[str] = field(default_factory=list)
    analyses: list[str] = field(default_factory=list)  # `<analysis>` minus _done
    key: str | None = None
    parent_key: str | None = None
    qc_status_col: bool = False


@dataclass
class ProjectShape:
    levels: dict[str, LevelInfo]
    row_level: str  # which level has the most analyses (heatmap row unit)
    has_qc_events: bool

    def analyses_for(self, level: str) -> list[str]:
        return self.levels[level].analyses if level in self.levels else []


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.OperationalError:
        return []


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _pick_key(cols: Sequence[str], hints: Sequence[str]) -> str | None:
    """Pick the natural key column. Honour `hints` first, then fall back to the
    first column."""
    for h in hints:
        if h in cols:
            return h
    return cols[0] if cols else None


def introspect(conn: sqlite3.Connection) -> ProjectShape:
    """Walk the schema and return a :class:`ProjectShape`. Cheap — pragma_table_info
    is a constant-time call against sqlite metadata."""
    levels: dict[str, LevelInfo] = {}
    for table in LEVELS:
        if not _table_exists(conn, table):
            continue
        cols = _columns(conn, table)
        analyses = sorted(c[: -len(DONE_SUFFIX)] for c in cols if c.endswith(DONE_SUFFIX))
        info = LevelInfo(
            table=table,
            columns=cols,
            analyses=analyses,
            qc_status_col="qc_status" in cols,
        )
        if table == "patients":
            info.key = _pick_key(cols, ("patient_id",))
        elif table == "specimens":
            info.key = _pick_key(cols, ("specimen_id", "sample"))
            info.parent_key = "patient_id" if "patient_id" in cols else None
        elif table == "assays":
            info.key = _pick_key(cols, ("assay_id",))
            info.parent_key = "specimen_id" if "specimen_id" in cols else None
        levels[table] = info

    # Heatmap row level = whichever has the most analyses; ties break specimens > assays > patients.
    def score(t: str) -> tuple[int, int]:
        info = levels.get(t)
        if info is None:
            return (-1, -1)
        priority = {"specimens": 2, "assays": 1, "patients": 0}[t]
        return (len(info.analyses), priority)

    row_level = max(LEVELS, key=score) if any(t in levels for t in LEVELS) else "specimens"

    return ProjectShape(
        levels=levels,
        row_level=row_level,
        has_qc_events=_table_exists(conn, "qc_events"),
    )
