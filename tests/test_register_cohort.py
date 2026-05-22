# tests/test_register_cohort.py
"""Unit + CLI tests for register-cohort (proposal 0012)."""
import argparse, copy, subprocess, sys
import pandas as pd
import pytest
import casetrack

SCHEMA = {
    "project": {"schema_v": 1},
    "levels": {
        "patient":  {"key": "patient_id",
                     "columns": {"patient_id": {"type": "TEXT"},
                                 "cohort": {"type": "TEXT", "required": True}}},
        "specimen": {"key": "specimen_id", "parent": "patient", "parent_key": "patient_id",
                     "columns": {"specimen_id": {"type": "TEXT"}, "patient_id": {"type": "TEXT"},
                                 "tissue_site": {"type": "TEXT", "required": True}}},
        "assay":    {"key": "assay_id", "parent": "specimen", "parent_key": "specimen_id",
                     "columns": {"assay_id": {"type": "TEXT"}, "specimen_id": {"type": "TEXT"},
                                 "assay_type": {"type": "TEXT", "required": True}}},
    },
}


def test_route_columns_by_level():
    routed = casetrack._route_samplesheet_columns(
        ["patient_id", "cohort", "specimen_id", "tissue_site", "assay_id", "assay_type"], SCHEMA)
    assert routed["patient"] == ["patient_id", "cohort"]
    assert routed["specimen"] == ["specimen_id", "patient_id", "tissue_site"]
    assert routed["assay"] == ["assay_id", "specimen_id", "assay_type"]


def test_route_columns_undeclared_raises():
    with pytest.raises(ValueError):
        casetrack._route_samplesheet_columns(["patient_id", "bogus"], SCHEMA)


def test_explode_dedups_parents():
    df = pd.DataFrame({
        "patient_id": ["P1", "P1", "P2"],
        "cohort": ["c", "c", "c"],
        "specimen_id": ["P1_T", "P1_N", "P2_T"],
        "tissue_site": ["tumor", "normal", "tumor"],
        "assay_id": ["P1_T_A", "P1_N_A", "P2_T_A"],
        "assay_type": ["ONT", "ONT", "ONT"],
    })
    frames = casetrack._explode_samplesheet(df, SCHEMA)
    assert len(frames["patient"]) == 2     # P1, P2
    assert len(frames["specimen"]) == 3
    assert len(frames["assay"]) == 3
    assert set(frames["specimen"].columns) == {"specimen_id", "patient_id", "tissue_site"}


def test_route_columns_ambiguous_schema_raises():
    bad_schema = copy.deepcopy(SCHEMA)
    bad_schema["levels"]["specimen"]["columns"]["cohort"] = {"type": "TEXT"}
    with pytest.raises(ValueError, match="ambiguous"):
        casetrack._route_samplesheet_columns(
            ["patient_id", "cohort", "specimen_id", "assay_id"], bad_schema)


# ---------------------------------------------------------------------------
# _validate_samplesheet tests
# ---------------------------------------------------------------------------

def _full_sheet():
    return pd.DataFrame({
        "patient_id": ["P1", "P2"], "cohort": ["c", "c"],
        "specimen_id": ["P1_T", "P2_T"], "tissue_site": ["tumor", "tumor"],
        "assay_id": ["P1_T_A", "P2_T_A"], "assay_type": ["ONT", "ONT"],
    })


def test_validate_ok():
    casetrack._validate_samplesheet(_full_sheet(), SCHEMA)  # no raise


def test_validate_missing_required_column():
    df = _full_sheet().drop(columns=["assay_type"])  # required attr missing
    with pytest.raises(ValueError, match="assay_type"):
        casetrack._validate_samplesheet(df, SCHEMA)


def test_validate_blank_key_breaks_chain():
    df = _full_sheet(); df.loc[0, "assay_id"] = ""
    with pytest.raises(ValueError, match="chain|empty|assay_id"):
        casetrack._validate_samplesheet(df, SCHEMA)


def test_validate_specimen_two_patients():
    df = _full_sheet(); df.loc[1, "specimen_id"] = "P1_T"; df.loc[1, "patient_id"] = "P2"
    with pytest.raises(ValueError, match="specimen|parent"):
        casetrack._validate_samplesheet(df, SCHEMA)


def test_validate_duplicate_assay():
    df = pd.concat([_full_sheet(), _full_sheet().iloc[[0]].assign(assay_type="WGS")])
    with pytest.raises(ValueError, match="assay_id|duplicate"):
        casetrack._validate_samplesheet(df, SCHEMA)


def test_validate_conflicting_attribute():
    df = _full_sheet(); df.loc[1, "patient_id"] = "P1"; df.loc[1, "cohort"] = "other"
    with pytest.raises(ValueError, match="conflict|cohort"):
        casetrack._validate_samplesheet(df, SCHEMA)


def test_validate_missing_key_column():
    """A sheet missing an entire key column must raise a clean ValueError, not KeyError."""
    df = _full_sheet().drop(columns=["assay_id"])
    with pytest.raises(ValueError, match="assay_id|key"):
        casetrack._validate_samplesheet(df, SCHEMA)
