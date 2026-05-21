"""Tests for casetrack_qc.cohort_artifacts CRUD + read-time staleness.

Proposal 0009 §6.1–6.2.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-20
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import casetrack
from casetrack_qc import cohort_artifacts as ca


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _init_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    ns = argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    )
    casetrack.cmd_init(ns)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES ('P1'), ('P2');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) VALUES "
                "  ('P1-t', 'P1', 'tumor'), ('P2-t', 'P2', 'tumor');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('P1-t-ONT', 'P1-t', 'ONT'),"
                "  ('P2-t-ONT', 'P2-t', 'ONT');"
            )
    finally:
        conn.close()
    return proj


@pytest.fixture
def conn(tmp_path: Path):
    proj = _init_project(tmp_path)
    c = casetrack.open_project_db(proj / "casetrack.db")
    ca.ensure_cohort_artifacts_schema(c)
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _insert(conn, **kw):
    defaults = dict(
        analysis="joint_genotype", run_tag="run1", path="/cohort.vcf.gz",
        n_inputs=2, transaction_id="txn1",
    )
    defaults.update(kw)
    return ca.insert_artifact(conn, **defaults)


# ── CRUD ──────────────────────────────────────────────────────────────────────


def test_insert_artifact_returns_id_and_roundtrips(conn):
    art_id = _insert(conn, checksum="abc", stats_json='{"ti_tv": 2.04}')
    got = ca.get_artifact(conn, art_id)
    assert got is not None
    assert got.analysis == "joint_genotype"
    assert got.run_tag == "run1"
    assert got.path == "/cohort.vcf.gz"
    assert got.checksum == "abc"
    assert got.n_inputs == 2
    assert got.stats_json == '{"ti_tv": 2.04}'


def test_insert_duplicate_key_raises_friendly_error(conn):
    _insert(conn)
    with pytest.raises(ca.CohortArtifactError, match="already exists"):
        _insert(conn, path="/other.vcf.gz")


def test_get_artifact_by_key(conn):
    art_id = _insert(conn)
    got = ca.get_artifact_by_key(conn, "joint_genotype", "run1")
    assert got is not None and got.artifact_id == art_id


def test_get_artifact_by_key_missing_returns_none(conn):
    assert ca.get_artifact_by_key(conn, "joint_genotype", "nope") is None


def test_list_artifacts(conn):
    _insert(conn, run_tag="run1")
    _insert(conn, run_tag="run2")
    arts = ca.list_artifacts(conn)
    assert {a.run_tag for a in arts} == {"run1", "run2"}


def test_add_and_read_inputs_roundtrip(conn):
    art_id = _insert(conn)
    n = ca.add_artifact_inputs(conn, art_id, ["P1-t-ONT", "P2-t-ONT"])
    assert n == 2
    assert ca.artifact_inputs(conn, art_id) == ["P1-t-ONT", "P2-t-ONT"]


def test_add_inputs_rejects_unknown_assay(conn):
    art_id = _insert(conn)
    with pytest.raises(ca.CohortArtifactError, match="unknown assay"):
        ca.add_artifact_inputs(conn, art_id, ["P1-t-ONT", "ghost"])


# ── Staleness (the differentiator) ──────────────────────────────────────────


def test_fresh_artifact_is_not_stale(conn):
    art_id = _insert(conn)
    ca.add_artifact_inputs(conn, art_id, ["P1-t-ONT", "P2-t-ONT"])
    conn.commit()
    stale = ca.artifact_staleness(conn)
    assert stale[art_id] == []


def test_censored_input_makes_artifact_stale(conn):
    art_id = _insert(conn)
    ca.add_artifact_inputs(conn, art_id, ["P1-t-ONT", "P2-t-ONT"])
    conn.commit()
    with casetrack.begin_immediate(conn):
        conn.execute(
            "UPDATE assays SET qc_status='censored' WHERE assay_id='P1-t-ONT'"
        )
    stale = ca.artifact_staleness(conn)
    assert stale[art_id] == ["P1-t-ONT"]


def test_consent_revoked_patient_cascades_to_stale(conn):
    art_id = _insert(conn)
    ca.add_artifact_inputs(conn, art_id, ["P1-t-ONT", "P2-t-ONT"])
    conn.commit()
    with casetrack.begin_immediate(conn):
        conn.execute(
            "UPDATE patients SET consent_status='revoked' WHERE patient_id='P2'"
        )
    stale = ca.artifact_staleness(conn)
    assert stale[art_id] == ["P2-t-ONT"]


def test_staleness_respects_include_censored(conn):
    art_id = _insert(conn)
    ca.add_artifact_inputs(conn, art_id, ["P1-t-ONT", "P2-t-ONT"])
    conn.commit()
    with casetrack.begin_immediate(conn):
        conn.execute(
            "UPDATE assays SET qc_status='censored' WHERE assay_id='P1-t-ONT'"
        )
    # With include_censored, a censored input no longer counts as stale.
    stale = ca.artifact_staleness(conn, include_censored=True)
    assert stale[art_id] == []
