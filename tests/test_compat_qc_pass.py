"""Backwards-compatibility: v0.3 projects with a legacy qc_pass BOOLEAN
column should still be readable on v0.4, and migrate-qc must leave the
project in a state equivalent to a fresh v0.4 init.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import casetrack


CASETRACK_BIN = [sys.executable, str(Path(__file__).resolve().parent.parent / "casetrack.py")]


def _run(
    args: list[str], check: bool = True, *, allow_legacy: bool = False,
) -> subprocess.CompletedProcess:
    """Run a casetrack subcommand. `allow_legacy` sets
    CASETRACK_ALLOW_LEGACY=1 in the subprocess env — needed when the test
    deliberately operates on a v0.3 project that hasn't been migrated to
    the v0.6 identity scheme yet (see proposal 0005 §9).
    """
    import os as _os
    env = None
    if allow_legacy:
        env = _os.environ.copy()
        env["CASETRACK_ALLOW_LEGACY"] = "1"
    return subprocess.run(
        CASETRACK_BIN + args, check=check, capture_output=True, text=True,
        env=env,
    )


def _build_v03(proj: Path) -> None:
    """Emulate a v0.3 project shape (no QC schema)."""
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
            'CREATE TABLE "patients" ("patient_id" TEXT NOT NULL UNIQUE, PRIMARY KEY ("patient_id"));'
            'CREATE TABLE "specimens" ("specimen_id" TEXT NOT NULL UNIQUE, "patient_id" TEXT NOT NULL, '
            'PRIMARY KEY ("specimen_id"), FOREIGN KEY ("patient_id") REFERENCES "patients"("patient_id"));'
            'CREATE TABLE "assays" ("assay_id" TEXT NOT NULL UNIQUE, "specimen_id" TEXT NOT NULL, '
            '"assay_type" TEXT NOT NULL, "qc_pass" BOOLEAN, '
            'PRIMARY KEY ("assay_id"), '
            'FOREIGN KEY ("specimen_id") REFERENCES "specimens"("specimen_id"));'
        )
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients VALUES ('P1');"
                "INSERT INTO specimens VALUES ('S1','P1');"
                "INSERT INTO assays VALUES "
                "  ('A1','S1','ONT',1),"
                "  ('A2','S1','ONT',0);"
            )
    finally:
        conn.close()


def test_v03_project_readable_on_v04_status(tmp_path: Path):
    """status must not crash on a v0.3 project that hasn't been migrated yet.

    v0.6 Part B final adds a hard-error gate refusing un-migrated projects
    (proposal 0005 §9 step 4). The documented bypass for read-only audits
    of legacy cohorts is CASETRACK_ALLOW_LEGACY=1 — exercising it here.
    """
    proj = tmp_path / "v03"
    _build_v03(proj)
    res = _run(
        ["status", "--project-dir", str(proj), "--fmt", "json"],
        allow_legacy=True,
    )
    assert res.returncode == 0, res.stderr


def test_migrate_qc_preserves_qc_pass_information(tmp_path: Path):
    proj = tmp_path / "v03"
    _build_v03(proj)

    _run(["migrate-qc", "--project-dir", str(proj)])

    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        # A1 had qc_pass=True → qc_status=pass, no event.
        (a1,) = conn.execute(
            "SELECT qc_status FROM assays WHERE assay_id='A1'"
        ).fetchone()
        assert a1 == "pass"
        # A2 had qc_pass=False → qc_status=fail + 1 qc_events row.
        (a2,) = conn.execute(
            "SELECT qc_status FROM assays WHERE assay_id='A2'"
        ).fetchone()
        assert a2 == "fail"
        (cnt,) = conn.execute(
            "SELECT COUNT(*) FROM qc_events WHERE entity_id='A2'"
        ).fetchone()
        assert cnt == 1
        # qc_pass column is gone.
        cols = {r[1] for r in conn.execute('PRAGMA table_info("assays")').fetchall()}
        assert "qc_pass" not in cols
        assert "qc_status" in cols
    finally:
        conn.close()


def test_migrate_qc_is_reversible_via_recover(tmp_path: Path):
    proj = tmp_path / "v03"
    _build_v03(proj)

    # Seed a baseline init_project provenance entry so recover has a start.
    import json
    init_entry = {
        "action": "init_project",
        "transaction_id": "txn_init",
        "template": "blank",
        "project_name": "legacy",
        "schema_v_before": 0,
        "schema_v_after": 1,
        "sql": [
            'CREATE TABLE "patients" ("patient_id" TEXT NOT NULL UNIQUE, PRIMARY KEY ("patient_id"))',
            'CREATE TABLE "specimens" ("specimen_id" TEXT NOT NULL UNIQUE, "patient_id" TEXT NOT NULL, '
            'PRIMARY KEY ("specimen_id"), FOREIGN KEY ("patient_id") REFERENCES "patients"("patient_id"))',
            'CREATE TABLE "assays" ("assay_id" TEXT NOT NULL UNIQUE, "specimen_id" TEXT NOT NULL, '
            '"assay_type" TEXT NOT NULL, "qc_pass" BOOLEAN, PRIMARY KEY ("assay_id"), '
            'FOREIGN KEY ("specimen_id") REFERENCES "specimens"("specimen_id"))',
        ],
    }
    reg_entries = [
        {"action": "register", "level": "patient", "id": "P1",
         "transaction_id": "txn_p1"},
        {"action": "register", "level": "specimen", "id": "S1", "parent": "P1",
         "parent_created": False, "meta": {}, "transaction_id": "txn_s1"},
        {"action": "register", "level": "assay", "id": "A1", "parent": "S1",
         "parent_created": False, "meta": {"assay_type": "ONT", "qc_pass": 1},
         "transaction_id": "txn_a1"},
        {"action": "register", "level": "assay", "id": "A2", "parent": "S1",
         "parent_created": False, "meta": {"assay_type": "ONT", "qc_pass": 0},
         "transaction_id": "txn_a2"},
    ]
    with open(proj / "provenance.jsonl", "w") as f:
        f.write(json.dumps(init_entry) + "\n")
        for e in reg_entries:
            f.write(json.dumps(e) + "\n")

    # Run migrate-qc, then recover.
    _run(["migrate-qc", "--project-dir", str(proj)])
    (proj / "casetrack.db").unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(proj / "casetrack.db") + suffix)
        if p.exists():
            p.unlink()

    res = _run(["recover", "--project-dir", str(proj), "--force"])
    assert res.returncode == 0, res.stderr

    # A2 should still end up as fail via replay of migrate_qc.
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        (status,) = conn.execute(
            "SELECT qc_status FROM assays WHERE assay_id='A2'"
        ).fetchone()
        assert status == "fail"
    finally:
        conn.close()
