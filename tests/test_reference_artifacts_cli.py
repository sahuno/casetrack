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


def test_schema_apply_syncs_references_and_logs_version_change(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    text = toml.read_text()
    text += (
        '\n[references.genome]\n'
        'path = "/db/hg38.fa"\nversion = "hg38_v0"\nkind = "genome"\n'
    )
    toml.write_text(text)
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True,
                   capture_output=True, text=True)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    assert ra.get_reference(conn, "genome").version == "hg38_v0"
    conn.close()

    # bump the version and re-apply
    toml.write_text(text.replace("hg38_v0", "hg38_v1"))
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True,
                   capture_output=True, text=True)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    assert ra.get_reference(conn, "genome").version == "hg38_v1"
    conn.close()
    prov = (pdir / "provenance.jsonl").read_text()
    assert "reference_version_change" in prov
    assert "hg38_v0" in prov and "hg38_v1" in prov
