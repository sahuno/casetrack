"""Tests for `cohort-artifacts` region_scope display + --scope filter (0013).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import casetrack
from casetrack_qc.cohort_artifacts_cli import cmd_append_cohort, cmd_cohort_artifacts


def _project_with_two_scoped_artifacts(tmp_path: Path) -> Path:
    """Init a project, seed one assay via direct SQL, append two scoped artifacts.

    Matches the seed pattern used by tests/test_region_scope_append.py — single-
    column TSVs are rejected by cmd_add_metadata.
    """
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
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
    conn.close()

    def _ns(**kw):
        base = dict(project_dir=str(proj), path="/x", inputs="A1",
                    inputs_from=None, stats=None, checksum=None, created_by=None,
                    uses_references=None, derived_from=None, region_scope=None)
        base.update(kw)
        return argparse.Namespace(**base)

    cmd_append_cohort(_ns(analysis="dss_dmr", run_tag="gw", region_scope="genome-wide"))
    cmd_append_cohort(_ns(analysis="dss_dmr", run_tag="prom",
                          region_scope="promoters_EPDnew"))
    return proj


def test_json_output_includes_region_scope(tmp_path: Path, capsys):
    proj = _project_with_two_scoped_artifacts(tmp_path)
    capsys.readouterr()  # drain init + append-cohort banners
    cmd_cohort_artifacts(argparse.Namespace(
        project_dir=str(proj), fmt="json", stale_only=False, scope=None))
    rows = json.loads(capsys.readouterr().out)
    scopes = {r["run_tag"]: r["region_scope"] for r in rows}
    assert scopes == {"gw": "genome-wide", "prom": "promoters_EPDnew"}


def test_scope_filter_narrows_rows(tmp_path: Path, capsys):
    proj = _project_with_two_scoped_artifacts(tmp_path)
    capsys.readouterr()  # drain init + append-cohort banners
    cmd_cohort_artifacts(argparse.Namespace(
        project_dir=str(proj), fmt="json", stale_only=False,
        scope="promoters_EPDnew"))
    rows = json.loads(capsys.readouterr().out)
    assert [r["run_tag"] for r in rows] == ["prom"]


def test_table_output_shows_scope(tmp_path: Path, capsys):
    proj = _project_with_two_scoped_artifacts(tmp_path)
    capsys.readouterr()  # drain init + append-cohort banners
    cmd_cohort_artifacts(argparse.Namespace(
        project_dir=str(proj), fmt="table", stale_only=False, scope=None))
    out = capsys.readouterr().out
    assert "genome-wide" in out and "promoters_EPDnew" in out
