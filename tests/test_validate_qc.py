"""Tests for cmd_validate — v0.4 QC invariants.

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


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
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
                "INSERT INTO patients (patient_id) VALUES ('HGSOC002');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES ('HGSOC002-tumor','HGSOC002','tumor');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) "
                "VALUES ('HGSOC002-tumor-ONT-RNA','HGSOC002-tumor','ONT');"
            )
    finally:
        conn.close()
    return proj


def test_validate_clean_project(seeded: Path):
    res = _run(["validate", "--project-dir", str(seeded)])
    assert res.returncode == 0


def test_validate_detects_consent_invariant_drift(seeded: Path):
    conn = casetrack.open_project_db(seeded / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute(
                "UPDATE patients SET consent_status='revoked', "
                "withdrawal_date='2026-03-15' WHERE patient_id='HGSOC002'"
            )
    finally:
        conn.close()
    res = _run(["validate", "--project-dir", str(seeded)], check=False)
    assert res.returncode != 0
    assert "consent invariant" in res.stderr
    assert "no active qc_events" in res.stderr


def test_validate_detects_qc_status_mismatch(seeded: Path):
    """Hand-edit qc_status without a matching event → mismatch is reported."""
    conn = casetrack.open_project_db(seeded / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute(
                "UPDATE assays SET qc_status='fail' "
                "WHERE assay_id='HGSOC002-tumor-ONT-RNA'"
            )
    finally:
        conn.close()
    res = _run(["validate", "--project-dir", str(seeded)], check=False)
    assert res.returncode != 0
    assert "qc_status mismatch" in res.stderr


def test_validate_detects_orphan_active_event(seeded: Path):
    """An active event whose entity was removed is flagged."""
    conn = casetrack.open_project_db(seeded / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute(
                "INSERT INTO qc_events "
                "(level, entity_id, kind, reason, source, created_at, "
                "created_by, transaction_id) "
                "VALUES ('assay', 'GHOST_ASSAY', 'qc_fail', 'r', 'manual', "
                "'2026-04-17T00:00:00', 'me', 'txn_x')"
            )
    finally:
        conn.close()
    res = _run(["validate", "--project-dir", str(seeded)], check=False)
    assert res.returncode != 0
    assert "missing assay" in res.stderr
