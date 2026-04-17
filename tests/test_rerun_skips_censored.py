"""Tests for cmd_rerun QC filtering.

Default: skip assays whose qc_status is fail/censored or whose patient is
consent-revoked. `--force-censored` includes them with a stderr warning.

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
                "INSERT INTO patients (patient_id) VALUES ('HGSOC002'), ('HGSOC006');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES ('HGSOC002-normal','HGSOC002','normal'),"
                "       ('HGSOC002-tumor','HGSOC002','tumor'),"
                "       ('HGSOC006-tumor','HGSOC006','tumor');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('HGSOC002-normal-ONT-RNA','HGSOC002-normal','ONT'),"
                "  ('HGSOC002-tumor-ONT-RNA','HGSOC002-tumor','ONT'),"
                "  ('HGSOC006-tumor-ONT-RNA','HGSOC006-tumor','ONT');"
            )
    finally:
        conn.close()


@pytest.fixture
def cohort(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    _seed(p)
    return p


def test_rerun_default_skips_censored_assay(cohort: Path):
    _run([
        "censor", "--project-dir", str(cohort),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    res = _run([
        "rerun", "--project-dir", str(cohort),
        "--analysis", "modkit", "--list-only",
    ])
    out_ids = set(res.stdout.split())
    assert "HGSOC002-normal-ONT-RNA" not in out_ids
    assert "HGSOC002-tumor-ONT-RNA" in out_ids
    assert "HGSOC006-tumor-ONT-RNA" in out_ids
    assert "Skipped 1" in res.stderr


def test_rerun_force_censored_includes_them(cohort: Path):
    _run([
        "censor", "--project-dir", str(cohort),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    res = _run([
        "rerun", "--project-dir", str(cohort),
        "--analysis", "modkit", "--list-only", "--force-censored",
    ])
    out_ids = set(res.stdout.split())
    assert "HGSOC002-normal-ONT-RNA" in out_ids
    assert "--force-censored" in res.stderr


def test_rerun_skips_consent_revoked_patient_assays(cohort: Path):
    _run([
        "censor", "--project-dir", str(cohort),
        "--level", "patient", "--id", "HGSOC002",
        "--kind", "consent_revoked", "--reason", "withdrew",
        "--withdrawal-date", "2026-03-15",
    ])
    res = _run([
        "rerun", "--project-dir", str(cohort),
        "--analysis", "modkit", "--list-only",
    ])
    out_ids = set(res.stdout.split())
    # Both HGSOC002 assays excluded (cascade from patient).
    assert "HGSOC002-tumor-ONT-RNA" not in out_ids
    assert "HGSOC002-normal-ONT-RNA" not in out_ids
    # HGSOC006 assay still included.
    assert "HGSOC006-tumor-ONT-RNA" in out_ids
