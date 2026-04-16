"""Tests for `casetrack add-metadata --project-dir` (v0.3 / proposal 0001 §7.1).

Covers bulk UPDATE + opt-in bulk INSERT with --allow-new --yes, fill-only vs
--overwrite semantics, parent-FK enforcement for new rows, column validation
against the TOML schema, and provenance wiring.

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


def _init_ns(project_dir: Path, template: str = "hgsoc") -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None, key="sample_id",
        metadata=None, cols=None, from_template=template, project_name=None, force=False,
    )


def _reg_ns(project_dir: Path, *, level: str, id: str, parent: str | None = None,
            meta: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir), level=level, id=id, parent=parent,
        meta=meta, allow_new_parent=False, yes=False,
    )


def _meta_ns(project_dir: Path, *, level: str, metadata: Path,
             allow_new: bool = False, yes: bool = False,
             overwrite: bool = False, fill_only: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), metadata=str(metadata),
        key="sample_id", level=level,
        fill_only=fill_only, overwrite=overwrite, allow_new=allow_new, yes=yes,
    )


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    """Project with 3 patients registered (no metadata beyond IDs)."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    for pid in ("P1", "P2", "P3"):
        casetrack.cmd_register(_reg_ns(proj, level="patient", id=pid))
    return proj


def _conn(proj: Path) -> sqlite3.Connection:
    return casetrack.open_project_db(proj / "casetrack.db")


def _write_tsv(path: Path, df: pd.DataFrame) -> Path:
    df.to_csv(path, sep="\t", index=False)
    return path


# ── Happy path ────────────────────────────────────────────────────────────────


def test_bulk_update_existing_rows(seeded: Path, tmp_path: Path):
    tsv = _write_tsv(tmp_path / "clin.tsv", pd.DataFrame({
        "patient_id": ["P1", "P2", "P3"],
        "age": [55, 60, 65],
        "sex": ["F", "M", "F"],
    }))
    casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=tsv))

    with _conn(seeded) as c:
        rows = dict(c.execute("SELECT patient_id, age FROM patients").fetchall())
    assert rows == {"P1": 55, "P2": 60, "P3": 65}


def test_insert_new_rows_with_allow_new(seeded: Path, tmp_path: Path):
    """P1 gets updated, P99 gets inserted."""
    tsv = _write_tsv(tmp_path / "clin.tsv", pd.DataFrame({
        "patient_id": ["P1", "P99"],
        "age": [55, 72],
    }))
    casetrack.cmd_add_metadata(_meta_ns(
        seeded, level="patient", metadata=tsv, allow_new=True, yes=True,
    ))

    with _conn(seeded) as c:
        ids = {r[0] for r in c.execute("SELECT patient_id FROM patients").fetchall()}
    assert "P99" in ids
    assert "P1" in ids  # still exists


def test_missing_key_without_allow_new_exits_two(seeded: Path, tmp_path: Path, capsys):
    tsv = _write_tsv(tmp_path / "clin.tsv", pd.DataFrame({
        "patient_id": ["P1", "P_GHOST"],
        "age": [55, 99],
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=tsv))
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "P_GHOST" in err
    assert "--allow-new --yes" in err


def test_missing_key_rolls_back_updates(seeded: Path, tmp_path: Path):
    """One row exists (P1), one doesn't (P_GHOST) — no partial update allowed."""
    tsv = _write_tsv(tmp_path / "clin.tsv", pd.DataFrame({
        "patient_id": ["P1", "P_GHOST"],
        "age": [55, 99],
    }))
    with pytest.raises(SystemExit):
        casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=tsv))

    with _conn(seeded) as c:
        (age,) = c.execute("SELECT age FROM patients WHERE patient_id='P1'").fetchone()
    assert age is None  # P1's age should NOT have been updated


# ── Fill-only vs overwrite ────────────────────────────────────────────────────


def test_default_is_fill_only(seeded: Path, tmp_path: Path):
    t1 = _write_tsv(tmp_path / "t1.tsv", pd.DataFrame({
        "patient_id": ["P1"], "age": [55],
    }))
    t2 = _write_tsv(tmp_path / "t2.tsv", pd.DataFrame({
        "patient_id": ["P1"], "age": [99],
    }))
    casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=t1))
    casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=t2))
    with _conn(seeded) as c:
        (age,) = c.execute("SELECT age FROM patients WHERE patient_id='P1'").fetchone()
    assert age == 55  # first write wins under fill-only


def test_overwrite_replaces_existing(seeded: Path, tmp_path: Path):
    t1 = _write_tsv(tmp_path / "t1.tsv", pd.DataFrame({
        "patient_id": ["P1"], "age": [55],
    }))
    t2 = _write_tsv(tmp_path / "t2.tsv", pd.DataFrame({
        "patient_id": ["P1"], "age": [99],
    }))
    casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=t1))
    casetrack.cmd_add_metadata(_meta_ns(
        seeded, level="patient", metadata=t2, overwrite=True
    ))
    with _conn(seeded) as c:
        (age,) = c.execute("SELECT age FROM patients WHERE patient_id='P1'").fetchone()
    assert age == 99


def test_overwrite_and_fill_only_mutually_exclusive(seeded: Path, tmp_path: Path, capsys):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({
        "patient_id": ["P1"], "age": [55],
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(_meta_ns(
            seeded, level="patient", metadata=tsv, overwrite=True, fill_only=True,
        ))
    assert excinfo.value.code == 1
    assert "mutually exclusive" in capsys.readouterr().err


# ── Parent FK enforcement on new INSERTs ──────────────────────────────────────


def test_insert_at_specimen_requires_parent_column(seeded: Path, tmp_path: Path, capsys):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({
        "specimen_id": ["S_NEW"],
        "tissue_site": ["tumor"],
        # patient_id missing — can't insert.
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(_meta_ns(
            seeded, level="specimen", metadata=tsv, allow_new=True, yes=True,
        ))
    assert excinfo.value.code == 1
    assert "patient_id" in capsys.readouterr().err


def test_insert_at_specimen_missing_parent_exits_two(seeded: Path, tmp_path: Path, capsys):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({
        "specimen_id": ["S_NEW"],
        "patient_id": ["P_GHOST"],  # parent doesn't exist in DB
        "tissue_site": ["tumor"],
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(_meta_ns(
            seeded, level="specimen", metadata=tsv, allow_new=True, yes=True,
        ))
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "P_GHOST" in err
    assert "Register them first" in err


def test_insert_at_specimen_with_existing_parent(seeded: Path, tmp_path: Path):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({
        "specimen_id": ["S_NEW"],
        "patient_id": ["P1"],
        "tissue_site": ["tumor"],
    }))
    casetrack.cmd_add_metadata(_meta_ns(
        seeded, level="specimen", metadata=tsv, allow_new=True, yes=True,
    ))
    with _conn(seeded) as c:
        row = c.execute(
            "SELECT specimen_id, patient_id, tissue_site FROM specimens"
        ).fetchone()
    assert row == ("S_NEW", "P1", "tumor")


# ── Schema validation ────────────────────────────────────────────────────────


def test_unknown_column_rejected(seeded: Path, tmp_path: Path, capsys):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({
        "patient_id": ["P1"],
        "invented_col": ["?"],
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=tsv))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "invented_col" in err
    assert "not declared" in err


def test_tsv_without_key_col_rejected(seeded: Path, tmp_path: Path, capsys):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({"age": [55]}))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=tsv))
    assert excinfo.value.code == 1
    assert "patient_id" in capsys.readouterr().err


def test_tsv_with_only_key_col_rejected(seeded: Path, tmp_path: Path, capsys):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({"patient_id": ["P1"]}))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=tsv))
    assert excinfo.value.code == 1
    assert "no columns besides" in capsys.readouterr().err


# ── CHECK enforcement via SQLite ──────────────────────────────────────────────


def test_check_rejects_bad_enum(seeded: Path, tmp_path: Path, capsys):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({
        "patient_id": ["P1"],
        "sex": ["robot"],  # not in the enum
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=tsv))
    assert excinfo.value.code == 1
    assert "aborted" in capsys.readouterr().err


# ── Provenance ────────────────────────────────────────────────────────────────


def test_provenance_entry_shape(seeded: Path, tmp_path: Path):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({
        "patient_id": ["P1", "P99"], "age": [55, 72],
    }))
    casetrack.cmd_add_metadata(_meta_ns(
        seeded, level="patient", metadata=tsv, allow_new=True, yes=True,
    ))
    entries = [
        json.loads(ln)
        for ln in (seeded / "provenance.jsonl").read_text().splitlines()
    ]
    am = next(e for e in entries if e["action"] == "add_metadata")
    assert am["level"] == "patient"
    assert am["columns"] == ["age"]
    assert am["rows_updated"] == 1
    assert am["rows_inserted"] == 1
    assert "metadata_checksum" in am and len(am["metadata_checksum"]) == 32
    assert am["transaction_id"].startswith("txn_")


def test_failed_add_metadata_leaves_no_provenance(seeded: Path, tmp_path: Path):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({
        "patient_id": ["P_GHOST"], "age": [55],
    }))
    with pytest.raises(SystemExit):
        casetrack.cmd_add_metadata(_meta_ns(seeded, level="patient", metadata=tsv))
    entries = [
        json.loads(ln)
        for ln in (seeded / "provenance.jsonl").read_text().splitlines()
    ]
    assert all(e["action"] != "add_metadata" for e in entries)


# ── --allow-new safety double-flag ────────────────────────────────────────────


def test_allow_new_requires_yes(seeded: Path, tmp_path: Path, capsys):
    tsv = _write_tsv(tmp_path / "t.tsv", pd.DataFrame({
        "patient_id": ["P1"], "age": [55],
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(_meta_ns(
            seeded, level="patient", metadata=tsv, allow_new=True, yes=False,
        ))
    assert excinfo.value.code == 1
    assert "requires --yes" in capsys.readouterr().err
