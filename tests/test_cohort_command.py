"""Tests for the base `casetrack cohort` command (proposal §8.2).

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
                "INSERT INTO patients (patient_id) VALUES "
                "  ('HGSOC002'),('HGSOC006');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES "
                "  ('HGSOC002-tumor','HGSOC002','tumor'),"
                "  ('HGSOC002-normal','HGSOC002','normal'),"
                "  ('HGSOC006-tumor','HGSOC006','tumor'),"
                "  ('HGSOC006-normal','HGSOC006','normal');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('HGSOC002-tumor-ONT-RNA','HGSOC002-tumor','ONT'),"
                "  ('HGSOC002-normal-ONT-RNA','HGSOC002-normal','ONT'),"
                "  ('HGSOC006-tumor-ONT-RNA','HGSOC006-tumor','ONT'),"
                "  ('HGSOC006-normal-ONT-RNA','HGSOC006-normal','ONT');"
            )
    finally:
        conn.close()


@pytest.fixture
def cohort(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    _seed(p)
    return p


def test_cohort_summary_json(cohort: Path):
    _run([
        "censor", "--project-dir", str(cohort),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "library prep failed",
    ])
    res = _run(["cohort", "--project-dir", str(cohort), "--fmt", "json"])
    data = json.loads(res.stdout)
    assert data["assays"]["total"] == 4
    assert data["assays"]["usable"] == 3
    assert data["assays"]["excluded"] == 1
    assert data["patients"]["total"] == 2


def test_cohort_summary_table_default(cohort: Path):
    res = _run(["cohort", "--project-dir", str(cohort)])
    assert "Patients:" in res.stdout
    assert "Assays:" in res.stdout
    assert "4 total" in res.stdout


def test_cohort_summary_md_format(cohort: Path):
    res = _run(["cohort", "--project-dir", str(cohort), "--fmt", "md"])
    assert "# Cohort" in res.stdout
    assert "**Patients**" in res.stdout


def test_cohort_counts_consent_buckets(cohort: Path):
    _run([
        "censor", "--project-dir", str(cohort),
        "--level", "patient", "--id", "HGSOC006",
        "--kind", "consent_revoked", "--reason", "withdrew",
        "--withdrawal-date", "2026-03-15",
    ])
    res = _run(["cohort", "--project-dir", str(cohort), "--fmt", "json"])
    data = json.loads(res.stdout)
    # One patient still consented, one revoked.
    consent_buckets = data["patients"]["by_consent"]
    assert consent_buckets.get("revoked") == 1
    assert consent_buckets.get("consented") == 1
