"""Recover replay of QC provenance actions.

Success criterion from proposal §13: `casetrack recover` round-trips a project
with a non-trivial QC history byte-identical to the original DB (modulo rowid
ordering and auto-increment gaps). We test for equivalent end state — same
events, same qc_status columns, same consent column state.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import casetrack


CASETRACK_BIN = [sys.executable, str(Path(__file__).resolve().parent.parent / "casetrack.py")]


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        CASETRACK_BIN + args,
        check=check,
        capture_output=True,
        text=True,
    )


def _seed_cohort(proj: Path) -> None:
    ns = argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="hgsoc_test", force=False,
    )
    casetrack.cmd_init(ns)
    for pid in ("HGSOC002", "HGSOC006"):
        _run(["register", "--project-dir", str(proj), "--level", "patient",
              "--id", pid])
    for sid, pid, tissue in (
        ("HGSOC002-tumor", "HGSOC002", "tumor"),
        ("HGSOC002-normal", "HGSOC002", "normal"),
        ("HGSOC006-tumor", "HGSOC006", "tumor"),
    ):
        _run(["register", "--project-dir", str(proj), "--level", "specimen",
              "--id", sid, "--parent", pid,
              "--meta", f"tissue_site={tissue}"])
    for aid, sid in (
        ("HGSOC002-normal-ONT-RNA", "HGSOC002-normal"),
        ("HGSOC002-tumor-ONT-RNA",  "HGSOC002-tumor"),
        ("HGSOC006-tumor-ONT-RNA",  "HGSOC006-tumor"),
    ):
        _run(["register", "--project-dir", str(proj), "--level", "assay",
              "--id", aid, "--parent", sid,
              "--meta", "assay_type=ONT"])


def _db_snapshot(db_path: Path) -> dict:
    """Hashable snapshot of the QC-relevant state for comparison."""
    conn = sqlite3.connect(str(db_path))
    try:
        events = conn.execute(
            "SELECT level, entity_id, kind, reason, source, created_by, "
            "resolved_at IS NULL AS active FROM qc_events "
            "ORDER BY level, entity_id, kind"
        ).fetchall()
        patients = conn.execute(
            "SELECT patient_id, qc_status, consent_status, consent_date, "
            "withdrawal_date FROM patients ORDER BY patient_id"
        ).fetchall()
        specimens = conn.execute(
            "SELECT specimen_id, qc_status FROM specimens ORDER BY specimen_id"
        ).fetchall()
        assays = conn.execute(
            "SELECT assay_id, qc_status FROM assays ORDER BY assay_id"
        ).fetchall()
        return {
            "events": events,
            "patients": patients,
            "specimens": specimens,
            "assays": assays,
        }
    finally:
        conn.close()


@pytest.fixture
def history_proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    _seed_cohort(proj)

    # Mixed history: two active events, one resolved event, one consent revocation.
    _run(["censor", "--project-dir", str(proj),
          "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
          "--kind", "library_prep_failed", "--reason", "cDNA yield 8 ng"])
    _run(["censor", "--project-dir", str(proj),
          "--level", "assay", "--id", "HGSOC002-tumor-ONT-RNA",
          "--kind", "qc_warn", "--reason", "borderline depth"])
    _run(["censor", "--project-dir", str(proj),
          "--level", "assay", "--id", "HGSOC006-tumor-ONT-RNA",
          "--kind", "qc_fail", "--reason", "contamination"])
    # Now resolve the HGSOC006 one.
    _run(["uncensor", "--project-dir", str(proj),
          "--level", "assay", "--id", "HGSOC006-tumor-ONT-RNA",
          "--reason", "re-seq passed"])
    # Revoke consent for HGSOC002.
    _run(["censor", "--project-dir", str(proj),
          "--level", "patient", "--id", "HGSOC002",
          "--kind", "consent_revoked", "--reason", "withdrew 2026-03-15",
          "--withdrawal-date", "2026-03-15"])
    return proj


def test_recover_replays_qc_actions(history_proj: Path, tmp_path: Path):
    before = _db_snapshot(history_proj / "casetrack.db")

    # Backup provenance, blow away DB, replay.
    prov_backup = tmp_path / "prov.jsonl"
    shutil.copy(history_proj / "provenance.jsonl", prov_backup)
    (history_proj / "casetrack.db").unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(history_proj / "casetrack.db") + suffix)
        if p.exists():
            p.unlink()

    # Recover.
    res = _run(["recover", "--project-dir", str(history_proj), "--force"])
    assert res.returncode == 0, res.stderr

    after = _db_snapshot(history_proj / "casetrack.db")

    # Every event, status, consent value should match.
    assert before["events"] == after["events"]
    assert before["patients"] == after["patients"]
    assert before["specimens"] == after["specimens"]
    assert before["assays"] == after["assays"]


def test_recover_handles_migrate_qc_entry(tmp_path: Path):
    """A migrate-qc provenance entry replays into the same schema changes."""
    # Build a legacy project manually (no QC), migrate it, then recover.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "provenance.jsonl").touch()

    # Minimal v0.3 shape.
    (proj / "casetrack.toml").write_text(
        '[project]\nname = "x"\nschema_v = 1\n'
        '[levels.patient]\nkey = "patient_id"\n'
        '[levels.patient.columns]\n'
        'patient_id = { type = "TEXT", required = true, unique = true }\n'
        '[levels.specimen]\nkey = "specimen_id"\nparent = "patient"\n'
        'parent_key = "patient_id"\n'
        '[levels.specimen.columns]\n'
        'specimen_id = { type = "TEXT", required = true, unique = true }\n'
        'patient_id = { type = "TEXT", required = true }\n'
        '[levels.assay]\nkey = "assay_id"\nparent = "specimen"\n'
        'parent_key = "specimen_id"\n'
        '[levels.assay.columns]\n'
        'assay_id = { type = "TEXT", required = true, unique = true }\n'
        'specimen_id = { type = "TEXT", required = true }\n'
        'assay_type = { type = "TEXT", required = true }\n'
    )
    # Seed an init_project provenance so recover has a baseline.
    init_sql = [
        'CREATE TABLE "patients" ("patient_id" TEXT NOT NULL UNIQUE, PRIMARY KEY ("patient_id"))',
        'CREATE TABLE "specimens" ("specimen_id" TEXT NOT NULL UNIQUE, "patient_id" TEXT NOT NULL, '
        'PRIMARY KEY ("specimen_id"), FOREIGN KEY ("patient_id") REFERENCES "patients"("patient_id"))',
        'CREATE TABLE "assays" ("assay_id" TEXT NOT NULL UNIQUE, "specimen_id" TEXT NOT NULL, '
        '"assay_type" TEXT NOT NULL, PRIMARY KEY ("assay_id"), '
        'FOREIGN KEY ("specimen_id") REFERENCES "specimens"("specimen_id"))',
    ]
    (proj / "provenance.jsonl").write_text(json.dumps({
        "action": "init_project",
        "transaction_id": "txn_init",
        "template": "blank",
        "project_name": "x",
        "schema_v_before": 0,
        "schema_v_after": 1,
        "sql": init_sql,
    }) + "\n")
    # Apply the DDL so the recover run has something to rebuild from scratch.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            for s in init_sql:
                conn.execute(s)
    finally:
        conn.close()

    # Write a migrate_qc provenance entry (no migrated rows — just schema).
    with open(proj / "provenance.jsonl", "a") as f:
        f.write(json.dumps({
            "action": "migrate_qc",
            "transaction_id": "txn_migrate",
            "executed_sql": [],
            "migrated_rows": [],
            "legacy_column": None,
            "toml_updated": True,
        }) + "\n")

    (proj / "casetrack.db").unlink()
    res = _run(["recover", "--project-dir", str(proj), "--force"])
    assert res.returncode == 0, res.stderr

    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "qc_events" in names
        cols = {r[1] for r in conn.execute('PRAGMA table_info("assays")').fetchall()}
        assert "qc_status" in cols
    finally:
        conn.close()
