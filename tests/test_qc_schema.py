"""Tests for casetrack_qc.schema — DDL, qc_status migrations, TOML parsing.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

import casetrack
from casetrack_qc import schema as qc_schema
from casetrack_qc.schema import (
    DEFAULT_CONSENT_ENUM,
    DEFAULT_QC_KIND_SCOPES,
    DEFAULT_QC_KINDS,
    ensure_qc_schema,
    parse_qc_config,
    qc_schema_exists,
    write_qc_toml_block,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _init_project(tmp_path: Path) -> Path:
    """Fresh v0.4 project — init already applies the QC schema."""
    proj = tmp_path / "proj"
    ns = argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    )
    casetrack.cmd_init(ns)
    return proj


# ── DDL ───────────────────────────────────────────────────────────────────────


def test_init_creates_qc_events_table(tmp_path: Path):
    proj = _init_project(tmp_path)
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "qc_events" in names
    finally:
        conn.close()


def test_init_creates_qc_events_indexes(tmp_path: Path):
    proj = _init_project(tmp_path)
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_qc_events_entity" in names
        assert "idx_qc_events_active" in names
        assert "idx_qc_events_kind" in names
    finally:
        conn.close()


def test_init_adds_qc_status_columns(tmp_path: Path):
    proj = _init_project(tmp_path)
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        for table in ("patients", "specimens", "assays"):
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
            assert "qc_status" in cols, f"{table} missing qc_status"
    finally:
        conn.close()


def test_init_adds_consent_columns_on_patients(tmp_path: Path):
    proj = _init_project(tmp_path)
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("patients")').fetchall()}
        for required in ("consent_status", "consent_date", "withdrawal_date"):
            assert required in cols
    finally:
        conn.close()


def test_qc_events_check_kind_rejects_unknown(tmp_path: Path):
    """The DDL CHECK constraint should reject a bogus kind."""
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO qc_events "
                "(level, entity_id, kind, reason, source, created_at, "
                "created_by, transaction_id) "
                "VALUES ('assay', 'A1', 'not_a_real_kind', 'r', 'manual', "
                "'2026-04-17T00:00:00', 'me', 'txn_x')"
            )
    finally:
        conn.close()


def test_qc_status_check_rejects_consent_revoked_on_specimen(tmp_path: Path):
    """specimens.qc_status CHECK must exclude consent_revoked (patient-only)."""
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        conn.execute(
            "INSERT INTO patients (patient_id) VALUES ('P1')"
        )
        conn.execute(
            "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
            "VALUES ('S1', 'P1', 'tumor')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE specimens SET qc_status='consent_revoked' WHERE specimen_id='S1'"
            )
    finally:
        conn.close()


def test_patient_consent_status_default_is_consented(tmp_path: Path):
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        conn.execute("INSERT INTO patients (patient_id) VALUES ('P1')")
        conn.commit()
        (status,) = conn.execute(
            "SELECT consent_status FROM patients WHERE patient_id='P1'"
        ).fetchone()
        assert status == "consented"
    finally:
        conn.close()


# ── ensure_qc_schema idempotency ──────────────────────────────────────────────


def test_ensure_qc_schema_is_idempotent(tmp_path: Path):
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        # Already in place from init — second call should add nothing.
        with casetrack.begin_immediate(conn):
            executed = ensure_qc_schema(conn)
        assert executed == []
        assert qc_schema_exists(conn) is True
    finally:
        conn.close()


def test_qc_schema_exists_false_before_migrate(tmp_path: Path):
    """A hand-rolled v0.3-style DB (no QC objects) returns False."""
    db = tmp_path / "old.db"
    conn = casetrack.open_project_db(db)
    try:
        # Minimal three-level schema, no QC.
        conn.executescript(
            "CREATE TABLE patients (patient_id TEXT PRIMARY KEY);"
            "CREATE TABLE specimens (specimen_id TEXT PRIMARY KEY, "
            "  patient_id TEXT);"
            "CREATE TABLE assays (assay_id TEXT PRIMARY KEY, "
            "  specimen_id TEXT);"
        )
        conn.commit()
        assert qc_schema_exists(conn) is False
        with casetrack.begin_immediate(conn):
            executed = ensure_qc_schema(conn)
        # Should have created qc_events + indexes + 3 qc_status + 3 consent.
        assert any("CREATE TABLE qc_events" in s for s in executed)
        assert qc_schema_exists(conn) is True
    finally:
        conn.close()


# ── TOML parsing ──────────────────────────────────────────────────────────────


def test_parse_qc_config_falls_back_to_defaults():
    cfg = parse_qc_config({})
    assert "qc_fail" in cfg["kinds"]
    assert "library_prep_failed" in cfg["kinds"]
    assert cfg["default_source"] == "manual"
    assert cfg["kind_scopes"]["consent_revoked"] == ["patient"]


def test_parse_qc_config_honours_overrides():
    schema = {
        "qc": {
            "kinds": ["qc_fail", "custom_kind"],
            "default_source": "slurm",
            "default_exclude": ["fail"],
            "kind_scopes": {"custom_kind": ["assay"]},
        }
    }
    cfg = parse_qc_config(schema)
    assert cfg["kinds"] == ["qc_fail", "custom_kind"]
    assert cfg["default_source"] == "slurm"
    assert cfg["kind_scopes"] == {"custom_kind": ["assay"]}


def test_write_qc_toml_block_appends_once(tmp_path: Path):
    toml = tmp_path / "casetrack.toml"
    toml.write_text('[project]\nname = "x"\nschema_v = 1\n')
    assert write_qc_toml_block(toml) is True
    text1 = toml.read_text()
    assert "[qc]" in text1
    assert "[qc.kind_scopes]" in text1

    # Second call must not append a duplicate block.
    assert write_qc_toml_block(toml) is False
    text2 = toml.read_text()
    assert text2 == text1


def test_init_writes_qc_toml_block(tmp_path: Path):
    proj = _init_project(tmp_path)
    toml = (proj / "casetrack.toml").read_text()
    assert "[qc]" in toml
    assert "library_prep_failed" in toml


def test_parse_qc_config_from_initialized_project(tmp_path: Path):
    proj = _init_project(tmp_path)
    schema = casetrack.load_schema(proj / "casetrack.toml")
    cfg = parse_qc_config(schema)
    assert set(DEFAULT_QC_KINDS) <= set(cfg["kinds"])
    assert cfg["kind_scopes"]["consent_revoked"] == ["patient"]
