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
    with pytest.raises(ValueError, match="conflicting attribute"):
        casetrack._validate_samplesheet(df, SCHEMA)


def test_validate_missing_key_column():
    """A sheet missing an entire key column must raise a clean ValueError, not KeyError."""
    df = _full_sheet().drop(columns=["assay_id"])
    with pytest.raises(ValueError, match="assay_id|key"):
        casetrack._validate_samplesheet(df, SCHEMA)


# ---------------------------------------------------------------------------
# cmd_register_cohort integration tests (Task 4)
# ---------------------------------------------------------------------------

def _init_project(tmp_path):
    """Init an hgsoc project and return its directory path."""
    proj = tmp_path / "proj"
    ns = argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name=None, force=False,
    )
    casetrack.cmd_init(ns)
    return proj


def _write_sheet(path):
    """Write a minimal valid hgsoc sample sheet (3 assays, 2 patients)."""
    path.write_text(
        "patient_id\ttissue_site\tspecimen_id\tassay_type\tassay_id\n"
        "P1\ttumor\tP1-T\tONT\tP1-T-ONT\n"
        "P1\tnormal\tP1-N\tONT\tP1-N-ONT\n"
        "P2\ttumor\tP2-T\tONT\tP2-T-ONT\n"
    )


def _ns(proj, sheet, **kw):
    base = dict(project_dir=str(proj), project=None, samplesheet=str(sheet),
                overwrite=False, dry_run=False, force_archived=False, yes=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_register_cohort_loads_all_levels(tmp_path):
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"
    _write_sheet(sheet)
    casetrack.cmd_register_cohort(_ns(proj, sheet))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM specimens").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM assays").fetchone()[0] == 3
    finally:
        conn.close()


def test_register_cohort_dry_run_writes_nothing(tmp_path):
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"
    _write_sheet(sheet)
    casetrack.cmd_register_cohort(_ns(proj, sheet, dry_run=True))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
    finally:
        conn.close()


def test_register_cohort_rerun_idempotent(tmp_path, capsys):
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"
    _write_sheet(sheet)
    casetrack.cmd_register_cohort(_ns(proj, sheet))
    capsys.readouterr()  # discard first-run output
    casetrack.cmd_register_cohort(_ns(proj, sheet))  # second run inserts 0
    out = capsys.readouterr().out
    assert "assays +0" in out
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM assays").fetchone()[0] == 3
    finally:
        conn.close()


def test_register_cohort_rolls_back_on_bad_sheet(tmp_path):
    """Blank assay_id fails validation → sys.exit(2) → nothing written."""
    proj = _init_project(tmp_path)
    sheet = tmp_path / "bad.tsv"
    sheet.write_text(
        "patient_id\ttissue_site\tspecimen_id\tassay_type\tassay_id\n"
        "P1\ttumor\tP1-T\tONT\t\n"  # blank assay_id → validation error
    )
    with pytest.raises(SystemExit):
        casetrack.cmd_register_cohort(_ns(proj, sheet))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
    finally:
        conn.close()


def test_register_cohort_rolls_back_on_mid_transaction_integrity_error(tmp_path, monkeypatch):
    """IntegrityError on the assay frame rolls back already-staged patients+specimens."""
    import sqlite3
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"
    _write_sheet(sheet)

    original_upsert = casetrack._upsert_level

    def failing_upsert(conn, *, level, frame, schema, allow_new, overwrite):
        if level == "assay":
            raise sqlite3.IntegrityError("forced error on assay frame")
        return original_upsert(conn, level=level, frame=frame, schema=schema,
                               allow_new=allow_new, overwrite=overwrite)

    monkeypatch.setattr(casetrack, "_upsert_level", failing_upsert)

    with pytest.raises(SystemExit) as exc_info:
        casetrack.cmd_register_cohort(_ns(proj, sheet))
    assert exc_info.value.code == 1

    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM specimens").fetchone()[0] == 0
    finally:
        conn.close()
