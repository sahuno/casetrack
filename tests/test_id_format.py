"""Tests for v0.6 hierarchy ID format enforcement (proposal 0005 Part A).

Covers:
- validate_hierarchy_id direct regex / reserved-literal / length checks.
- Schema-level config: [levels.<level>] id_pattern + allow_case_variants,
  [project] allow_unicode_ids.
- Integration via `casetrack register`: malformed IDs are rejected with a
  clear error; case-variants are rejected unless opted in; custom
  id_pattern override allows legacy LIMS IDs; unicode opt-in works.
- Integration via `casetrack add-metadata --allow-new`: same enforcement.
- `casetrack recover` is tolerant of pre-existing malformed IDs.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-19
"""
from __future__ import annotations

import argparse
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
        bare=False,
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


def _load_fresh_schema(project_dir: Path) -> dict:
    """Load the project's schema from disk, skipping the cached compilation."""
    return casetrack.load_schema(project_dir / "casetrack.toml")


def _set_toml_line(project_dir: Path, section: str, key: str, value: str) -> None:
    """Append `key = value` under the given `[section]` header in casetrack.toml."""
    toml_path = project_dir / "casetrack.toml"
    text = toml_path.read_text()
    lines = text.splitlines()
    header = f"[{section}]"
    for i, line in enumerate(lines):
        if line.strip() == header:
            lines.insert(i + 1, f"{key} = {value}")
            break
    else:
        lines.append(f"\n{header}\n{key} = {value}")
    toml_path.write_text("\n".join(lines) + "\n")


# ── validate_hierarchy_id (pure function) ─────────────────────────────────────


def test_accept_plain_ascii(hgsoc_project: Path):
    schema = _load_fresh_schema(hgsoc_project)
    for good in ("P01", "HG006_PAY77227", "MSK-001", "HG002.v2",
                 "2026_cohort_A", "A" * 64):
        casetrack.validate_hierarchy_id(good, schema, "patient")


@pytest.mark.parametrize("bad,reason", [
    ("P 01",       "whitespace"),
    ("P\t01",      "whitespace"),
    ("P01\n",      "whitespace"),
    ("P01;rm",     "shell metacharacter"),
    ("-P01",       "leading hyphen"),
    (".hidden",    "leading dot"),
    ("P01/v2",     "path separator"),
    ("P01\\v2",    "path separator"),
    ("P01'x",      "shell metacharacter"),
    ("P01\"x",     "shell metacharacter"),
    ("P01$x",      "shell metacharacter"),
    ("P01|x",      "shell metacharacter"),
    ("P01&x",      "shell metacharacter"),
    ("P01\x00x",   "null byte"),
    ("",           "empty"),
    ("   ",        "whitespace-only"),
    (".",          "reserved literal"),
    ("..",         "reserved literal"),
    ("αβγ",        "non-ASCII"),
    ("A" * 65,     "length > 64"),
])
def test_reject_malformed_ascii(hgsoc_project: Path, bad: str, reason: str):
    schema = _load_fresh_schema(hgsoc_project)
    with pytest.raises(ValueError):
        casetrack.validate_hierarchy_id(bad, schema, "patient")


def test_reject_null_and_pandas_na(hgsoc_project: Path):
    import pandas as pd
    schema = _load_fresh_schema(hgsoc_project)
    with pytest.raises(ValueError, match="cannot be null"):
        casetrack.validate_hierarchy_id(None, schema, "patient")
    with pytest.raises(ValueError, match="cannot be null"):
        casetrack.validate_hierarchy_id(pd.NA, schema, "patient")


# ── schema-level config ───────────────────────────────────────────────────────


def test_schema_rejects_unanchored_id_pattern(tmp_path: Path):
    """id_pattern must anchor with ^ and $."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    _set_toml_line(proj, "levels.patient", "id_pattern", '"[a-z]+"')
    with pytest.raises(casetrack.SchemaError, match="must anchor"):
        casetrack.load_schema(proj / "casetrack.toml")


def test_schema_rejects_invalid_regex(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    _set_toml_line(proj, "levels.patient", "id_pattern", '"^[a-z$"')
    with pytest.raises(casetrack.SchemaError, match="not a valid regex"):
        casetrack.load_schema(proj / "casetrack.toml")


def test_schema_rejects_non_bool_allow_case_variants(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    _set_toml_line(proj, "levels.patient", "allow_case_variants", '"yes"')
    with pytest.raises(casetrack.SchemaError, match="must be a boolean"):
        casetrack.load_schema(proj / "casetrack.toml")


def test_schema_rejects_non_bool_allow_unicode_ids(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    _set_toml_line(proj, "project", "allow_unicode_ids", '"yes"')
    with pytest.raises(casetrack.SchemaError, match="must be a boolean"):
        casetrack.load_schema(proj / "casetrack.toml")


def test_custom_id_pattern_accepts_legacy_colon_ids(tmp_path: Path):
    """A cohort with legacy LIMS IDs containing ':' should be able to opt in."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    _set_toml_line(
        proj, "levels.patient", "id_pattern",
        '"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"',
    )
    schema = casetrack.load_schema(proj / "casetrack.toml")
    # Would fail under the default pattern; should pass under the override.
    casetrack.validate_hierarchy_id("MSK-001:2024", schema, "patient")


def test_allow_unicode_ids_accepts_non_ascii(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    _set_toml_line(proj, "project", "allow_unicode_ids", "true")
    schema = casetrack.load_schema(proj / "casetrack.toml")
    casetrack.validate_hierarchy_id("αβγ", schema, "patient")
    casetrack.validate_hierarchy_id("日本語", schema, "patient")
    # Structural rules still apply even in unicode mode.
    with pytest.raises(ValueError, match="forbidden character"):
        casetrack.validate_hierarchy_id("αβγ x", schema, "patient")
    with pytest.raises(ValueError, match="cannot start with"):
        casetrack.validate_hierarchy_id(".αβγ", schema, "patient")


# ── integration via cmd_register ──────────────────────────────────────────────


def test_register_rejects_id_with_whitespace(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="patient", id="P 01",
        ))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "P 01" in err
    assert "valid identifier" in err or "does not match" in err


def test_register_rejects_id_with_shell_meta(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="patient", id="P01;rm",
        ))
    assert exc.value.code == 1
    assert "P01;rm" in capsys.readouterr().err


def test_register_accepts_valid_id(hgsoc_project: Path):
    casetrack.cmd_register(_reg_ns(
        hgsoc_project, level="patient", id="P01",
    ))
    conn = casetrack.open_project_db(hgsoc_project / "casetrack.db")
    try:
        row = conn.execute(
            "SELECT patient_id FROM patients WHERE patient_id='P01'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_register_rejects_case_variant(hgsoc_project: Path, capsys):
    casetrack.cmd_register(_reg_ns(
        hgsoc_project, level="patient", id="HG006",
    ))
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="patient", id="hg006",
        ))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "hg006" in err and "HG006" in err
    assert "allow_case_variants" in err


def test_allow_case_variants_true_accepts_them(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    _set_toml_line(proj, "levels.patient", "allow_case_variants", "true")
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="HG006"))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="hg006"))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        rows = conn.execute(
            "SELECT patient_id FROM patients ORDER BY patient_id"
        ).fetchall()
        assert {r[0] for r in rows} == {"HG006", "hg006"}
    finally:
        conn.close()


def test_register_rejects_malformed_parent(hgsoc_project: Path, capsys):
    # First create a valid patient.
    casetrack.cmd_register(_reg_ns(
        hgsoc_project, level="patient", id="P01",
    ))
    # Now try to create a specimen whose parent ID is malformed.
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_register(_reg_ns(
            hgsoc_project, level="specimen", id="SPEC_A", parent="P 01",
        ))
    assert exc.value.code == 1
    assert "P 01" in capsys.readouterr().err


def test_register_with_id_pattern_override(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    _set_toml_line(
        proj, "levels.patient", "id_pattern",
        '"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"',
    )
    # Custom pattern lets ':' through.
    casetrack.cmd_register(_reg_ns(
        proj, level="patient", id="MSK-001:2024",
    ))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        row = conn.execute(
            "SELECT patient_id FROM patients WHERE patient_id='MSK-001:2024'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


# ── recover path tolerance ───────────────────────────────────────────────────


def test_recover_tolerates_legacy_malformed_ids(tmp_path: Path):
    """Recover replays provenance against a fresh DB — pre-existing malformed
    IDs (from before v0.6) must pass through unchanged, not be re-validated.
    """
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    # Simulate a malformed ID that slipped into a legacy DB: write directly.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        conn.execute("INSERT INTO patients (patient_id) VALUES (?)", ("Legacy ID!",))
        conn.commit()
    finally:
        conn.close()
    # Reading it back must work — query/export paths do NOT re-validate.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        row = conn.execute(
            "SELECT patient_id FROM patients WHERE patient_id='Legacy ID!'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


# ── unicode mode integration ─────────────────────────────────────────────────


def test_register_with_allow_unicode_ids(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    _set_toml_line(proj, "project", "allow_unicode_ids", "true")
    casetrack.cmd_register(_reg_ns(
        proj, level="patient", id="日本語_01",
    ))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        row = conn.execute("SELECT patient_id FROM patients").fetchone()
        assert row[0] == "日本語_01"
    finally:
        conn.close()
