"""Tests for `casetrack migrate` (v0.3 / proposal 0001 §13.1).

Covers column routing, type inference, FK enforcement during insert,
--metadata-map overrides, audit report, and sandbox copy.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import casetrack


# ── helpers ───────────────────────────────────────────────────────────────────


def _migrate_ns(
    flat: Path,
    out_dir: Path,
    *,
    patient_col: str = "patient_id",
    specimen_col: str = "specimen_id",
    assay_col: str = "assay_id",
    metadata_map: str | None = None,
    force: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        flat=str(flat),
        out_dir=str(out_dir),
        patient_col=patient_col,
        specimen_col=specimen_col,
        assay_col=assay_col,
        metadata_map=metadata_map,
        project_name=None,
        force=force,
    )


def _write_flat_hgsoc_mini(path: Path) -> Path:
    """Two patients, three specimens, four assays — with columns at every level."""
    df = pd.DataFrame([
        # patient-level constant: age, sex. specimen-level constant: tissue_site, coverage.
        # assay-level varying: assay_type.
        {"patient_id": "P1", "specimen_id": "S1", "assay_id": "A1",
         "age": 60, "sex": "F", "tissue_site": "tumor",
         "coverage": 32.1, "assay_type": "WGS"},
        {"patient_id": "P1", "specimen_id": "S1", "assay_id": "A2",
         "age": 60, "sex": "F", "tissue_site": "tumor",
         "coverage": 32.1, "assay_type": "ATAC"},
        {"patient_id": "P1", "specimen_id": "S2", "assay_id": "A3",
         "age": 60, "sex": "F", "tissue_site": "normal",
         "coverage": 28.5, "assay_type": "WGS"},
        {"patient_id": "P2", "specimen_id": "S3", "assay_id": "A4",
         "age": 55, "sex": "F", "tissue_site": "tumor",
         "coverage": 41.2, "assay_type": "WGS"},
    ])
    df.to_csv(path, sep="\t", index=False)
    return path


# ── Classifier ────────────────────────────────────────────────────────────────


def test_classify_constant_per_patient(tmp_path: Path):
    flat = _write_flat_hgsoc_mini(tmp_path / "flat.tsv")
    df = pd.read_csv(flat, sep="\t")
    level, reason = casetrack._classify_column(df, "age", "patient_id", "specimen_id")
    assert level == "patient"
    assert "patient" in reason


def test_classify_constant_per_specimen(tmp_path: Path):
    flat = _write_flat_hgsoc_mini(tmp_path / "flat.tsv")
    df = pd.read_csv(flat, sep="\t")
    level, _ = casetrack._classify_column(df, "tissue_site", "patient_id", "specimen_id")
    assert level == "specimen"


def test_classify_assay_level_when_varies_within_specimen(tmp_path: Path):
    flat = _write_flat_hgsoc_mini(tmp_path / "flat.tsv")
    df = pd.read_csv(flat, sep="\t")
    level, _ = casetrack._classify_column(df, "assay_type", "patient_id", "specimen_id")
    assert level == "assay"


def test_type_inference():
    assert casetrack._infer_column_type(pd.Series([1, 2, 3])) == "INTEGER"
    assert casetrack._infer_column_type(pd.Series([1.0, 2.5])) == "REAL"
    assert casetrack._infer_column_type(pd.Series(["a", "b"])) == "TEXT"
    assert casetrack._infer_column_type(pd.Series([True, False])) == "BOOLEAN"


# ── metadata-map parser ───────────────────────────────────────────────────────


def test_metadata_map_parses_clean_input():
    out = casetrack._parse_metadata_map("patient:age,sex;specimen:tissue_site")
    assert out["patient"] == {"age", "sex"}
    assert out["specimen"] == {"tissue_site"}
    assert out["assay"] == set()


def test_metadata_map_empty_input_returns_empty_sets():
    out = casetrack._parse_metadata_map("")
    assert all(v == set() for v in out.values())
    out = casetrack._parse_metadata_map(None)
    assert all(v == set() for v in out.values())


def test_metadata_map_rejects_unknown_level():
    with pytest.raises(casetrack.MigrationError, match="unknown level"):
        casetrack._parse_metadata_map("cohort:size")


def test_metadata_map_rejects_malformed_chunk():
    with pytest.raises(casetrack.MigrationError, match="expected 'level:col"):
        casetrack._parse_metadata_map("patient_age_no_colon")


# ── End-to-end migration ──────────────────────────────────────────────────────


def test_migrate_creates_project(tmp_path: Path, capsys):
    flat = _write_flat_hgsoc_mini(tmp_path / "flat.tsv")
    out = tmp_path / "proj"

    casetrack.cmd_migrate(_migrate_ns(flat, out))
    captured = capsys.readouterr()
    assert "Migrated" in captured.out

    assert (out / "casetrack.db").exists()
    assert (out / "casetrack.toml").exists()
    assert (out / "provenance.jsonl").exists()
    assert (out / ".gitignore").exists()
    assert (out / ".migration_report.tsv").exists()
    assert (out / ".migration_report.md").exists()
    assert (out / "sandbox" / "source_manifest.tsv").exists()


def test_migrate_inserts_deduplicated_rows(tmp_path: Path):
    flat = _write_flat_hgsoc_mini(tmp_path / "flat.tsv")
    out = tmp_path / "proj"
    casetrack.cmd_migrate(_migrate_ns(flat, out))

    conn = casetrack.open_project_db(out / "casetrack.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM specimens").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM assays").fetchone()[0] == 4

        # Patient-level column persisted at patient row only.
        (age_col,) = conn.execute("SELECT age FROM patients WHERE patient_id='P1'").fetchone()
        assert age_col == 60

        # Specimen-level column persisted at specimen row.
        rows = dict(conn.execute("SELECT specimen_id, tissue_site FROM specimens").fetchall())
        assert rows["S1"] == "tumor"
        assert rows["S2"] == "normal"

        # Assay-level varied.
        atypes = dict(conn.execute("SELECT assay_id, assay_type FROM assays").fetchall())
        assert atypes == {"A1": "WGS", "A2": "ATAC", "A3": "WGS", "A4": "WGS"}
    finally:
        conn.close()


def test_migration_report_matches_classifications(tmp_path: Path):
    flat = _write_flat_hgsoc_mini(tmp_path / "flat.tsv")
    out = tmp_path / "proj"
    casetrack.cmd_migrate(_migrate_ns(flat, out))

    report = pd.read_csv(out / ".migration_report.tsv", sep="\t")
    row_map = dict(zip(report["column"], report["assigned_level"]))
    assert row_map == {
        "age": "patient",
        "sex": "patient",
        "tissue_site": "specimen",
        "coverage": "specimen",
        "assay_type": "assay",
    }


def test_provenance_records_migrate_event(tmp_path: Path):
    flat = _write_flat_hgsoc_mini(tmp_path / "flat.tsv")
    out = tmp_path / "proj"
    casetrack.cmd_migrate(_migrate_ns(flat, out))

    entries = [
        json.loads(line)
        for line in (out / "provenance.jsonl").read_text().splitlines()
        if line.strip()
    ]
    actions = [e["action"] for e in entries]
    assert "migrate" in actions
    migrate_entry = next(e for e in entries if e["action"] == "migrate")
    assert migrate_entry["rows_inserted"] == {"patient": 2, "specimen": 3, "assay": 4}
    assert migrate_entry["column_classifications"]["age"] == "patient"
    assert "source_checksum" in migrate_entry


# ── Overrides ─────────────────────────────────────────────────────────────────


def test_metadata_map_overrides_heuristic(tmp_path: Path):
    """Force `age` onto assay level even though it's constant within patients."""
    flat = _write_flat_hgsoc_mini(tmp_path / "flat.tsv")
    out = tmp_path / "proj"

    casetrack.cmd_migrate(_migrate_ns(flat, out, metadata_map="assay:age"))

    report = pd.read_csv(out / ".migration_report.tsv", sep="\t")
    row = report[report["column"] == "age"].iloc[0]
    assert row["assigned_level"] == "assay"
    assert "override" in row["reason"]


# ── FK enforcement ────────────────────────────────────────────────────────────


def test_migrate_aborts_on_dirty_parent_ref(tmp_path: Path, capsys):
    """specimen_id 'S1' attributed to two different patients violates the UNIQUE PK on specimen_id."""
    flat = tmp_path / "flat.tsv"
    pd.DataFrame([
        {"patient_id": "P1", "specimen_id": "S1", "assay_id": "A1", "assay_type": "WGS"},
        {"patient_id": "P2", "specimen_id": "S1", "assay_id": "A2", "assay_type": "WGS"},
    ]).to_csv(flat, sep="\t", index=False)

    out = tmp_path / "proj"
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_migrate(_migrate_ns(flat, out))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "aborted" in err


# ── Error paths ───────────────────────────────────────────────────────────────


def test_missing_flat_file_exits(tmp_path: Path, capsys):
    out = tmp_path / "proj"
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_migrate(_migrate_ns(tmp_path / "ghost.tsv", out))
    assert excinfo.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_missing_required_column_exits(tmp_path: Path, capsys):
    flat = tmp_path / "flat.tsv"
    pd.DataFrame({"patient_id": ["P1"], "assay_id": ["A1"]}).to_csv(flat, sep="\t", index=False)
    out = tmp_path / "proj"
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_migrate(_migrate_ns(flat, out))  # no specimen_id column
    assert excinfo.value.code == 1
    assert "specimen_id" in capsys.readouterr().err


def test_refuses_overwrite_without_force(tmp_path: Path, capsys):
    flat = _write_flat_hgsoc_mini(tmp_path / "flat.tsv")
    out = tmp_path / "proj"
    casetrack.cmd_migrate(_migrate_ns(flat, out))

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_migrate(_migrate_ns(flat, out))
    assert excinfo.value.code == 1
    assert "already exists" in capsys.readouterr().err


def test_force_rewrites_db(tmp_path: Path):
    flat = _write_flat_hgsoc_mini(tmp_path / "flat.tsv")
    out = tmp_path / "proj"
    casetrack.cmd_migrate(_migrate_ns(flat, out))

    # Mutate the source and re-run with --force.
    pd.DataFrame([
        {"patient_id": "PX", "specimen_id": "SX", "assay_id": "AX", "assay_type": "WGS"},
    ]).to_csv(flat, sep="\t", index=False)
    casetrack.cmd_migrate(_migrate_ns(flat, out, force=True))

    conn = sqlite3.connect(str(out / "casetrack.db"))
    try:
        patients = [r[0] for r in conn.execute("SELECT patient_id FROM patients").fetchall()]
        assert patients == ["PX"]
    finally:
        conn.close()
