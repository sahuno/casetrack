"""Dashboard must render QC chips + excluded list when QC events exist,
and stay unchanged (no crashes) on pre-migrate projects.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

import casetrack


CASETRACK_BIN = [sys.executable, str(Path(__file__).resolve().parent.parent / "casetrack.py")]


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        CASETRACK_BIN + args, check=check, capture_output=True, text=True
    )


def _seed(proj: Path) -> None:
    ns = argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    )
    casetrack.cmd_init(ns)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES "
                "  ('HGSOC002'),('HGSOC006'),('HGSOC099');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES "
                "  ('HGSOC002-normal','HGSOC002','normal'),"
                "  ('HGSOC099-normal','HGSOC099','normal');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('HGSOC002-normal-ONT-RNA','HGSOC002-normal','ONT'),"
                "  ('HGSOC099-normal-ONT-RNA','HGSOC099-normal','ONT');"
            )
    finally:
        conn.close()


@pytest.fixture
def cohort(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    _seed(p)
    return p


def test_dashboard_renders_qc_chips(cohort: Path, tmp_path: Path):
    _run([
        "censor", "--project-dir", str(cohort),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    _run([
        "censor", "--project-dir", str(cohort),
        "--level", "patient", "--id", "HGSOC099",
        "--kind", "consent_revoked", "--reason", "withdrew",
        "--withdrawal-date", "2026-03-15",
    ])
    out = tmp_path / "d.html"
    _run(["dashboard", "--project-dir", str(cohort), "--output", str(out)])
    html_text = out.read_text()
    assert "consent-revoked" in html_text
    assert "QC-failed" in html_text
    # Excluded section lists the specific events.
    assert "HGSOC002-normal-ONT-RNA" in html_text
    assert "library_prep_failed" in html_text or "qc_fail" in html_text


def test_dashboard_without_qc_events_has_no_chips(cohort: Path, tmp_path: Path):
    """Empty QC state ⇒ no chips section (§12 Q12 backward compat)."""
    out = tmp_path / "d.html"
    _run(["dashboard", "--project-dir", str(cohort), "--output", str(out)])
    html_text = out.read_text()
    # No rendered chip div, and no "consent-revoked" / "QC-failed" labels.
    assert 'class="qc-chips"' not in html_text
    assert "consent-revoked</span>" not in html_text
    assert "QC-failed</span>" not in html_text
