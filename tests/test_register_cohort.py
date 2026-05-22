# tests/test_register_cohort.py
"""Unit + CLI tests for register-cohort (proposal 0012)."""
import argparse, copy, json, subprocess, sys
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


# ---------------------------------------------------------------------------
# CLI end-to-end tests (Task 5) — exercise the argparse + dispatch layer
# ---------------------------------------------------------------------------

def _run(args):
    return subprocess.run(
        [sys.executable, "-m", "casetrack", *args],
        capture_output=True,
        text=True,
    )


def test_register_cohort_cli_end_to_end(tmp_path):
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"
    _write_sheet(sheet)
    r = _run(["register-cohort", "--project-dir", str(proj), "--samplesheet", str(sheet)])
    assert r.returncode == 0, r.stderr
    assert "register-cohort:" in r.stdout
    r2 = _run(["register-cohort", "--project-dir", str(proj), "--samplesheet", str(sheet), "--dry-run"])
    assert r2.returncode == 0 and "[dry-run]" in r2.stdout


def test_register_cohort_cli_validation_exit2(tmp_path):
    proj = _init_project(tmp_path)
    bad = tmp_path / "bad.tsv"
    bad.write_text("patient_id\tbogus_col\nP1\tx\n")  # undeclared column
    r = _run(["register-cohort", "--project-dir", str(proj), "--samplesheet", str(bad)])
    assert r.returncode == 2
    assert "Error" in r.stderr and "Traceback" not in r.stderr


# ---------------------------------------------------------------------------
# Fix 1 regression: blank optional cells store NULL, not empty string (0012 review)
# ---------------------------------------------------------------------------

def test_register_cohort_blank_optional_cell_stores_null(tmp_path):
    """A blank optional column (e.g. age) in the sample sheet must be stored as
    NULL in SQLite, not as an empty string ''.  This ensures add-metadata
    --fill-only can back-fill the value later, and NULL-based coverage queries
    give correct counts.

    The hgsoc template has optional patient columns (age, sex, diagnosis, …) and
    optional specimen columns (timepoint, collection_date, tumor_purity) — any of
    which may be absent from rows in the sheet.  We include 'age' (INTEGER) with
    a blank cell for P1 and a real value for P2.
    """
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort_blank.tsv"
    # Include the optional 'age' column; leave it blank for P1, filled for P2.
    sheet.write_text(
        "patient_id\tage\ttissue_site\tspecimen_id\tassay_type\tassay_id\n"
        "P1\t\ttumor\tP1-T\tONT\tP1-T-ONT\n"   # age blank → should be NULL
        "P2\t42\ttumor\tP2-T\tONT\tP2-T-ONT\n"  # age = 42
    )
    casetrack.cmd_register_cohort(_ns(proj, sheet))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        p1_age = conn.execute(
            "SELECT age FROM patients WHERE patient_id = 'P1'"
        ).fetchone()[0]
        p2_age = conn.execute(
            "SELECT age FROM patients WHERE patient_id = 'P2'"
        ).fetchone()[0]
    finally:
        conn.close()

    # P1's blank age must be NULL (Python None), not empty string
    assert p1_age is None, f"Expected NULL for blank age, got {p1_age!r}"
    # P2's age must be stored (SQLite stores INTEGER as int via _coerce_for_sqlite)
    assert p2_age is not None, "Expected non-NULL age for P2"


# ---------------------------------------------------------------------------
# Fix 2: --overwrite updates changed attributes (proposal §10, 0012 review)
# ---------------------------------------------------------------------------

def test_register_cohort_overwrite_updates_changed_attrs(tmp_path):
    """--overwrite must update a changed attribute value on re-registration.
    Without --overwrite (fill-only), an already-non-null value must NOT change.
    Uses the optional 'age' column (INTEGER) on the patient level in the hgsoc
    template.
    """
    proj = _init_project(tmp_path)

    # First load: P1 age = 30
    sheet1 = tmp_path / "cohort1.tsv"
    sheet1.write_text(
        "patient_id\tage\ttissue_site\tspecimen_id\tassay_type\tassay_id\n"
        "P1\t30\ttumor\tP1-T\tONT\tP1-T-ONT\n"
    )
    casetrack.cmd_register_cohort(_ns(proj, sheet1))

    conn = casetrack.open_project_db(proj / "casetrack.db")
    age_after_first = conn.execute(
        "SELECT age FROM patients WHERE patient_id = 'P1'"
    ).fetchone()[0]
    conn.close()
    assert age_after_first is not None  # sanity: first load stored the value

    # Second load: P1 age = 45, fill-only (no --overwrite) — must NOT change
    sheet2 = tmp_path / "cohort2.tsv"
    sheet2.write_text(
        "patient_id\tage\ttissue_site\tspecimen_id\tassay_type\tassay_id\n"
        "P1\t45\ttumor\tP1-T\tONT\tP1-T-ONT\n"
    )
    casetrack.cmd_register_cohort(_ns(proj, sheet2, overwrite=False))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    age_fill_only = conn.execute(
        "SELECT age FROM patients WHERE patient_id = 'P1'"
    ).fetchone()[0]
    conn.close()
    assert int(age_fill_only) == 30, (
        f"fill-only should not overwrite non-null age; got {age_fill_only!r}"
    )

    # Third load: same sheet, with --overwrite — must update age to 45
    casetrack.cmd_register_cohort(_ns(proj, sheet2, overwrite=True))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    age_after_overwrite = conn.execute(
        "SELECT age FROM patients WHERE patient_id = 'P1'"
    ).fetchone()[0]
    conn.close()
    assert int(age_after_overwrite) == 45, (
        f"--overwrite should update age to 45; got {age_after_overwrite!r}"
    )


# ---------------------------------------------------------------------------
# Fix 3a: FK linkage — 3-table JOIN produces the right row count (0012 review)
# ---------------------------------------------------------------------------

def test_register_cohort_join_across_levels(tmp_path):
    """A 3-table JOIN (patients ⋈ specimens ⋈ assays) must produce exactly one
    row per assay, confirming FK linkage is intact after register-cohort.
    """
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"
    _write_sheet(sheet)
    casetrack.cmd_register_cohort(_ns(proj, sheet))

    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        row_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM patients p
            JOIN specimens s ON s.patient_id = p.patient_id
            JOIN assays a ON a.specimen_id = s.specimen_id
            """
        ).fetchone()[0]
    finally:
        conn.close()

    # _write_sheet has 3 assays; each joins to exactly one specimen and one patient
    assert row_count == 3, f"Expected 3 joined rows, got {row_count}"


# ---------------------------------------------------------------------------
# Fix 3b: provenance shape — action + counts dict (0012 review)
# ---------------------------------------------------------------------------

def test_register_cohort_provenance_shape(tmp_path):
    """The provenance.jsonl entry written by register-cohort must have
    action == 'register_cohort' and a counts dict with inserted/updated
    keys for each level (patient, specimen, assay).
    """
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"
    _write_sheet(sheet)
    casetrack.cmd_register_cohort(_ns(proj, sheet))

    prov_path = proj / "provenance.jsonl"
    entries = [json.loads(ln) for ln in prov_path.read_text().splitlines() if ln.strip()]
    reg = next((e for e in entries if e.get("action") == "register_cohort"), None)
    assert reg is not None, "No 'register_cohort' entry found in provenance.jsonl"

    # counts dict must exist and have the expected structure
    counts = reg.get("counts", {})
    for lvl in ("patient", "specimen", "assay"):
        assert lvl in counts, f"counts missing level {lvl!r}"
        assert "inserted" in counts[lvl], f"counts[{lvl!r}] missing 'inserted'"
        assert "updated" in counts[lvl], f"counts[{lvl!r}] missing 'updated'"

    # first load: all 3 assays / 3 specimens / 2 patients must be inserts
    assert counts["patient"]["inserted"] == 2
    assert counts["specimen"]["inserted"] == 3
    assert counts["assay"]["inserted"] == 3
