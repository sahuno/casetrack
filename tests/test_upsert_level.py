# tests/test_upsert_level.py
"""Unit tests for the shared _upsert_level engine (proposal 0012 §6.6)."""
import sqlite3
import pandas as pd
import pytest
import casetrack


def _schema():
    return {
        "project": {"schema_v": 1},
        "levels": {
            "patient":  {"key": "patient_id",
                         "columns": {"patient_id": {"type": "TEXT"}, "cohort": {"type": "TEXT"}}},
            "specimen": {"key": "specimen_id", "parent": "patient", "parent_key": "patient_id",
                         "columns": {"specimen_id": {"type": "TEXT"}, "patient_id": {"type": "TEXT"},
                                     "tissue_site": {"type": "TEXT"}}},
            "assay":    {"key": "assay_id", "parent": "specimen", "parent_key": "specimen_id",
                         "columns": {"assay_id": {"type": "TEXT"}, "specimen_id": {"type": "TEXT"},
                                     "assay_type": {"type": "TEXT"}}},
        },
    }


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE patients(patient_id TEXT PRIMARY KEY, cohort TEXT);"
        "CREATE TABLE specimens(specimen_id TEXT PRIMARY KEY, patient_id TEXT, tissue_site TEXT);"
        "CREATE TABLE assays(assay_id TEXT PRIMARY KEY, specimen_id TEXT, assay_type TEXT);"
    )
    return conn


def test_upsert_inserts_new_rows():
    conn = _db()
    frame = pd.DataFrame({"patient_id": ["P1", "P2"], "cohort": ["c", "c"]})
    with casetrack.begin_immediate(conn):
        res = casetrack._upsert_level(conn, level="patient", frame=frame,
                                      schema=_schema(), allow_new=True, overwrite=False)
    assert res["inserted"] == 2 and res["updated"] == 0
    assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 2


def test_upsert_key_only_frame_inserts():
    """A key-only frame (no attribute columns) still inserts — register-cohort needs this."""
    conn = _db()
    frame = pd.DataFrame({"patient_id": ["P1"]})
    with casetrack.begin_immediate(conn):
        res = casetrack._upsert_level(conn, level="patient", frame=frame,
                                      schema=_schema(), allow_new=True, overwrite=False)
    assert res["inserted"] == 1
    assert conn.execute("SELECT patient_id FROM patients").fetchone()[0] == "P1"


def test_upsert_fill_only_vs_overwrite():
    conn = _db()
    conn.execute("INSERT INTO patients(patient_id, cohort) VALUES ('P1','old')")
    conn.commit()
    frame = pd.DataFrame({"patient_id": ["P1"], "cohort": ["new"]})
    with casetrack.begin_immediate(conn):
        casetrack._upsert_level(conn, level="patient", frame=frame, schema=_schema(),
                                allow_new=True, overwrite=False)  # fill-only: existing non-null kept
    assert conn.execute("SELECT cohort FROM patients").fetchone()[0] == "old"
    with casetrack.begin_immediate(conn):
        casetrack._upsert_level(conn, level="patient", frame=frame, schema=_schema(),
                                allow_new=True, overwrite=True)
    assert conn.execute("SELECT cohort FROM patients").fetchone()[0] == "new"


def test_upsert_missing_parent_raises_routing():
    conn = _db()
    frame = pd.DataFrame({"specimen_id": ["S1"], "patient_id": ["GHOST"], "tissue_site": ["t"]})
    with pytest.raises(casetrack._MetadataRouting):
        with casetrack.begin_immediate(conn):
            casetrack._upsert_level(conn, level="specimen", frame=frame, schema=_schema(),
                                    allow_new=True, overwrite=False)


def test_upsert_undeclared_column_raises():
    conn = _db()
    frame = pd.DataFrame({"patient_id": ["P1"], "bogus": ["x"]})
    with pytest.raises(ValueError):
        with casetrack.begin_immediate(conn):
            casetrack._upsert_level(conn, level="patient", frame=frame, schema=_schema(),
                                    allow_new=True, overwrite=False)
