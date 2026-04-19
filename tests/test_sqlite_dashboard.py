"""Tests for `casetrack dashboard --project-dir` (v0.3 nested HTML).

Verifies the HTML structure (patients → specimens → assays), per-analysis
completion widgets, and graceful handling of empty projects.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd
import pytest

import casetrack


def _init_ns(project_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc", project_name=None, force=False,
    )


def _reg_ns(project_dir: Path, *, level: str, id: str,
            parent: str | None = None, meta: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir), level=level, id=id, parent=parent,
        meta=meta, allow_new_parent=False, yes=False,
    )


def _append_ns(project_dir: Path, results: Path, analysis: str) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), results=str(results),
        key="sample_id", analysis=analysis, level=None, col_type=None,
        overwrite=False, allow_new=False, yes=False,
    )


def _dash_ns(project_dir: Path, output: Path) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir),
        output=str(output), key="sample_id",
    )


@pytest.fixture
def rich_project(tmp_path: Path) -> Path:
    """2 patients, 3 specimens, 4 assays, 2 analyses — matching the smoke test."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    for pid in ("P1", "P2"):
        casetrack.cmd_register(_reg_ns(proj, level="patient", id=pid,
                                       meta="age=55,sex=F"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S1", parent="P1",
                                    meta="tissue_site=tumor"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S2", parent="P1",
                                    meta="tissue_site=normal"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S3", parent="P2",
                                    meta="tissue_site=tumor"))
    for aid, sid in (("A1", "S1"), ("A2", "S1"), ("A3", "S2"), ("A4", "S3")):
        casetrack.cmd_register(_reg_ns(
            proj, level="assay", id=aid, parent=sid, meta="assay_type=WGS"
        ))

    mod = proj / "modkit.tsv"
    pd.DataFrame({"assay_id": ["A1", "A2"],
                  "mean_meth": [0.7, 0.6]}).to_csv(mod, sep="\t", index=False)
    casetrack.cmd_append(_append_ns(proj, mod, "modkit"))

    var = proj / "variant.tsv"
    pd.DataFrame({"assay_id": ["A1", "A4"],
                  "n_snvs": [12345, 54321]}).to_csv(var, sep="\t", index=False)
    casetrack.cmd_append(_append_ns(proj, var, "variant"))

    return proj


# ── Output file ───────────────────────────────────────────────────────────────


def test_dashboard_writes_html_file(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    assert out.exists()
    assert out.stat().st_size > 2000  # self-contained HTML isn't tiny


def test_dashboard_self_contained(rich_project: Path, tmp_path: Path):
    """No external CSS/JS references — everything inline so it scp's clean."""
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    # No CDN links or script tags.
    assert "<script" not in html
    assert "https://" not in html
    assert "<style>" in html  # CSS is inlined


# ── Structural checks ────────────────────────────────────────────────────────


def test_html_has_nested_patient_specimen_structure(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    # Two patient <details>, three specimen <details>.
    assert len(re.findall(r'<details class="patient"', html)) == 2
    assert len(re.findall(r'<details class="specimen"', html)) == 3


def test_html_contains_each_patient_id(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    assert "<strong>P1</strong>" in html
    assert "<strong>P2</strong>" in html


def test_html_contains_each_specimen_id(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    for sid in ("S1", "S2", "S3"):
        assert f"<strong>{sid}</strong>" in html


def test_html_contains_each_assay_id(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    for aid in ("A1", "A2", "A3", "A4"):
        assert f'<td class="id">{aid}</td>' in html


def test_assay_table_has_one_column_per_analysis(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    # modkit and variant both appear as analysis column headers.
    assert '<th class="vtext">modkit</th>' in html
    assert '<th class="vtext">variant</th>' in html


def test_done_cells_render_checkmark(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    # A1 has both analyses done → at least 2 ✓ cells overall.
    assert html.count('<td class="done"') >= 4  # 2 modkit + 2 variant
    assert "✓" in html


def test_missing_cells_render_blank_td(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    # A3 has neither analysis done, A2 missing variant, A4 missing modkit.
    assert html.count('<td class="missing">') >= 3


def test_patient_metadata_shown(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    assert "<b>age:</b> 55" in html
    assert "<b>sex:</b> F" in html


def test_specimen_metadata_shown(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    assert "<b>tissue_site:</b> tumor" in html
    assert "<b>tissue_site:</b> normal" in html


# ── Summary widgets ───────────────────────────────────────────────────────────


def test_top_metrics_show_counts(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    # Each metric value appears as <div class="value">N</div>.
    assert '<div class="value">2</div>' in html  # 2 patients
    assert '<div class="value">3</div>' in html  # 3 specimens
    assert '<div class="value">4</div>' in html  # 4 assays


def test_per_analysis_bars_present(rich_project: Path, tmp_path: Path):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    html = out.read_text()
    assert '<div class="analysis-row">' in html
    assert ">modkit</div>" in html
    assert ">variant</div>" in html


def test_stdout_summary(rich_project: Path, tmp_path: Path, capsys):
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(rich_project, out))
    msg = capsys.readouterr().out
    assert "2 patients" in msg
    assert "3 specimens" in msg
    assert "4 assays" in msg


# ── Empty project edge case ───────────────────────────────────────────────────


def test_empty_project_renders_without_crash(tmp_path: Path):
    proj = tmp_path / "empty"
    casetrack.cmd_init(_init_ns(proj))
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(proj, out))
    html = out.read_text()
    assert "No patients registered yet" in html


# ── HTML escaping ─────────────────────────────────────────────────────────────


def test_patient_id_is_html_escaped(tmp_path: Path):
    """Dashboard must HTML-escape legacy malformed IDs that predate v0.6 format
    enforcement. v0.6+ register rejects such IDs at source (proposal 0005
    Part A), but pre-v0.6 DBs may already contain them — read paths must
    still render safely.
    """
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    # Bypass the register validator to simulate a legacy malformed ID that
    # slipped into the DB before v0.6 enforcement.
    import sqlite3
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        conn.execute(
            "INSERT INTO patients (patient_id) VALUES (?)",
            ("P<script>alert(1)</script>",),
        )
        conn.commit()
    finally:
        conn.close()
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(proj, out))
    html = out.read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ── Flat mode still works ─────────────────────────────────────────────────────


def test_flat_dashboard_still_works(initialized_manifest: Path, tmp_path: Path):
    out = tmp_path / "flat.html"
    ns = argparse.Namespace(
        manifest=str(initialized_manifest), project_dir=None,
        output=str(out), key="sample_id",
    )
    casetrack.cmd_dashboard(ns)
    assert out.exists()
    assert "casetrack dashboard" in out.read_text()
