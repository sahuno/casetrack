"""Tests for `casetrack register` (v0.3 / proposal 0001 §7.2 & §19 Q2).

Covers happy-path insertion at each level, strict FK enforcement, the
--allow-new-parent --yes opt-in, --meta parsing + type coercion, CHECK
enforcement, and provenance wiring.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

import casetrack


# ── fixtures ──────────────────────────────────────────────────────────────────


def _init_ns(project_dir: Path, template: str = "hgsoc") -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        samples=None,
        key="sample_id",
        metadata=None,
        cols=None,
        from_template=template,
        project_name=None,
        force=False,
    )


def _reg_ns(project_dir: Path, *, level: str, id: str, parent: str | None = None,
            meta: str | None = None, allow_new_parent: bool = False,
            yes: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir),
        level=level,
        id=id,
        parent=parent,
        meta=meta,
        allow_new_parent=allow_new_parent,
        yes=yes,
    )


@pytest.fixture
def hgsoc_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    return proj


def _conn(project_dir: Path) -> sqlite3.Connection:
    return casetrack.open_project_db(project_dir / "casetrack.db")


# ── _parse_meta_kv ────────────────────────────────────────────────────────────


def test_parse_meta_kv_basic():
    assert casetrack._parse_meta_kv("a=1,b=2") == {"a": "1", "b": "2"}


def test_parse_meta_kv_empty_returns_empty():
    assert casetrack._parse_meta_kv(None) == {}
    assert casetrack._parse_meta_kv("") == {}


def test_parse_meta_kv_rejects_malformed():
    with pytest.raises(ValueError, match="expected 'key=value'"):
        casetrack._parse_meta_kv("no_equals_sign")


def test_parse_meta_kv_trims_whitespace():
    assert casetrack._parse_meta_kv("  a = 1 , b=2 ") == {"a": "1", "b": "2"}


# ── _coerce_scalar ────────────────────────────────────────────────────────────


def test_coerce_integer():
    assert casetrack._coerce_scalar("42", "INTEGER", "age") == 42


def test_coerce_real():
    assert casetrack._coerce_scalar("0.75", "REAL", "purity") == 0.75


def test_coerce_boolean_truthy():
    for v in ("true", "1", "yes", "Y", "T"):
        assert casetrack._coerce_scalar(v, "BOOLEAN", "qc") == 1


def test_coerce_boolean_falsy():
    for v in ("false", "0", "no", "N", "F"):
        assert casetrack._coerce_scalar(v, "BOOLEAN", "qc") == 0


def test_coerce_bad_integer_raises():
    with pytest.raises(ValueError, match="expected INTEGER"):
        casetrack._coerce_scalar("not_a_number", "INTEGER", "age")


def test_coerce_bad_boolean_raises():
    with pytest.raises(ValueError, match="expected BOOLEAN"):
        casetrack._coerce_scalar("maybe", "BOOLEAN", "qc")


# ── Happy path ────────────────────────────────────────────────────────────────


def test_register_patient(hgsoc_project: Path):
    casetrack.cmd_register(_reg_ns(
        hgsoc_project, level="patient", id="P001",
        meta="age=55,sex=F,brca_status=brca1",
    ))
    with _conn(hgsoc_project) as c:
        row = c.execute("SELECT patient_id, age, sex, brca_status FROM patients").fetchone()
    assert row == ("P001", 55, "F", "brca1")


def test_register_specimen_under_existing_patient(hgsoc_project: Path):
    casetrack.cmd_register(_reg_ns(hgsoc_project, level="patient", id="P001"))
    casetrack.cmd_register(_reg_ns(
        hgsoc_project, level="specimen", id="S001", parent="P001",
        meta="tissue_site=tumor,timepoint=t0",
    ))
    with _conn(hgsoc_project) as c:
        row = c.execute(
            "SELECT specimen_id, patient_id, tissue_site, timepoint FROM specimens"
        ).fetchone()
    assert row == ("S001", "P001", "tumor", "t0")


def test_register_assay_under_existing_specimen(hgsoc_project: Path):
    casetrack.cmd_register(_reg_ns(hgsoc_project, level="patient", id="P001"))
    casetrack.cmd_register(_reg_ns(
        hgsoc_project, level="specimen", id="S001", parent="P001",
        meta="tissue_site=tumor",
    ))
    casetrack.cmd_register(_reg_ns(
        hgsoc_project, level="assay", id="A001", parent="S001",
        meta="assay_type=WGS,replicate=1,qc_pass=true",
    ))
    with _conn(hgsoc_project) as c:
        row = c.execute(
            "SELECT assay_id, specimen_id, assay_type, replicate, qc_pass FROM assays"
        ).fetchone()
    assert row == ("A001", "S001", "WGS", 1, 1)


# ── FK enforcement / exit-2 contract ──────────────────────────────────────────


def test_missing_parent_exits_two(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="specimen", id="S001", parent="PHANTOM",
        ))
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "--allow-new-parent" in err


def test_specimen_missing_parent_rolls_back(hgsoc_project: Path):
    """Exit-2 must leave zero rows behind — nothing in patients *or* specimens."""
    with pytest.raises(SystemExit):
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="specimen", id="S001", parent="PHANTOM",
        ))
    with _conn(hgsoc_project) as c:
        assert c.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM specimens").fetchone()[0] == 0


def test_allow_new_parent_requires_yes(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="specimen", id="S001", parent="P001",
            allow_new_parent=True, yes=False,
        ))
    assert excinfo.value.code == 1
    assert "requires --yes" in capsys.readouterr().err


def test_allow_new_parent_creates_stub(hgsoc_project: Path):
    casetrack.cmd_register(_reg_ns(
        hgsoc_project, level="specimen", id="S001", parent="P_NEW",
        meta="tissue_site=tumor",
        allow_new_parent=True, yes=True,
    ))
    with _conn(hgsoc_project) as c:
        patients = c.execute("SELECT patient_id, age FROM patients").fetchall()
        specimens = c.execute("SELECT specimen_id, patient_id FROM specimens").fetchall()
    assert patients == [("P_NEW", None)]  # metadata-free stub
    assert specimens == [("S001", "P_NEW")]


def test_allow_new_parent_rejected_at_assay_level(hgsoc_project: Path, capsys):
    """Creating a specimen stub inline would require inventing a patient_id."""
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="assay", id="A001", parent="S_GHOST",
            meta="assay_type=WGS",
            allow_new_parent=True, yes=True,
        ))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "not supported" in err
    assert "Register the specimen first" in err


# ── --level / --parent validation ─────────────────────────────────────────────


def test_patient_rejects_parent(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="patient", id="P001", parent="PX",
        ))
    assert excinfo.value.code == 1
    assert "does not take --parent" in capsys.readouterr().err


def test_specimen_requires_parent(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="specimen", id="S001",
        ))
    assert excinfo.value.code == 1
    assert "--parent is required" in capsys.readouterr().err


def test_meta_cannot_override_key(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="patient", id="P001",
            meta="patient_id=SNEAKY",
        ))
    assert excinfo.value.code == 1
    assert "use --id / --parent" in capsys.readouterr().err


def test_meta_cannot_override_parent_key(hgsoc_project: Path, capsys):
    casetrack.cmd_register(_reg_ns(hgsoc_project, level="patient", id="P001"))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="specimen", id="S001", parent="P001",
            meta="patient_id=SNEAKY,tissue_site=tumor",
        ))
    assert excinfo.value.code == 1
    assert "use --id / --parent" in capsys.readouterr().err


def test_meta_rejects_unknown_column(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="patient", id="P001",
            meta="invented_column=42",
        ))
    assert excinfo.value.code == 1
    assert "not declared in schema" in capsys.readouterr().err


# ── CHECK / UNIQUE enforcement via SQLite ─────────────────────────────────────


def test_check_constraint_rejects_bad_enum(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="patient", id="P001",
            meta="sex=robot",  # not in the enum
        ))
    assert excinfo.value.code == 1
    assert "register aborted" in capsys.readouterr().err


def test_duplicate_id_rejected(hgsoc_project: Path, capsys):
    casetrack.cmd_register(_reg_ns(hgsoc_project, level="patient", id="P001"))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(hgsoc_project, level="patient", id="P001"))
    assert excinfo.value.code == 1
    assert "aborted" in capsys.readouterr().err


# ── Provenance ────────────────────────────────────────────────────────────────


def test_provenance_entry_has_expected_shape(hgsoc_project: Path):
    casetrack.cmd_register(_reg_ns(
        hgsoc_project, level="patient", id="P001",
        meta="age=55,sex=F",
    ))
    lines = (hgsoc_project / "provenance.jsonl").read_text().splitlines()
    # Entry 0 is the init_project event; entry 1 should be our register.
    entries = [json.loads(ln) for ln in lines]
    reg = next(e for e in entries if e["action"] == "register")
    assert reg["level"] == "patient"
    assert reg["id"] == "P001"
    assert reg["parent"] is None
    assert reg["parent_created"] is False
    assert reg["meta"] == {"age": 55, "sex": "F"}
    assert reg["rows_affected"] == 1
    assert reg["transaction_id"].startswith("txn_")
    assert any("INSERT INTO" in s for s in reg["sql"])


def test_provenance_records_parent_stub_creation(hgsoc_project: Path):
    casetrack.cmd_register(_reg_ns(
        hgsoc_project, level="specimen", id="S001", parent="P_NEW",
        meta="tissue_site=tumor",
        allow_new_parent=True, yes=True,
    ))
    entries = [
        json.loads(ln)
        for ln in (hgsoc_project / "provenance.jsonl").read_text().splitlines()
    ]
    reg = next(e for e in entries if e["action"] == "register")
    assert reg["parent_created"] is True
    assert reg["rows_affected"] == 2  # parent stub + target row


def test_failed_register_does_not_log_provenance(hgsoc_project: Path):
    """Exit-2 on missing parent must not leave a provenance entry behind."""
    with pytest.raises(SystemExit):
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="specimen", id="S001", parent="PHANTOM",
        ))
    entries = [
        json.loads(ln)
        for ln in (hgsoc_project / "provenance.jsonl").read_text().splitlines()
    ]
    assert all(e["action"] != "register" for e in entries)


# ── Project-dir validation ────────────────────────────────────────────────────


def test_missing_project_dir_exits(tmp_path: Path, capsys):
    ghost = tmp_path / "ghost"
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(ghost, level="patient", id="P001"))
    assert excinfo.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_missing_toml_exits(tmp_path: Path, capsys):
    proj = tmp_path / "broken"
    proj.mkdir()
    (proj / "casetrack.db").touch()
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_register(_reg_ns(proj, level="patient", id="P001"))
    assert excinfo.value.code == 1
    assert "casetrack.toml" in capsys.readouterr().err
