# tests/test_reference_artifacts_cli.py
import subprocess
import sys
import sqlite3
from pathlib import Path
import pytest
import casetrack
from casetrack_qc import reference_artifacts as ra


def _init_project(tmp_path) -> Path:
    pdir = tmp_path / "proj"
    subprocess.run(
        [sys.executable, "-m", "casetrack", "init", "--project-dir", str(pdir),
         "--project-name", "proj"], check=True, capture_output=True, text=True)
    return pdir


def test_init_creates_reference_schema(tmp_path):
    pdir = _init_project(tmp_path)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    assert ra.reference_schema_exists(conn) is True


def test_migrate_references_is_idempotent(tmp_path):
    pdir = _init_project(tmp_path)
    # drop the tables to simulate a pre-0010 project
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    conn.execute("DROP TABLE reference_usage")
    conn.execute("DROP TABLE reference_artifacts")
    conn.commit(); conn.close()

    r = subprocess.run(
        [sys.executable, "-m", "casetrack", "migrate-references",
         "--project-dir", str(pdir)], capture_output=True, text=True)
    assert r.returncode == 0, f"migrate-references failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    assert ra.reference_schema_exists(conn)
    conn.close()
    # second run: no-op
    r2 = subprocess.run(
        [sys.executable, "-m", "casetrack", "migrate-references",
         "--project-dir", str(pdir)], capture_output=True, text=True)
    assert r2.returncode == 0
    assert "No migration needed" in r2.stdout
