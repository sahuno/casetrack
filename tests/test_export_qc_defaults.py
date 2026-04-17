"""Tests for cmd_export QC-aware defaults.

Default: excludes QC-failed + consent-revoked. ``--include-censored`` adds
fail/censored back. ``--include-consent-revoked`` adds consent-revoked back.
An audit line prints to stderr summarizing what was filtered.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd
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
                "INSERT INTO patients (patient_id) VALUES ('HGSOC002'), ('HGSOC099');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES ('HGSOC002-normal','HGSOC002','normal'),"
                "       ('HGSOC002-tumor','HGSOC002','tumor'),"
                "       ('HGSOC099-normal','HGSOC099','normal');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('HGSOC002-normal-ONT-RNA','HGSOC002-normal','ONT'),"
                "  ('HGSOC002-tumor-ONT-RNA','HGSOC002-tumor','ONT'),"
                "  ('HGSOC099-normal-ONT-RNA','HGSOC099-normal','ONT');"
            )
    finally:
        conn.close()


@pytest.fixture
def cohort(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    _seed(p)
    _run([
        "censor", "--project-dir", str(p),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "bad",
    ])
    _run([
        "censor", "--project-dir", str(p),
        "--level", "patient", "--id", "HGSOC099",
        "--kind", "consent_revoked", "--reason", "withdrew",
        "--withdrawal-date", "2026-03-15",
    ])
    return p


def test_export_tables_default_filters_censored(cohort: Path, tmp_path: Path):
    out = tmp_path / "out"
    res = _run([
        "export", "--project-dir", str(cohort),
        "--output", str(out), "--shape", "tables",
    ])
    assert "excluded" in res.stderr
    df_assays = pd.read_csv(out / "assays.tsv", sep="\t")
    ids = set(df_assays["assay_id"])
    assert "HGSOC002-normal-ONT-RNA" not in ids
    assert "HGSOC099-normal-ONT-RNA" not in ids
    assert "HGSOC002-tumor-ONT-RNA" in ids


def test_export_include_censored_restores_failed(cohort: Path, tmp_path: Path):
    out = tmp_path / "out"
    _run([
        "export", "--project-dir", str(cohort),
        "--output", str(out), "--shape", "tables",
        "--include-censored",
    ])
    df_assays = pd.read_csv(out / "assays.tsv", sep="\t")
    ids = set(df_assays["assay_id"])
    assert "HGSOC002-normal-ONT-RNA" in ids
    # consent-revoked still excluded.
    assert "HGSOC099-normal-ONT-RNA" not in ids


def test_export_both_flags_includes_everything(cohort: Path, tmp_path: Path):
    out = tmp_path / "out"
    _run([
        "export", "--project-dir", str(cohort),
        "--output", str(out), "--shape", "tables",
        "--include-censored", "--include-consent-revoked",
    ])
    df_assays = pd.read_csv(out / "assays.tsv", sep="\t")
    assert len(df_assays) == 3


def test_export_joined_default_excludes_censored(cohort: Path, tmp_path: Path):
    out = tmp_path / "joined.tsv"
    _run([
        "export", "--project-dir", str(cohort),
        "--output", str(out), "--shape", "joined",
    ])
    df = pd.read_csv(out, sep="\t")
    ids = set(df["assay_id"])
    assert "HGSOC002-normal-ONT-RNA" not in ids
    assert "HGSOC099-normal-ONT-RNA" not in ids
