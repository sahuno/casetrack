"""Tests for proposal 0013 CRUD: storing region_scope + input roles.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
from pathlib import Path

import casetrack
from casetrack_qc import cohort_artifacts as ca


def _project_with_assays(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    ))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES ('P1');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) VALUES "
                "  ('S1', 'P1', 'tumor');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('A_T', 'S1', 'ONT'),"
                "  ('A_N', 'S1', 'ONT');"
            )
    finally:
        conn.close()
    return proj


def test_insert_artifact_stores_region_scope(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            ca.ensure_cohort_artifacts_schema(conn)
            aid = ca.insert_artifact(
                conn, analysis="dss_dmr", run_tag="rt1", path="/x/dmr.bed",
                n_inputs=2, transaction_id="t1", region_scope="promoters_EPDnew")
        art = ca.get_artifact(conn, aid)
        assert art.region_scope == "promoters_EPDnew"
        with casetrack.begin_immediate(conn):
            aid2 = ca.insert_artifact(
                conn, analysis="dss_dmr", run_tag="rt2", path="/x/d2.bed",
                n_inputs=1, transaction_id="t2")
        assert ca.get_artifact(conn, aid2).region_scope is None
    finally:
        conn.close()


def test_add_artifact_inputs_stores_roles(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            ca.ensure_cohort_artifacts_schema(conn)
            aid = ca.insert_artifact(
                conn, analysis="dss_dmr", run_tag="rt1", path="/x/dmr.bed",
                n_inputs=2, transaction_id="t1")
            ca.add_artifact_inputs(
                conn, aid, ["A_T", "A_N"], roles={"A_T": "tumor", "A_N": "normal"})
        assert ca.artifact_inputs(conn, aid) == ["A_N", "A_T"]
        assert ca.artifact_input_roles(conn, aid) == {"A_N": "normal", "A_T": "tumor"}
    finally:
        conn.close()


def test_add_artifact_inputs_without_roles_is_backward_compatible(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            ca.ensure_cohort_artifacts_schema(conn)
            aid = ca.insert_artifact(
                conn, analysis="x", run_tag="rt1", path="/x", n_inputs=1,
                transaction_id="t1")
            ca.add_artifact_inputs(conn, aid, ["A_T"])
        assert ca.artifact_input_roles(conn, aid) == {"A_T": None}
    finally:
        conn.close()
