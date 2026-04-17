"""Tests for cmd_status --usable / --include-censored / --include-consent-revoked.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import json
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
                "INSERT INTO patients (patient_id) VALUES ('HGSOC002'), ('HGSOC006'), ('HGSOC099');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES ('HGSOC002-normal','HGSOC002','normal'),"
                "       ('HGSOC002-tumor','HGSOC002','tumor'),"
                "       ('HGSOC006-tumor','HGSOC006','tumor'),"
                "       ('HGSOC099-normal','HGSOC099','normal');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('HGSOC002-normal-ONT-RNA','HGSOC002-normal','ONT'),"
                "  ('HGSOC002-tumor-ONT-RNA','HGSOC002-tumor','ONT'),"
                "  ('HGSOC006-tumor-ONT-RNA','HGSOC006-tumor','ONT'),"
                "  ('HGSOC099-normal-ONT-RNA','HGSOC099-normal','ONT');"
            )
    finally:
        conn.close()


@pytest.fixture
def cohort(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    _seed(p)
    return p


def test_status_usable_counts_excluded(cohort: Path):
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
    res = _run([
        "status", "--project-dir", str(cohort), "--usable",
    ])
    # 4 total assays; 1 QC-failed; 1 consent-revoked (HGSOC099-normal).
    assert "Usable assays: 2 / 4" in res.stdout
    assert "QC-failed:   1" in res.stdout
    assert "Consent-rev: 1" in res.stdout


def test_status_default_excludes_censored(cohort: Path):
    """Default status (no --usable) excludes censored assays from counts."""
    _run([
        "censor", "--project-dir", str(cohort),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    res = _run([
        "status", "--project-dir", str(cohort), "--fmt", "json",
    ])
    data = json.loads(res.stdout)
    # Default filter excludes fail/censored; 3 usable assays remain.
    assay_rows = [r for r in data if r["level"] == "assay"]
    if assay_rows:
        for r in assay_rows:
            assert r["total"] == 3


def test_status_include_censored_restores_counts(cohort: Path):
    _run([
        "censor", "--project-dir", str(cohort),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    res = _run([
        "status", "--project-dir", str(cohort),
        "--include-censored", "--include-consent-revoked",
        "--fmt", "json",
    ])
    data = json.loads(res.stdout)
    assay_rows = [r for r in data if r["level"] == "assay"]
    if assay_rows:
        for r in assay_rows:
            assert r["total"] == 4
