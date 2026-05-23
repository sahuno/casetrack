"""Region_scope surfaces in status, dashboard, and MCP tool (proposal 0013).

Three thin surface tests — the heavy lifting (region_scope persisted on
``cohort_artifacts``, propagated through the ``_cohort_artifacts`` view,
exported as a column) is covered by sibling 0013 test modules. These confirm
that the read-path surfaces *display* the scope.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
from pathlib import Path

import casetrack
from casetrack_qc.cohort_artifacts_cli import cmd_append_cohort
from casetrack_mcp.tools import cohort_artifacts_tool


def _scoped_project(tmp_path: Path, *, registered: bool = False) -> Path:
    """Init a project, seed one assay via direct SQL, append a scoped artifact.

    Mirrors the seed pattern in tests/test_region_scope_listcmd.py — single-column
    TSVs are rejected by cmd_add_metadata.

    When ``registered=True``, also registers the project so MCP tools can
    resolve it by ``project_id``.
    """
    proj = tmp_path / "proj"
    if registered:
        # bare=True / project_name=None mirrors the MCP-test fixture so the
        # project_id is the directory basename.
        casetrack.cmd_init(argparse.Namespace(
            manifest=None, project_dir=str(proj), samples=None, key="sample_id",
            metadata=None, cols=None, from_template="blank",
            project_name=None, project_id=None, force=False, bare=True,
        ))
    else:
        casetrack.cmd_init(argparse.Namespace(
            manifest=None, project_dir=str(proj), samples=None, key="sample_id",
            metadata=None, cols=None, from_template="hgsoc",
            project_name="test", force=False,
        ))
    # Blank template lacks `tissue_site`; hgsoc template has it. Probe the
    # specimens schema and pick the matching INSERT.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        spec_cols = {r[1] for r in conn.execute("PRAGMA table_info(specimens)")}
        with casetrack.begin_immediate(conn):
            conn.execute("INSERT INTO patients (patient_id) VALUES ('P1')")
            if "tissue_site" in spec_cols:
                conn.execute(
                    "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                    "VALUES ('S1', 'P1', 'tumor')"
                )
            else:
                conn.execute(
                    "INSERT INTO specimens (specimen_id, patient_id) "
                    "VALUES ('S1', 'P1')"
                )
            conn.execute(
                "INSERT INTO assays (assay_id, specimen_id, assay_type) "
                "VALUES ('A1', 'S1', 'ONT')"
            )
    finally:
        conn.close()
    cmd_append_cohort(argparse.Namespace(
        project_dir=str(proj), analysis="dss_dmr", run_tag="gw", path="/x",
        inputs="A1", inputs_from=None, stats=None, checksum=None,
        created_by=None, uses_references=None, derived_from=None,
        region_scope="genome-wide"))
    return proj


# ── status: _emit_cohort_artifacts_section ──────────────────────────────────


def test_status_section_shows_scope(tmp_path: Path, capsys):
    proj = _scoped_project(tmp_path)
    capsys.readouterr()  # drain init + append-cohort banners
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        casetrack._emit_cohort_artifacts_section(conn)
    finally:
        conn.close()
    out = capsys.readouterr().out
    assert "Cohort artifacts" in out
    assert "dss_dmr/gw" in out
    assert "scope=genome-wide" in out


def test_status_section_omits_scope_when_null(tmp_path: Path, capsys):
    """An artifact without region_scope must not emit `scope=` (no `scope=None`)."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    ))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute("INSERT INTO patients (patient_id) VALUES ('P1')")
            conn.execute(
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES ('S1', 'P1', 'tumor')"
            )
            conn.execute(
                "INSERT INTO assays (assay_id, specimen_id, assay_type) "
                "VALUES ('A1', 'S1', 'ONT')"
            )
    finally:
        conn.close()
    cmd_append_cohort(argparse.Namespace(
        project_dir=str(proj), analysis="dss_dmr", run_tag="nosc", path="/x",
        inputs="A1", inputs_from=None, stats=None, checksum=None,
        created_by=None, uses_references=None, derived_from=None,
        region_scope=None))
    capsys.readouterr()
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        casetrack._emit_cohort_artifacts_section(conn)
    finally:
        conn.close()
    out = capsys.readouterr().out
    assert "dss_dmr/nosc" in out
    assert "scope=" not in out


# ── dashboard: _cohort_artifacts_html ───────────────────────────────────────


def test_dashboard_renders_scope_column(tmp_path: Path):
    proj = _scoped_project(tmp_path)
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard_project(argparse.Namespace(
        project_dir=str(proj), project=None, output=str(out),
    ))
    html_text = out.read_text()
    # Header column + value cell both present.
    assert "<th>scope</th>" in html_text
    assert "genome-wide" in html_text


# ── MCP: cohort_artifacts_tool payload ──────────────────────────────────────


def test_mcp_tool_includes_region_scope(tmp_path: Path):
    proj = _scoped_project(tmp_path, registered=True)
    payload = cohort_artifacts_tool(proj.name)
    assert payload["n_artifacts"] == 1
    art = payload["artifacts"][0]
    assert art["region_scope"] == "genome-wide"
