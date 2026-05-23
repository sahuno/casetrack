"""Tests for the _cohort_artifacts view region_scope + scope_ref_key (0013).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
from pathlib import Path

import casetrack
from casetrack_qc import reference_artifacts as ra
from casetrack_qc.cohort_artifacts_cli import cmd_append_cohort


def _project_with_scoped_artifacts(tmp_path: Path) -> Path:
    """Init a project, seed one assay + one TOML-style reference, register two
    cohort artifacts — one with a reference-backed scope, one with a free-text
    label.

    Matches the seed pattern used by tests/test_region_scope_listcmd.py — single-
    column TSVs are rejected by cmd_add_metadata, so we go direct to SQL.
    """
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="regionview", force=False,
    ))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    with casetrack.begin_immediate(conn):
        conn.executescript(
            "INSERT INTO patients (patient_id) VALUES ('P1');\n"
            "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
            "VALUES ('S1', 'P1', 'tumor');\n"
            "INSERT INTO assays (assay_id, specimen_id, assay_type) "
            "VALUES ('A1', 'S1', 'ONT');"
        )
        ra.ensure_reference_schema(conn)
        ra.sync_references_from_toml(conn, {
            "promoters_EPDnew": {"path": "/db/p.bed", "version": "v1",
                                 "kind": "intervals"},
        })
    conn.close()

    def _ns(**kw):
        base = dict(project_dir=str(proj), path="/x", inputs="A1",
                    inputs_from=None, stats=None, checksum=None, created_by=None,
                    uses_references=None, derived_from=None, region_scope=None)
        base.update(kw)
        return argparse.Namespace(**base)

    cmd_append_cohort(_ns(analysis="dss_dmr", run_tag="prom",
                          region_scope="promoters_EPDnew"))
    cmd_append_cohort(_ns(analysis="dss_dmr", run_tag="gw",
                          region_scope="genome-wide"))
    cmd_append_cohort(_ns(analysis="dss_dmr", run_tag="nosc",
                          region_scope=None))
    return proj


def test_view_on_pre_0013_db_emits_null_scope_columns(tmp_path: Path):
    """Pre-0013 project: ``region_scope`` column absent → view still installs
    and both new columns are NULL for every row.

    Uses Approach A (``ALTER TABLE … DROP COLUMN``); SQLite ≥ 3.35 required.
    """
    proj = _project_with_scoped_artifacts(tmp_path)
    # Simulate a pre-0013 DB by dropping the region_scope column that
    # cmd_append_cohort wrote.  Approach A: ALTER TABLE DROP COLUMN.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    with casetrack.begin_immediate(conn):
        conn.execute("ALTER TABLE cohort_artifacts DROP COLUMN region_scope")
    conn.close()

    # The view must install cleanly and return NULL for both scope columns.
    con = casetrack._prepare_v03_query_connection(proj / "casetrack.db")
    try:
        rows = con.execute(
            'SELECT region_scope, scope_ref_key FROM "_cohort_artifacts"'
        ).fetchall()
    finally:
        con.close()

    assert len(rows) > 0, "expected at least one artifact row"
    for region_scope, scope_ref_key in rows:
        assert region_scope is None, f"expected NULL region_scope, got {region_scope!r}"
        assert scope_ref_key is None, f"expected NULL scope_ref_key, got {scope_ref_key!r}"


def test_view_exposes_region_scope_and_scope_ref_key(tmp_path: Path):
    proj = _project_with_scoped_artifacts(tmp_path)
    con = casetrack._prepare_v03_query_connection(proj / "casetrack.db")
    try:
        rows = con.execute(
            'SELECT run_tag, region_scope, scope_ref_key FROM "_cohort_artifacts" '
            'ORDER BY run_tag'
        ).fetchall()
    finally:
        con.close()
    got = {run_tag: (region_scope, scope_ref_key)
           for run_tag, region_scope, scope_ref_key in rows}
    assert got["prom"] == ("promoters_EPDnew", "promoters_EPDnew")
    # Label-only scope: no reference_artifacts row resolves → scope_ref_key NULL.
    assert got["gw"] == ("genome-wide", None)
    # Unscoped artifact: both columns NULL.
    assert got["nosc"] == (None, None)
