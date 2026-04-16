"""Tests for casetrack.toml loading + validation (v0.3 / proposal 0001 §6).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

from pathlib import Path

import pytest

import casetrack


def _write_toml(path: Path, contents: str) -> Path:
    path.write_text(contents)
    return path


# ── Happy path ────────────────────────────────────────────────────────────────


def test_blank_template_round_trips(tmp_path: Path):
    """Every shipped template must produce a schema that `load_schema` accepts."""
    for name in casetrack.TEMPLATES:
        toml_path = tmp_path / f"{name}.toml"
        toml_path.write_text(casetrack.TEMPLATES[name]("demo"))
        schema = casetrack.load_schema(toml_path)
        assert set(schema["levels"]) == set(casetrack.LEVEL_ORDER)
        assert schema["project"]["name"] == "demo"
        assert schema["project"]["schema_v"] == 1


def test_hgsoc_template_has_expected_enums(tmp_path: Path):
    toml_path = tmp_path / "schema.toml"
    toml_path.write_text(casetrack.TEMPLATES["hgsoc"]("proj"))
    schema = casetrack.load_schema(toml_path)

    assay_cols = schema["levels"]["assay"]["columns"]
    assert "enum" in assay_cols["assay_type"]
    assert set(assay_cols["assay_type"]["enum"]) >= {"scRNA", "WGS", "ONT"}


def test_schema_declares_expected_parents(tmp_path: Path):
    toml_path = tmp_path / "schema.toml"
    toml_path.write_text(casetrack.TEMPLATES["blank"]("proj"))
    schema = casetrack.load_schema(toml_path)
    assert schema["levels"]["patient"].get("parent") is None
    assert schema["levels"]["specimen"]["parent"] == "patient"
    assert schema["levels"]["assay"]["parent"] == "specimen"


# ── Error paths ───────────────────────────────────────────────────────────────


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(casetrack.SchemaError, match="not found"):
        casetrack.load_schema(tmp_path / "nope.toml")


def test_invalid_toml_syntax_raises(tmp_path: Path):
    p = _write_toml(tmp_path / "bad.toml", "[project\nname = broken")
    with pytest.raises(casetrack.SchemaError, match="failed to parse"):
        casetrack.load_schema(p)


def test_missing_project_section_raises(tmp_path: Path):
    p = _write_toml(tmp_path / "no_project.toml", "[levels.patient]\nkey = 'patient_id'\n")
    with pytest.raises(casetrack.SchemaError, match=r"\[project\]"):
        casetrack.load_schema(p)


def test_missing_level_raises(tmp_path: Path):
    p = _write_toml(
        tmp_path / "no_assay.toml",
        '[project]\nname = "x"\nschema_v = 1\n'
        '[levels.patient]\nkey = "patient_id"\n[levels.patient.columns]\n'
        'patient_id = { type = "TEXT", required = true, unique = true }\n'
        '[levels.specimen]\nkey = "specimen_id"\nparent = "patient"\nparent_key = "patient_id"\n'
        '[levels.specimen.columns]\n'
        'specimen_id = { type = "TEXT", required = true, unique = true }\n'
        'patient_id = { type = "TEXT", required = true }\n',
    )
    with pytest.raises(casetrack.SchemaError, match=r"\[levels\.assay\]"):
        casetrack.load_schema(p)


def test_wrong_parent_rejected(tmp_path: Path):
    """specimen.parent must be 'patient', not anything else."""
    toml = casetrack.TEMPLATES["blank"]("proj")
    # Mutate the parent relationship.
    toml = toml.replace('parent     = "patient"', 'parent     = "assay"', 1)
    p = _write_toml(tmp_path / "bad_parent.toml", toml)
    with pytest.raises(casetrack.SchemaError, match="parent mismatch"):
        casetrack.load_schema(p)


def test_invalid_column_type_rejected(tmp_path: Path):
    toml = casetrack.TEMPLATES["blank"]("proj")
    toml = toml.replace('{ type = "TEXT", required = true, unique = true }',
                        '{ type = "STRING", required = true, unique = true }', 1)
    p = _write_toml(tmp_path / "bad_type.toml", toml)
    with pytest.raises(casetrack.SchemaError, match="invalid type"):
        casetrack.load_schema(p)


def test_key_not_in_columns_rejected(tmp_path: Path):
    toml = (
        '[project]\nname = "x"\nschema_v = 1\n'
        '[levels.patient]\nkey = "patient_id"\n'
        '[levels.patient.columns]\n'
        'other_col = { type = "TEXT" }\n'
    )
    p = _write_toml(tmp_path / "no_key_col.toml", toml)
    with pytest.raises(casetrack.SchemaError, match="declared key"):
        casetrack.load_schema(p)


def test_enum_must_be_list_of_strings(tmp_path: Path):
    toml = casetrack.TEMPLATES["hgsoc"]("proj")
    toml = toml.replace('enum = ["F", "M", "intersex", "unknown"]',
                        'enum = [1, 2, 3]', 1)
    p = _write_toml(tmp_path / "bad_enum.toml", toml)
    with pytest.raises(casetrack.SchemaError, match="list of strings"):
        casetrack.load_schema(p)
