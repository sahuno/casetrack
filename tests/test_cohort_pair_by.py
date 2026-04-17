"""Tests for `casetrack cohort --pair-by` (proposal §8.3).

Covers the HGSOC002 broken-pair worked example + N-partition longitudinal.

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


# ── 2-partition cohort (tumor/normal) ─────────────────────────────────────────


def _seed_paired(proj: Path) -> None:
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
def paired(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    _seed_paired(p)
    _run([
        "censor", "--project-dir", str(p),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "library_prep_failed", "--reason", "cDNA yield 8 ng",
    ])
    return p


def test_pair_by_tissue_site_flags_hgsoc002_broken(paired: Path):
    """The canonical §4.5 case: HGSOC002 = broken, HGSOC006 = complete."""
    res = _run([
        "cohort", "--project-dir", str(paired),
        "--assay-type", "ONT", "--pair-by", "tissue_site",
        "--fmt", "json",
    ])
    data = json.loads(res.stdout)
    assert data["pair_by"] == "tissue_site"
    assert sorted(data["partitions"]) == ["normal", "tumor"]
    by_patient = {r["patient_id"]: r for r in data["rows"]}
    assert by_patient["HGSOC002"]["status"] == "broken"
    assert by_patient["HGSOC006"]["status"] == "complete"
    assert data["summary"]["complete"] == 1
    assert data["summary"]["broken"] == 1


def test_pair_by_partition_order_respected(paired: Path):
    res = _run([
        "cohort", "--project-dir", str(paired),
        "--assay-type", "ONT", "--pair-by", "tissue_site",
        "--partition-order", "tumor,normal",
        "--fmt", "json",
    ])
    data = json.loads(res.stdout)
    assert data["partitions"] == ["tumor", "normal"]


def test_pair_by_broken_only_filters(paired: Path):
    res = _run([
        "cohort", "--project-dir", str(paired),
        "--assay-type", "ONT", "--pair-by", "tissue_site",
        "--broken-only", "--fmt", "json",
    ])
    data = json.loads(res.stdout)
    assert len(data["rows"]) == 1
    assert data["rows"][0]["patient_id"] == "HGSOC002"


# ── N-partition longitudinal cohort ───────────────────────────────────────────


def _seed_longitudinal(proj: Path) -> None:
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
                "  ('HGSOC010'),('HGSOC011'),('HGSOC013');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site, timepoint) "
                "VALUES "
                "  ('s10-base','HGSOC010','tumor','baseline'),"
                "  ('s10-post','HGSOC010','tumor','post-tx'),"
                "  ('s10-prog','HGSOC010','tumor','progression'),"
                "  ('s11-base','HGSOC011','tumor','baseline'),"
                "  ('s11-post','HGSOC011','tumor','post-tx'),"
                "  ('s13-base','HGSOC013','tumor','baseline');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('a10-base','s10-base','ONT'),"
                "  ('a10-post','s10-post','ONT'),"
                "  ('a10-prog','s10-prog','ONT'),"
                "  ('a11-base','s11-base','ONT'),"
                "  ('a11-post','s11-post','ONT'),"
                "  ('a13-base','s13-base','ONT');"
            )
    finally:
        conn.close()


@pytest.fixture
def longitudinal(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    _seed_longitudinal(p)
    return p


def test_pair_by_three_partition_categorization(longitudinal: Path):
    res = _run([
        "cohort", "--project-dir", str(longitudinal),
        "--assay-type", "ONT", "--pair-by", "timepoint",
        "--partition-order", "baseline,post-tx,progression",
        "--fmt", "json",
    ])
    data = json.loads(res.stdout)
    assert data["partitions"] == ["baseline", "post-tx", "progression"]
    by_patient = {r["patient_id"]: r for r in data["rows"]}
    assert by_patient["HGSOC010"]["status"] == "complete"
    assert by_patient["HGSOC011"]["status"] == "incomplete"
    assert by_patient["HGSOC013"]["status"] == "singleton"
    s = data["summary"]
    assert s["complete"] == 1 and s["incomplete"] == 1 and s["singleton"] == 1


def test_pair_by_require_n_of_m(longitudinal: Path):
    res = _run([
        "cohort", "--project-dir", str(longitudinal),
        "--assay-type", "ONT", "--pair-by", "timepoint",
        "--partition-order", "baseline,post-tx,progression",
        "--require", "2", "--fmt", "json",
    ])
    data = json.loads(res.stdout)
    assert data["require_satisfied"] == 2   # HGSOC010 + HGSOC011


def test_pair_by_unknown_column_errors(longitudinal: Path):
    res = _run([
        "cohort", "--project-dir", str(longitudinal),
        "--pair-by", "not_a_column", "--fmt", "json",
    ], check=False)
    assert res.returncode != 0
    assert "not found" in res.stderr
