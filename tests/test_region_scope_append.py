"""Tests for `append-cohort` region_scope + roles + reference-resolve (0013).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
from pathlib import Path

import casetrack
from casetrack_qc import cohort_artifacts as ca
from casetrack_qc import reference_artifacts as ra
from casetrack_qc.cohort_artifacts_cli import cmd_append_cohort


def _project_with_assays(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    ))
    # Seed two assays (A_T, A_N) on one specimen/patient via direct SQL — the
    # same pattern the existing cohort-artifact CLI tests use.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES ('P1');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES ('S1', 'P1', 'tumor');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('A_T', 'S1', 'ONT'),"
                "  ('A_N', 'S1', 'ONT');"
            )
    finally:
        conn.close()
    return proj


def _append_ns(proj, **kw):
    base = dict(
        project_dir=str(proj), analysis="dss_dmr", run_tag="rt1",
        path="/x/dmr.bed", inputs="A_T,A_N", inputs_from=None, stats=None,
        checksum=None, created_by=None, uses_references=None, derived_from=None,
        region_scope=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_append_stores_region_scope_and_roles(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    cmd_append_cohort(_append_ns(
        proj, region_scope="genome-wide", inputs="A_T:tumor,A_N:normal"))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "dss_dmr", "rt1")
        assert art.region_scope == "genome-wide"
        assert ca.artifact_input_roles(conn, art.artifact_id) == {
            "A_N": "normal", "A_T": "tumor"}
    finally:
        conn.close()


def test_region_scope_matching_ref_key_captures_reference_usage(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            ra.ensure_reference_schema(conn)
            ra.sync_references_from_toml(conn, {
                "promoters_EPDnew": {
                    "path": "/db/prom.bed", "version": "2026-04-14",
                    "kind": "intervals"}})
    finally:
        conn.close()

    cmd_append_cohort(_append_ns(proj, region_scope="promoters_EPDnew"))

    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "dss_dmr", "rt1")
        st = ra.output_staleness(conn, scope="cohort", artifact_id=art.artifact_id)
        assert st["state"] == "fresh"
        with casetrack.begin_immediate(conn):
            ra.sync_references_from_toml(conn, {
                "promoters_EPDnew": {
                    "path": "/db/prom.bed", "version": "2026-05-01",
                    "kind": "intervals"}})
        st2 = ra.output_staleness(conn, scope="cohort", artifact_id=art.artifact_id)
        assert st2["state"] == "STALE"
        assert any("promoters_EPDnew" in r for r in st2["reasons"])
    finally:
        conn.close()


def test_label_only_scope_captures_no_reference_usage(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            ra.ensure_reference_schema(conn)  # tables exist but no matching key
    finally:
        conn.close()
    cmd_append_cohort(_append_ns(proj, region_scope="chr17:7565097-7590856"))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "dss_dmr", "rt1")
        st = ra.output_staleness(conn, scope="cohort", artifact_id=art.artifact_id)
        assert st["state"] == "untracked"  # no usage rows captured
    finally:
        conn.close()
