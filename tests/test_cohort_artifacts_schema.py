"""Tests for casetrack_qc.cohort_artifacts — DDL + idempotent ensure.

Proposal 0009 §6.1. Mirrors the qc_events additive-sibling pattern.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-20
"""
from __future__ import annotations

import argparse
import sqlite3
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
    return proj


@pytest.fixture
def conn(tmp_path: Path):
    """A connection in the *pre-0009* state.

    ``casetrack init`` now creates the cohort-artifact tables (they are wired
    into the init transaction), so to unit-test ``ensure`` / ``schema_exists``
    from scratch we drop them first — emulating a project created before this
    feature shipped. (That init creates them is locked by
    ``test_init_creates_cohort_artifacts_schema`` below.)
    """
    proj = _init_project(tmp_path)
    c = casetrack.open_project_db(proj / "casetrack.db")
    with casetrack.begin_immediate(c):
        c.execute("DROP TABLE IF EXISTS cohort_artifact_inputs")
        c.execute("DROP TABLE IF EXISTS cohort_artifacts")
    try:
        yield c
    finally:
        c.close()


def test_init_creates_cohort_artifacts_schema(tmp_path: Path):
    """Fresh `casetrack init` wires the cohort-artifact tables in-place."""
    proj = _init_project(tmp_path)
    c = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert ca.cohort_artifacts_schema_exists(c) is True
    finally:
        c.close()


# ── DDL / ensure ────────────────────────────────────────────────────────────


def test_ensure_creates_cohort_artifacts_table(conn):
    ca.ensure_cohort_artifacts_schema(conn)
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "cohort_artifacts" in names


def test_ensure_creates_inputs_join_table(conn):
    ca.ensure_cohort_artifacts_schema(conn)
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "cohort_artifact_inputs" in names


def test_ensure_creates_indexes(conn):
    ca.ensure_cohort_artifacts_schema(conn)
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_cohort_artifacts_key" in names
    assert "idx_cohort_artifact_inputs_assay" in names


def test_ensure_is_idempotent(conn):
    first = ca.ensure_cohort_artifacts_schema(conn)
    second = ca.ensure_cohort_artifacts_schema(conn)
    assert first, "first call should execute DDL"
    assert second == [], "second call should be a no-op"


def test_schema_exists_false_before_true_after(conn):
    assert ca.cohort_artifacts_schema_exists(conn) is False
    ca.ensure_cohort_artifacts_schema(conn)
    assert ca.cohort_artifacts_schema_exists(conn) is True


def test_unique_analysis_run_tag_enforced(conn):
    ca.ensure_cohort_artifacts_schema(conn)
    conn.execute(
        "INSERT INTO cohort_artifacts "
        "(analysis, run_tag, path, n_inputs, created_at, transaction_id) "
        "VALUES ('joint_genotype', 'run1', '/p.vcf.gz', 2, 't', 'txn1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cohort_artifacts "
            "(analysis, run_tag, path, n_inputs, created_at, transaction_id) "
            "VALUES ('joint_genotype', 'run1', '/other.vcf.gz', 3, 't', 'txn2')"
        )


def test_input_fk_rejects_unknown_assay(conn):
    ca.ensure_cohort_artifacts_schema(conn)
    cur = conn.execute(
        "INSERT INTO cohort_artifacts "
        "(analysis, run_tag, path, n_inputs, created_at, transaction_id) "
        "VALUES ('joint_genotype', 'run1', '/p.vcf.gz', 1, 't', 'txn1')"
    )
    art_id = cur.lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cohort_artifact_inputs (artifact_id, assay_id) "
            "VALUES (?, 'no-such-assay')",
            (art_id,),
        )
