"""Tests for the v0.8 static snapshot exporter.

Strategy: build a fixture project identical to ``tests/test_gui_app.py::gui_project``
(same init + seed helpers), then call ``render_snapshot`` directly and assert on the
written file set, link integrity, and static asset placement.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import casetrack  # noqa: E402

# Skip entire module if jinja2 / fastapi not installed.
pytest.importorskip("jinja2")
pytest.importorskip("fastapi")

from casetrack_gui.snapshot import render_snapshot  # noqa: E402


# ── shared fixture helpers (mirrors test_gui_app.py) ────────────────────────


def _init_project(project_dir: Path, project_id: str = "snap-cohort") -> Path:
    casetrack.cmd_init(argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        samples=None,
        key="sample_id",
        metadata=None,
        cols=None,
        from_template="hgsoc",
        project_name="snap_cohort",
        project_id=project_id,
        force=False,
        bare=True,
    ))
    return project_dir


def _seed_rows(project_dir: Path) -> None:
    db = project_dir / "casetrack.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO patients(patient_id, sex, qc_status, consent_status) VALUES (?,?,?,?)",
            ("P01", "F", "pass", "consented"),
        )
        conn.execute(
            "INSERT INTO patients(patient_id, sex, qc_status, consent_status) VALUES (?,?,?,?)",
            ("P02", "M", "pass", "consented"),
        )
        conn.execute(
            "INSERT INTO specimens(specimen_id, patient_id, tissue_site, qc_status) VALUES (?,?,?,?)",
            ("P01_tumor", "P01", "ovary", "pass"),
        )
        conn.execute(
            "INSERT INTO specimens(specimen_id, patient_id, tissue_site, qc_status) VALUES (?,?,?,?)",
            ("P01_normal", "P01", "blood", "pass"),
        )
        conn.execute(
            "INSERT INTO specimens(specimen_id, patient_id, tissue_site, qc_status) VALUES (?,?,?,?)",
            ("P02_tumor", "P02", "ovary", "pass"),
        )
        conn.execute(
            "INSERT INTO assays(assay_id, specimen_id, assay_type, qc_status) VALUES (?,?,?,?)",
            ("A_T1", "P01_tumor", "ONT", "pass"),
        )
        conn.execute(
            "INSERT INTO assays(assay_id, specimen_id, assay_type, qc_status) VALUES (?,?,?,?)",
            ("A_N1", "P01_normal", "ONT", "pass"),
        )
        conn.commit()
    finally:
        conn.close()


def _add_done_columns(project_dir: Path, level: str, analyses: list[str]) -> None:
    db = project_dir / "casetrack.db"
    conn = sqlite3.connect(db)
    try:
        for a in analyses:
            try:
                conn.execute(f"ALTER TABLE {level} ADD COLUMN {a}_done TEXT")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def _set_done(project_dir: Path, level: str, key: str, where_id: str, analysis: str) -> None:
    db = project_dir / "casetrack.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            f"UPDATE {level} SET {analysis}_done = '2026-05-01T10:00:00' WHERE {key} = ?",
            (where_id,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def snapshot_project(tmp_path: Path):
    project_dir = _init_project(tmp_path / "proj", project_id="snap-cohort")
    _seed_rows(project_dir)
    _add_done_columns(project_dir, "specimens", ["merge", "samtools_sort", "modkit_callmods"])
    _add_done_columns(project_dir, "assays", ["dorado_basecaller"])
    _set_done(project_dir, "specimens", "specimen_id", "P01_tumor", "merge")
    _set_done(project_dir, "specimens", "specimen_id", "P01_tumor", "samtools_sort")
    _set_done(project_dir, "assays", "assay_id", "A_T1", "dorado_basecaller")
    return project_dir, "snap-cohort"


# ── tests ────────────────────────────────────────────────────────────────────


def test_snapshot_writes_expected_file_set(snapshot_project, tmp_path):
    """render_snapshot writes index, qc, per-patient pages, and static CSS."""
    project_dir, project_id = snapshot_project
    output_dir = tmp_path / "snap"

    written = render_snapshot(project_id, project_dir, output_dir)
    written_names = {p.relative_to(output_dir).as_posix() for p in written}

    assert "index.html" in written_names
    assert "qc.html" in written_names
    assert "patient_P01.html" in written_names
    assert "patient_P02.html" in written_names
    assert "static/casetrack.css" in written_names

    # All reported paths must actually exist on disk.
    for p in written:
        assert p.exists(), f"render_snapshot reported {p} but it does not exist"


def test_snapshot_internal_links_are_relative_and_resolvable(snapshot_project, tmp_path):
    """Every <a href="..."> in the snapshot output resolves to a written file."""
    project_dir, project_id = snapshot_project
    output_dir = tmp_path / "snap"
    render_snapshot(project_id, project_dir, output_dir)

    broken: list[tuple[str, str]] = []
    for html_file in sorted(output_dir.glob("*.html")):
        content = html_file.read_text(encoding="utf-8")
        for href in re.findall(r'href="([^"]*)"', content):
            # Skip anchors, external URLs, and the stripped project-picker "#".
            if not href or href.startswith(("#", "http://", "https://", "mailto:")):
                continue
            target = (output_dir / href).resolve()
            if not target.exists():
                broken.append((html_file.name, href))

    assert broken == [], (
        "Snapshot contains broken relative links:\n"
        + "\n".join(f"  {f}: href={h!r}" for f, h in broken)
    )


def test_snapshot_static_assets_land_in_output(snapshot_project, tmp_path):
    """static/casetrack.css is present and non-empty in the snapshot output."""
    project_dir, project_id = snapshot_project
    output_dir = tmp_path / "snap"
    render_snapshot(project_id, project_dir, output_dir)

    static_dir = output_dir / "static"
    assert static_dir.is_dir()
    css = static_dir / "casetrack.css"
    assert css.exists()
    assert css.stat().st_size > 0

    # The HTML pages must reference the relative path, not the absolute server path.
    index_html = (output_dir / "index.html").read_text(encoding="utf-8")
    assert 'href="static/casetrack.css"' in index_html
    assert 'href="/static/casetrack.css"' not in index_html


def test_snapshot_no_mutation_forms_in_output(snapshot_project, tmp_path):
    """POST forms for censor/uncensor must not appear in snapshot pages."""
    project_dir, project_id = snapshot_project
    output_dir = tmp_path / "snap"
    render_snapshot(project_id, project_dir, output_dir)

    for html_file in output_dir.glob("*.html"):
        content = html_file.read_text(encoding="utf-8")
        assert "/censor" not in content, (
            f"{html_file.name} still contains a /censor action URL"
        )
        assert "/uncensor" not in content, (
            f"{html_file.name} still contains an /uncensor action URL"
        )
