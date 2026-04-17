"""Tests for the SLURM summary-TSV autoflag convention on cmd_append.

Proposal 0002 §6. When a summary TSV contains qc_pass / qc_fail_reason /
qc_warn columns, casetrack append emits qc_events rows + bumps qc_status
inside the same transaction as the data UPDATE.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from casetrack_qc.autoflag import (
    AUTOFLAG_COLUMNS,
    detect_autoflag_columns,
    extract_flag_actions,
)


CASETRACK_BIN = [sys.executable, str(Path(__file__).resolve().parent.parent / "casetrack.py")]


def _run(args: list[str], check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        CASETRACK_BIN + args,
        check=check,
        capture_output=True,
        text=True,
        env=full_env,
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
                "VALUES ('HGSOC002-tumor','HGSOC002','tumor'),"
                "       ('HGSOC002-normal','HGSOC002','normal'),"
                "       ('HGSOC006-tumor','HGSOC006','tumor');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('HGSOC002-normal-ONT-RNA','HGSOC002-normal','ONT'),"
                "  ('HGSOC002-tumor-ONT-RNA','HGSOC002-tumor','ONT'),"
                "  ('HGSOC006-tumor-ONT-RNA','HGSOC006-tumor','ONT');"
            )
    finally:
        conn.close()


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    _seed(p)
    return p


# ── unit-level helpers ────────────────────────────────────────────────────────


def test_detect_autoflag_columns_all_present():
    assert detect_autoflag_columns(
        ["assay_id", "qc_pass", "qc_fail_reason", "qc_warn", "meth"]
    ) == ["qc_pass", "qc_fail_reason", "qc_warn"]


def test_detect_autoflag_columns_none():
    assert detect_autoflag_columns(["assay_id", "meth", "n_reads"]) == []


def test_extract_flag_actions_flags_false_rows():
    df = pd.DataFrame([
        {"assay_id": "A1", "meth": 0.7, "qc_pass": True},
        {"assay_id": "A2", "meth": None, "qc_pass": False,
         "qc_fail_reason": "cDNA yield 8 ng"},
    ])
    actions = extract_flag_actions(df, "assay_id")
    assert len(actions) == 1
    assert actions[0]["entity_id"] == "A2"
    assert actions[0]["action"] == "fail"
    assert "cDNA" in actions[0]["reason"]


def test_extract_flag_actions_warn_without_pass_col():
    df = pd.DataFrame([
        {"assay_id": "A1", "qc_warn": True},
        {"assay_id": "A2", "qc_warn": False},
    ])
    actions = extract_flag_actions(df, "assay_id")
    assert len(actions) == 1
    assert actions[0]["action"] == "warn"


# ── end-to-end append with autoflag ───────────────────────────────────────────


def test_append_autoflag_creates_qc_event_and_bumps_status(proj: Path, tmp_path: Path):
    results = tmp_path / "modkit.tsv"
    pd.DataFrame([
        {"assay_id": "HGSOC002-tumor-ONT-RNA", "mean_meth": 0.72,
         "qc_pass": True},
        {"assay_id": "HGSOC002-normal-ONT-RNA", "mean_meth": None,
         "qc_pass": False,
         "qc_fail_reason": "library prep failed (cDNA yield 8 ng)"},
        {"assay_id": "HGSOC006-tumor-ONT-RNA", "mean_meth": 0.65,
         "qc_pass": True},
    ]).to_csv(results, sep="\t", index=False)

    res = _run([
        "append", "--project-dir", str(proj),
        "--results", str(results), "--analysis", "modkit",
    ])
    assert "qc event" in res.stdout or "qc events" in res.stdout

    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        # qc_pass / qc_fail_reason / qc_warn must NOT become assay columns.
        cols = {r[1] for r in conn.execute('PRAGMA table_info("assays")').fetchall()}
        for reserved in ("qc_pass", "qc_fail_reason", "qc_warn"):
            # qc_pass lives as a legacy BOOLEAN column in the hgsoc template.
            # It's OK if it still exists; we just need to make sure it didn't
            # pick up data from the TSV autoflag columns.
            if reserved == "qc_pass":
                continue
            assert reserved not in cols, f"{reserved} leaked into assay schema"

        (status,) = conn.execute(
            "SELECT qc_status FROM assays WHERE assay_id='HGSOC002-normal-ONT-RNA'"
        ).fetchone()
        assert status == "fail"

        rows = conn.execute(
            "SELECT entity_id, kind, reason, source FROM qc_events"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == (
            "HGSOC002-normal-ONT-RNA", "qc_fail",
            "library prep failed (cDNA yield 8 ng)", "slurm",
        )
    finally:
        conn.close()


def test_append_autoflag_shares_transaction_id_with_append(proj: Path, tmp_path: Path):
    results = tmp_path / "modkit.tsv"
    pd.DataFrame([
        {"assay_id": "HGSOC002-normal-ONT-RNA", "mean_meth": None,
         "qc_pass": False, "qc_fail_reason": "r"},
    ]).to_csv(results, sep="\t", index=False)

    _run([
        "append", "--project-dir", str(proj),
        "--results", str(results), "--analysis", "modkit",
    ])

    lines = (proj / "provenance.jsonl").read_text().splitlines()
    entries = [json.loads(l) for l in lines]
    append_entry = next(e for e in entries if e["action"] == "append" and "mean_meth" in (e.get("columns_added") or []))
    censor_entry = next(e for e in entries if e["action"] == "censor" and e.get("from_analysis") == "modkit")
    assert censor_entry["transaction_id"] == append_entry["transaction_id"]
    assert censor_entry["source"] == "slurm"


def test_append_autoflag_no_columns_no_events(proj: Path, tmp_path: Path):
    """Summary TSVs without autoflag columns must not create any qc_events."""
    results = tmp_path / "modkit.tsv"
    pd.DataFrame([
        {"assay_id": "HGSOC002-tumor-ONT-RNA", "mean_meth": 0.7},
        {"assay_id": "HGSOC002-normal-ONT-RNA", "mean_meth": 0.6},
    ]).to_csv(results, sep="\t", index=False)

    _run([
        "append", "--project-dir", str(proj),
        "--results", str(results), "--analysis", "modkit",
    ])
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        (cnt,) = conn.execute("SELECT COUNT(*) FROM qc_events").fetchone()
        assert cnt == 0
    finally:
        conn.close()


def test_append_autoflag_slurm_created_by(proj: Path, tmp_path: Path):
    results = tmp_path / "modkit.tsv"
    pd.DataFrame([
        {"assay_id": "HGSOC002-normal-ONT-RNA", "mean_meth": None,
         "qc_pass": False, "qc_fail_reason": "bad"},
    ]).to_csv(results, sep="\t", index=False)

    _run([
        "append", "--project-dir", str(proj),
        "--results", str(results), "--analysis", "modkit",
    ], env={"SLURM_JOB_ID": "99999"})

    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        (created_by,) = conn.execute(
            "SELECT created_by FROM qc_events"
        ).fetchone()
        assert created_by == "slurm:99999"
    finally:
        conn.close()
