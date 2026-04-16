"""Tests for `casetrack recover --project-dir` (proposal 0001 §9.4).

Builds a project via normal commands, captures the resulting DB state,
deletes the DB, recovers from provenance.jsonl, and verifies that the
reconstructed state matches.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import casetrack


def _init_ns(project_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc", project_name=None, force=False,
    )


def _reg_ns(project_dir: Path, *, level: str, id: str,
            parent: str | None = None, meta: str | None = None,
            allow_new_parent: bool = False, yes: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir), level=level, id=id, parent=parent,
        meta=meta, allow_new_parent=allow_new_parent, yes=yes,
    )


def _append_ns(project_dir: Path, results: Path, analysis: str) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), results=str(results),
        key="sample_id", analysis=analysis, level=None, col_type=None,
        overwrite=False, allow_new=False, yes=False,
    )


def _recover_ns(project_dir: Path, *, from_: str | None = None,
                force: bool = True, permit_partial: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir), from_=from_,
        force=force, permit_partial=permit_partial,
    )


def _db_snapshot(db_path: Path) -> dict:
    """Capture table rows (ignoring column order) for a state-equality check."""
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            "patients": sorted(conn.execute(
                "SELECT patient_id, age, sex, brca_status FROM patients "
                "ORDER BY patient_id"
            ).fetchall()),
            "specimens": sorted(conn.execute(
                "SELECT specimen_id, patient_id, tissue_site FROM specimens "
                "ORDER BY specimen_id"
            ).fetchall()),
            "assays": sorted(conn.execute(
                "SELECT assay_id, specimen_id, assay_type FROM assays "
                "ORDER BY assay_id"
            ).fetchall()),
        }
    finally:
        conn.close()


# ── Register-only project round-trips exactly ─────────────────────────────────


def test_recover_register_only_project(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P1",
                                    meta="age=55,sex=F,brca_status=brca1"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S1", parent="P1",
                                    meta="tissue_site=tumor"))
    casetrack.cmd_register(_reg_ns(proj, level="assay", id="A1", parent="S1",
                                    meta="assay_type=WGS"))

    before = _db_snapshot(proj / "casetrack.db")

    # Wipe the DB and recover.
    casetrack.cmd_recover_project(_recover_ns(proj))

    after = _db_snapshot(proj / "casetrack.db")
    assert before == after


# ── Append is replayed when the source TSV is still present ──────────────────


def test_recover_replays_append_from_source_tsv(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P1"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S1", parent="P1",
                                    meta="tissue_site=tumor"))
    casetrack.cmd_register(_reg_ns(proj, level="assay", id="A1", parent="S1",
                                    meta="assay_type=WGS"))

    results = proj / "modkit.tsv"
    pd.DataFrame({"assay_id": ["A1"], "mean_meth": [0.72]}).to_csv(
        results, sep="\t", index=False,
    )
    casetrack.cmd_append(_append_ns(proj, results, "modkit"))

    casetrack.cmd_recover_project(_recover_ns(proj))

    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        (mean,) = conn.execute(
            "SELECT mean_meth FROM assays WHERE assay_id='A1'"
        ).fetchone()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(assays)").fetchall()}
    finally:
        conn.close()
    assert mean == 0.72
    assert "modkit_done" in cols


# ── Missing source file → partial recovery ────────────────────────────────────


def test_recover_partial_when_source_missing(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P1"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S1", parent="P1",
                                    meta="tissue_site=tumor"))
    casetrack.cmd_register(_reg_ns(proj, level="assay", id="A1", parent="S1",
                                    meta="assay_type=WGS"))

    results = proj / "modkit.tsv"
    pd.DataFrame({"assay_id": ["A1"], "mean_meth": [0.72]}).to_csv(
        results, sep="\t", index=False,
    )
    casetrack.cmd_append(_append_ns(proj, results, "modkit"))
    results.unlink()  # simulate source being deleted

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_recover_project(_recover_ns(proj))
    assert excinfo.value.code == 2  # partial recovery exits 2 without --permit-partial
    err = capsys.readouterr().err
    assert "source file missing" in err


def test_recover_permit_partial(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P1"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S1", parent="P1",
                                    meta="tissue_site=tumor"))
    casetrack.cmd_register(_reg_ns(proj, level="assay", id="A1", parent="S1",
                                    meta="assay_type=WGS"))

    results = proj / "modkit.tsv"
    pd.DataFrame({"assay_id": ["A1"], "mean_meth": [0.72]}).to_csv(
        results, sep="\t", index=False,
    )
    casetrack.cmd_append(_append_ns(proj, results, "modkit"))
    results.unlink()

    casetrack.cmd_recover_project(_recover_ns(proj, permit_partial=True))
    # Exits 0; the register rows are back, the append is not.
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        n_assays = conn.execute("SELECT COUNT(*) FROM assays").fetchone()[0]
        (mean,) = conn.execute(
            "SELECT mean_meth FROM assays WHERE assay_id='A1'"
        ).fetchone() if "mean_meth" in {r[1] for r in conn.execute(
            "PRAGMA table_info(assays)"
        ).fetchall()} else (None,)
    finally:
        conn.close()
    assert n_assays == 1
    assert mean is None  # append couldn't replay


# ── Checksum mismatch ─────────────────────────────────────────────────────────


def test_recover_checksum_mismatch(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P1"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S1", parent="P1",
                                    meta="tissue_site=tumor"))
    casetrack.cmd_register(_reg_ns(proj, level="assay", id="A1", parent="S1",
                                    meta="assay_type=WGS"))

    results = proj / "modkit.tsv"
    pd.DataFrame({"assay_id": ["A1"], "mean_meth": [0.72]}).to_csv(
        results, sep="\t", index=False,
    )
    casetrack.cmd_append(_append_ns(proj, results, "modkit"))
    # Tamper with the source after provenance was written.
    pd.DataFrame({"assay_id": ["A1"], "mean_meth": [9.99]}).to_csv(
        results, sep="\t", index=False,
    )

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_recover_project(_recover_ns(proj))
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "checksum mismatch" in err


# ── --force / missing project handling ────────────────────────────────────────


def test_recover_refuses_overwrite_without_force(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_recover_project(_recover_ns(proj, force=False))
    assert excinfo.value.code == 1
    assert "already exists" in capsys.readouterr().err


def test_recover_missing_project_dir(tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_recover_project(_recover_ns(tmp_path / "ghost"))
    assert excinfo.value.code == 1
    assert "project directory not found" in capsys.readouterr().err


def test_recover_missing_provenance(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    (proj / "provenance.jsonl").unlink()

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_recover_project(_recover_ns(proj))
    assert excinfo.value.code == 1
    assert "provenance log not found" in capsys.readouterr().err


# ── --from points at an alternative log ──────────────────────────────────────


def test_recover_from_alternative_log(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P1"))

    backup = tmp_path / "snapshot.jsonl"
    backup.write_text((proj / "provenance.jsonl").read_text())
    (proj / "provenance.jsonl").unlink()

    casetrack.cmd_recover_project(_recover_ns(proj, from_=str(backup)))

    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        patients = [r[0] for r in conn.execute(
            "SELECT patient_id FROM patients"
        ).fetchall()]
    finally:
        conn.close()
    assert patients == ["P1"]
