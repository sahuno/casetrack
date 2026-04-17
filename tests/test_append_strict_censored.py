"""Tests for the strict-refuse append gate on censored entities.

Proposal 0002 §5.1.1 and §9 (decision #9).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import sqlite3
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
                "INSERT INTO patients (patient_id) VALUES ('HGSOC002');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES ('HGSOC002-normal','HGSOC002','normal');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) "
                "VALUES ('HGSOC002-normal-ONT-RNA','HGSOC002-normal','ONT');"
            )
    finally:
        conn.close()


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    _seed(p)
    # Censor the assay up front.
    _run([
        "censor", "--project-dir", str(p),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "library_prep_failed", "--reason", "yield 8 ng",
    ])
    return p


def _results_tsv(path: Path) -> Path:
    pd.DataFrame([
        {"assay_id": "HGSOC002-normal-ONT-RNA", "mean_meth": 0.72},
    ]).to_csv(path, sep="\t", index=False)
    return path


def test_append_on_censored_assay_exits_2(seeded: Path, tmp_path: Path):
    results = _results_tsv(tmp_path / "modkit.tsv")
    res = _run([
        "append", "--project-dir", str(seeded),
        "--results", str(results), "--analysis", "modkit",
    ], check=False)
    assert res.returncode == 2
    assert "censored" in res.stderr
    assert "--force-append-on-censored" in res.stderr


def test_append_force_on_censored_succeeds_with_yes(seeded: Path, tmp_path: Path):
    results = _results_tsv(tmp_path / "modkit.tsv")
    res = _run([
        "append", "--project-dir", str(seeded),
        "--results", str(results), "--analysis", "modkit",
        "--force-append-on-censored", "--yes",
    ])
    assert res.returncode == 0
    assert "Forcing append on 1 censored" in res.stderr
    conn = sqlite3.connect(str(seeded / "casetrack.db"))
    try:
        (v,) = conn.execute(
            "SELECT mean_meth FROM assays "
            "WHERE assay_id='HGSOC002-normal-ONT-RNA'"
        ).fetchone()
        # Entity still marked fail; data still landed.
        assert v is not None
        (status,) = conn.execute(
            "SELECT qc_status FROM assays "
            "WHERE assay_id='HGSOC002-normal-ONT-RNA'"
        ).fetchone()
        assert status == "fail"
    finally:
        conn.close()


def test_append_force_requires_yes(seeded: Path, tmp_path: Path):
    results = _results_tsv(tmp_path / "modkit.tsv")
    res = _run([
        "append", "--project-dir", str(seeded),
        "--results", str(results), "--analysis", "modkit",
        "--force-append-on-censored",
    ], check=False)
    assert res.returncode == 2


def test_append_on_uncensored_assay_still_works(seeded: Path, tmp_path: Path):
    # Uncensor first.
    _run([
        "uncensor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--reason", "re-sequenced 2026-04-10, passes",
    ])
    results = _results_tsv(tmp_path / "modkit.tsv")
    res = _run([
        "append", "--project-dir", str(seeded),
        "--results", str(results), "--analysis", "modkit",
    ])
    assert res.returncode == 0
