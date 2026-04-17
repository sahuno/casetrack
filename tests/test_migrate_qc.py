"""Tests for `casetrack migrate-qc` (v0.3 → v0.4 one-shot).

Exercises the path where a v0.3 project has a legacy ``qc_pass`` column on
assays that must become ``qc_status`` + a resolved-less ``qc_events`` row.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import casetrack
from casetrack_qc.schema import qc_schema_exists


CASETRACK_BIN = [sys.executable, str(Path(__file__).resolve().parent.parent / "casetrack.py")]


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        CASETRACK_BIN + args,
        check=check,
        capture_output=True,
        text=True,
    )


# ── v0.3-style fixture ────────────────────────────────────────────────────────


def _build_legacy_project(proj: Path) -> None:
    """Build a minimal v0.3 project shape — tables + legacy qc_pass column,
    no QC schema."""
    proj.mkdir()
    (proj / "casetrack.toml").write_text(
        '[project]\nname = "legacy"\nschema_v = 1\n'
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
        'qc_pass = { type = "BOOLEAN" }\n'
    )
    (proj / "provenance.jsonl").touch()
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        conn.executescript(
            'CREATE TABLE "patients" ("patient_id" TEXT NOT NULL UNIQUE,'
            ' PRIMARY KEY ("patient_id"));'
            'CREATE TABLE "specimens" ("specimen_id" TEXT NOT NULL UNIQUE,'
            ' "patient_id" TEXT NOT NULL,'
            ' PRIMARY KEY ("specimen_id"),'
            ' FOREIGN KEY ("patient_id") REFERENCES "patients"("patient_id"));'
            'CREATE TABLE "assays" ("assay_id" TEXT NOT NULL UNIQUE,'
            ' "specimen_id" TEXT NOT NULL,'
            ' "assay_type" TEXT NOT NULL,'
            ' "qc_pass" BOOLEAN,'
            ' PRIMARY KEY ("assay_id"),'
            ' FOREIGN KEY ("specimen_id") REFERENCES "specimens"("specimen_id"));'
        )
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients VALUES ('HGSOC002'), ('HGSOC006');"
                "INSERT INTO specimens VALUES "
                "  ('HGSOC002-tumor', 'HGSOC002'),"
                "  ('HGSOC002-normal', 'HGSOC002'),"
                "  ('HGSOC006-normal', 'HGSOC006');"
                "INSERT INTO assays VALUES "
                "  ('HGSOC002-tumor-ONT-RNA', 'HGSOC002-tumor', 'ONT', 1),"
                "  ('HGSOC002-normal-ONT-RNA', 'HGSOC002-normal', 'ONT', 0),"
                "  ('HGSOC006-normal-ONT-RNA', 'HGSOC006-normal', 'ONT', NULL);"
            )
    finally:
        conn.close()


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    proj = tmp_path / "legacy_proj"
    _build_legacy_project(proj)
    return proj


# ── tests ─────────────────────────────────────────────────────────────────────


def test_migrate_qc_adds_schema_and_ports_legacy_rows(legacy: Path):
    res = _run(["migrate-qc", "--project-dir", str(legacy)])
    assert res.returncode == 0

    conn = sqlite3.connect(str(legacy / "casetrack.db"))
    try:
        # Schema is in place.
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "qc_events" in names
        cols = {r[1] for r in conn.execute('PRAGMA table_info("assays")').fetchall()}
        assert "qc_status" in cols
        assert "qc_pass" not in cols  # dropped

        # FALSE row → qc_events + qc_status=fail.
        rows = conn.execute(
            "SELECT entity_id, kind, reason, source, resolved_at FROM qc_events"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "HGSOC002-normal-ONT-RNA"
        assert rows[0][1] == "qc_fail"
        assert "legacy" in rows[0][2]
        assert rows[0][3] == "import"
        assert rows[0][4] is None

        statuses = dict(conn.execute(
            "SELECT assay_id, qc_status FROM assays"
        ).fetchall())
        assert statuses["HGSOC002-tumor-ONT-RNA"] == "pass"
        assert statuses["HGSOC002-normal-ONT-RNA"] == "fail"
        assert statuses["HGSOC006-normal-ONT-RNA"] == "pass"
    finally:
        conn.close()


def test_migrate_qc_is_idempotent(legacy: Path):
    _run(["migrate-qc", "--project-dir", str(legacy)])
    # Second call: no legacy column, schema already in place.
    res = _run(["migrate-qc", "--project-dir", str(legacy)])
    assert res.returncode == 0
    assert "No migration needed" in res.stdout


def test_migrate_qc_dry_run_makes_no_changes(legacy: Path):
    res = _run(["migrate-qc", "--project-dir", str(legacy), "--dry-run"])
    assert res.returncode == 0
    assert "[dry-run]" in res.stdout

    conn = sqlite3.connect(str(legacy / "casetrack.db"))
    try:
        # Still legacy shape.
        cols = {r[1] for r in conn.execute('PRAGMA table_info("assays")').fetchall()}
        assert "qc_pass" in cols
        assert "qc_status" not in cols
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "qc_events" not in names
    finally:
        conn.close()


def test_migrate_qc_appends_toml_block(legacy: Path):
    _run(["migrate-qc", "--project-dir", str(legacy)])
    toml_text = (legacy / "casetrack.toml").read_text()
    assert "[qc]" in toml_text
    assert "library_prep_failed" in toml_text


def test_migrate_qc_writes_provenance_entry(legacy: Path):
    _run(["migrate-qc", "--project-dir", str(legacy)])
    lines = (legacy / "provenance.jsonl").read_text().splitlines()
    entries = [json.loads(l) for l in lines]
    actions = {e["action"] for e in entries}
    assert "migrate_qc" in actions
    mq = next(e for e in entries if e["action"] == "migrate_qc")
    assert mq["legacy_column"] == "qc_pass"
    assert mq["toml_updated"] is True
    assert len(mq["migrated_rows"]) == 1
