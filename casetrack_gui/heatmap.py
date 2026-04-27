"""Build the cohort heatmap data structure.

Specimen rows × analysis columns (or whichever level the project uses).
Per-cell glyph aggregation handles the case where a specimen owns several
assays for the same analysis (project-17424's `dorado` runs per assay,
collapses to a specimen at `merge`). For ≤4 child entities we render a
glyph string (e.g. ``●⊘`` = one done, one failed); beyond that we fall back
to a sparkline-friendly summary.

Glyphs (committed 2026-04-27):
  ●  done
  ◐  in-flight (slurm_job_id present, no _done)
  ◯  pending
  ⊘  failed (qc_status='fail')
  ⚠  warn (qc_status='warn')
  —  N/A

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from casetrack_gui.introspect import ProjectShape

GLYPH = {
    "done": "●",
    "in_flight": "◐",
    "pending": "◯",
    "failed": "⊘",
    "warn": "⚠",
    "na": "—",
}


@dataclass
class Cell:
    glyph: str
    title: str  # hover text
    css_class: str
    detail: list[str] = field(default_factory=list)  # per-child glyphs when aggregated


@dataclass
class Row:
    row_id: str
    parent_id: str | None
    qc_status: str | None
    cells: list[Cell]


@dataclass
class Heatmap:
    row_level: str
    parent_level: str | None
    analyses: list[str]
    rows: list[Row]
    aggregated_child_level: str | None = None  # 'assays' if dorado-style aggregation


def _state_from_done(done_value, qc_status: str | None) -> str:
    if qc_status == "fail":
        return "failed"
    if done_value is not None:
        return "done"
    return "pending"


def build(conn: sqlite3.Connection, shape: ProjectShape) -> Heatmap:
    """Pull rows from the heatmap-row level, then enrich with child-level
    aggregation when applicable."""
    row_level = shape.row_level
    info = shape.levels[row_level]
    analyses = info.analyses
    key = info.key
    parent = info.parent_key
    qc_col = "qc_status" if info.qc_status_col else None
    parent_level = {"specimens": "patients", "assays": "specimens"}.get(row_level)

    select_cols = [key]
    if parent:
        select_cols.append(parent)
    if qc_col:
        select_cols.append(qc_col)
    select_cols.extend(f"{a}{'_done'}" for a in analyses)

    rows_sql = f"SELECT {', '.join(select_cols)} FROM {info.table} ORDER BY {key}"
    raw = conn.execute(rows_sql).fetchall()

    # Cross-level aggregation: when we're at specimen level, also pull *_done
    # from assays so a specimen-row can show e.g. ●⊘ for a 2-assay basecaller
    # column. Only relevant when the assay level exists AND has analyses
    # NOT covered at the specimen level (otherwise the specimen-level value
    # is canonical).
    child_info = shape.levels.get("assays") if row_level == "specimens" else None
    child_analyses: list[str] = []
    child_lookup: dict[str, list[tuple[dict, str | None]]] = {}
    if child_info and child_info.parent_key == "specimen_id" and child_info.analyses:
        child_analyses = [a for a in child_info.analyses if a not in analyses]
        if child_analyses:
            cols = ["specimen_id", child_info.key]
            if child_info.qc_status_col:
                cols.append("qc_status")
            cols.extend(f"{a}_done" for a in child_analyses)
            child_sql = f"SELECT {', '.join(cols)} FROM assays"
            for r in conn.execute(child_sql).fetchall():
                spec, _aid, *rest = r
                qc = rest.pop(0) if child_info.qc_status_col else None
                child_lookup.setdefault(spec, []).append(
                    ({a: rest[i] for i, a in enumerate(child_analyses)}, qc)
                )

    merged_analyses = analyses + child_analyses

    out_rows: list[Row] = []
    for r in raw:
        ix = 0
        row_id = r[ix]; ix += 1
        parent_id = r[ix] if parent else None
        if parent: ix += 1
        qc = r[ix] if qc_col else None
        if qc_col: ix += 1
        done_vals = list(r[ix:])

        cells: list[Cell] = []
        for a, dv in zip(analyses, done_vals):
            state = _state_from_done(dv, qc)
            cells.append(Cell(glyph=GLYPH[state], title=f"{a}: {state}", css_class=state))

        for a in child_analyses:
            children = child_lookup.get(row_id, [])
            if not children:
                cells.append(Cell(glyph=GLYPH["na"], title=f"{a}: no child rows", css_class="na"))
                continue
            child_states = []
            for done_map, child_qc in children:
                child_states.append(_state_from_done(done_map.get(a), child_qc))
            if len(child_states) <= 4:
                glyph = "".join(GLYPH[s] for s in child_states)
            else:
                # Sparkline fallback: counts per state in stable order.
                from collections import Counter
                c = Counter(child_states)
                glyph = " ".join(f"{GLYPH[k]}{v}" for k, v in c.items())
            # CSS class = worst state present (failed > warn > pending > in_flight > done)
            order = ["failed", "warn", "pending", "in_flight", "done"]
            worst = next((s for s in order if s in child_states), "done")
            title = f"{a}: " + ", ".join(child_states)
            cells.append(Cell(glyph=glyph, title=title, css_class=worst, detail=child_states))

        out_rows.append(Row(row_id=row_id, parent_id=parent_id, qc_status=qc, cells=cells))

    return Heatmap(
        row_level=row_level,
        parent_level=parent_level,
        analyses=merged_analyses,
        rows=out_rows,
        aggregated_child_level="assays" if child_analyses else None,
    )


def next_up(conn: sqlite3.Connection, shape: ProjectShape, limit: int = 6) -> list[dict]:
    """Cheap "what's next" queue: per-analysis, list IDs whose immediate
    upstream is done but this analysis is not. Heuristic — the GUI surfaces
    it as a starting point, not a strict topological sort."""
    info = shape.levels.get(shape.row_level)
    if not info or not info.analyses:
        return []

    out: list[dict] = []
    analyses = info.analyses
    for i, a in enumerate(analyses):
        done_col = f"{a}_done"
        # Pending = this analysis null AND any earlier analysis non-null OR no earlier analysis.
        if i == 0:
            sql = f"SELECT {info.key} FROM {info.table} WHERE {done_col} IS NULL ORDER BY {info.key}"
        else:
            prev = f"{analyses[i - 1]}_done"
            sql = (
                f"SELECT {info.key} FROM {info.table} "
                f"WHERE {done_col} IS NULL AND {prev} IS NOT NULL ORDER BY {info.key}"
            )
        ids = [r[0] for r in conn.execute(sql).fetchall()]
        if ids:
            out.append({"analysis": a, "count": len(ids), "ids": ids[:limit], "more": max(0, len(ids) - limit)})
    return out
