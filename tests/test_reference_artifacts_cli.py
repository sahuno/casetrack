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


def _bootstrap_one_specimen(pdir):
    """Register P1 (patient) then S1 (specimen) via 'casetrack register'."""
    subprocess.run([sys.executable, "-m", "casetrack", "register",
                    "--project-dir", str(pdir), "--level", "patient",
                    "--id", "P1"],
                   check=True, capture_output=True, text=True)
    subprocess.run([sys.executable, "-m", "casetrack", "register",
                    "--project-dir", str(pdir), "--level", "specimen",
                    "--id", "S1", "--parent", "P1"],
                   check=True, capture_output=True, text=True)


def test_append_auto_captures_declared_uses(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n'
        '\n[analyses.clair3]\nlevel="specimen"\ncolumn_prefix="clair3"\nuses=["genome"]\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)

    summary = pdir / "clair3_summary.tsv"
    summary.write_text("specimen_id\tn_snv\nS1\t1000\n")
    subprocess.run([sys.executable, "-m", "casetrack", "append",
                    "--project-dir", str(pdir), "--analysis", "clair3",
                    "--level", "specimen",
                    "--results", str(summary), "--overwrite"],
                   check=True, capture_output=True, text=True)

    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    s = ra.output_staleness(conn, scope="analysis", entity_level="specimen",
                            entity_id="S1", analysis="clair3")
    assert s["state"] == "fresh"  # used hg38_v0, current is hg38_v0
    conn.close()


def test_no_track_references_skips_capture(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n'
        '\n[analyses.clair3]\nlevel="specimen"\ncolumn_prefix="clair3"\nuses=["genome"]\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)
    summary = pdir / "clair3_summary.tsv"
    summary.write_text("specimen_id\tn_snv\nS1\t1000\n")
    subprocess.run([sys.executable, "-m", "casetrack", "append",
                    "--project-dir", str(pdir), "--analysis", "clair3",
                    "--level", "specimen",
                    "--results", str(summary), "--overwrite",
                    "--no-track-references"], check=True, capture_output=True, text=True)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    s = ra.output_staleness(conn, scope="analysis", entity_level="specimen",
                            entity_id="S1", analysis="clair3")
    assert s["state"] == "untracked"
    conn.close()


def test_references_command_lists_and_filters_stale(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n'
        '\n[analyses.clair3]\nlevel="specimen"\ncolumn_prefix="clair3"\nuses=["genome"]\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)
    summary = pdir / "clair3_summary.tsv"; summary.write_text("specimen_id\tn_snv\nS1\t1\n")
    subprocess.run([sys.executable, "-m", "casetrack", "append", "--project-dir",
                    str(pdir), "--analysis", "clair3", "--level", "specimen",
                    "--results", str(summary), "--overwrite"],
                   check=True, capture_output=True, text=True)

    # list: genome present
    r = subprocess.run([sys.executable, "-m", "casetrack", "references",
                        "--project-dir", str(pdir), "--fmt", "json"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "genome" in r.stdout

    # bump version -> S1 becomes stale
    toml.write_text(toml.read_text().replace("hg38_v0", "hg38_v1"))
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    r2 = subprocess.run([sys.executable, "-m", "casetrack", "references",
                         "--project-dir", str(pdir), "--stale-only"],
                        capture_output=True, text=True)
    assert "S1" in r2.stdout and "STALE" in r2.stdout


def test_append_cohort_uses_references(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)
    # need an assay for cohort inputs (direct INSERT — add-metadata rejects
    # undeclared columns and assay_type is NOT NULL)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    conn.execute("INSERT INTO assays (assay_id, specimen_id, assay_type) "
                 "VALUES ('A1', 'S1', 'ONT')")
    conn.commit(); conn.close()
    vcf = pdir / "joint.vcf.gz"; vcf.write_text("x")
    subprocess.run([sys.executable, "-m", "casetrack", "append-cohort",
                    "--project-dir", str(pdir), "--analysis", "joint_genotype",
                    "--run-tag", "rt1", "--path", str(vcf), "--inputs", "A1",
                    "--uses-references", "genome"], check=True,
                   capture_output=True, text=True)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    aid = conn.execute("SELECT artifact_id FROM cohort_artifacts").fetchone()[0]
    s = ra.output_staleness(conn, scope="cohort", artifact_id=aid)
    assert s["state"] == "fresh"
    conn.close()
