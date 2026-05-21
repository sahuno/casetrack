"""Cohort-artifact staleness surfaced in the existing read paths.

Proposal 0009 §4: staleness must be visible in `status`, `query`, and `export`
— not only the dedicated `cohort-artifacts` command.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-20
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import casetrack
from casetrack_qc.cohort_artifacts_cli import cmd_append_cohort


def _project_with_stale_artifact(tmp_path: Path) -> Path:
    """Init a project, seed 2 assays, register a cohort artifact over both,
    then censor one input so the artifact is stale."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="rp", force=False,
    ))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES ('P1'), ('P2');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "  VALUES ('P1-t', 'P1', 'tumor'), ('P2-t', 'P2', 'tumor');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('P1-t-ONT', 'P1-t', 'ONT'), ('P2-t-ONT', 'P2-t', 'ONT');"
            )
    finally:
        conn.close()
    cmd_append_cohort(argparse.Namespace(
        project_dir=str(proj), analysis="joint_genotype", run_tag="run1",
        path="/cohort.vcf.gz", inputs="P1-t-ONT,P2-t-ONT", inputs_from=None,
        stats=None, checksum=None, created_by=None,
    ))
    # Censor one input.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute(
                "UPDATE assays SET qc_status='censored' WHERE assay_id='P1-t-ONT'"
            )
    finally:
        conn.close()
    return proj


# ── query: _cohort_artifacts view ───────────────────────────────────────────


def test_query_cohort_artifacts_view_exposes_staleness(tmp_path):
    proj = _project_with_stale_artifact(tmp_path)
    con = casetrack._prepare_v03_query_connection(proj / "casetrack.db")
    try:
        rows = con.execute(
            'SELECT run_tag, n_censored_inputs, stale FROM "_cohort_artifacts"'
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    run_tag, n_censored, stale = rows[0]
    assert run_tag == "run1"
    assert n_censored == 1
    assert bool(stale) is True


# ── status: cohort-artifacts section ────────────────────────────────────────


def _status_ns(proj, **kw):
    base = dict(
        project_dir=str(proj), project=None, group_by=None, fmt="table",
        usable=False, include_censored=False, include_consent_revoked=False,
        show_lineage=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_status_shows_cohort_artifact_staleness(tmp_path, capsys):
    proj = _project_with_stale_artifact(tmp_path)
    casetrack.cmd_status_project(_status_ns(proj))
    out = capsys.readouterr().out
    assert "Cohort artifacts" in out
    assert "STALE" in out
    assert "run1" in out


# ── export: --include-cohort-artifacts ──────────────────────────────────────


def _export_ns(proj, output, **kw):
    base = dict(
        project_dir=str(proj), project=None, output=str(output), sql=None,
        shape="tables", tables=None, include_censored=True,
        include_consent_revoked=True, include_lineage=False,
        include_cohort_artifacts=True,
    )
    base.update(kw)
    return argparse.Namespace(**base)


# ── dashboard: cohort-artifacts section ─────────────────────────────────────


def test_dashboard_shows_cohort_artifacts_section(tmp_path):
    proj = _project_with_stale_artifact(tmp_path)
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard_project(argparse.Namespace(
        project_dir=str(proj), project=None, output=str(out),
    ))
    html_text = out.read_text()
    assert "Cohort artifacts" in html_text
    assert "STALE" in html_text
    assert "run1" in html_text
    assert "P1-t-ONT" in html_text  # the censored input is named


def test_export_includes_cohort_artifacts_with_stale_column(tmp_path):
    import pandas as pd
    proj = _project_with_stale_artifact(tmp_path)
    out = tmp_path / "export.tsv"
    casetrack.cmd_export_project(_export_ns(proj, out))
    ca_file = tmp_path / "export.cohort_artifacts.tsv"
    assert ca_file.exists(), "cohort_artifacts table not exported"
    df = pd.read_csv(ca_file, sep="\t")
    assert "stale" in df.columns
    assert "n_censored_inputs" in df.columns
    row = df[df["run_tag"] == "run1"].iloc[0]
    assert bool(row["stale"]) is True
    assert int(row["n_censored_inputs"]) == 1
