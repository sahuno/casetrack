"""Tests for proposal 0013 schema: region_scope + input role columns.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import casetrack
from casetrack_qc import cohort_artifacts as ca


def _init_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    ns = argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    )
    casetrack.cmd_init(ns)
    return proj


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def test_fresh_init_has_region_scope_and_role(tmp_path: Path):
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert "region_scope" in _cols(conn, "cohort_artifacts")
        assert "role" in _cols(conn, "cohort_artifact_inputs")
    finally:
        conn.close()


def test_ensure_region_scope_columns_is_idempotent_and_additive(tmp_path: Path):
    """Drop the columns to emulate a pre-0013 project, then re-add them."""
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            # Emulate pre-0013: rebuild the 0009 tables without the new columns.
            conn.execute("DROP TABLE IF EXISTS cohort_artifact_inputs")
            conn.execute("DROP TABLE IF EXISTS cohort_artifacts")
            conn.execute(
                "CREATE TABLE cohort_artifacts (artifact_id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, analysis TEXT NOT NULL, run_tag TEXT NOT NULL, "
                "path TEXT NOT NULL, checksum TEXT, n_inputs INTEGER NOT NULL, "
                "stats_json TEXT, created_at TEXT NOT NULL, created_by TEXT, "
                "transaction_id TEXT NOT NULL, UNIQUE (analysis, run_tag))"
            )
            conn.execute(
                "CREATE TABLE cohort_artifact_inputs (artifact_id INTEGER NOT NULL, "
                "assay_id TEXT NOT NULL, PRIMARY KEY (artifact_id, assay_id))"
            )
        assert "region_scope" not in _cols(conn, "cohort_artifacts")
        with casetrack.begin_immediate(conn):
            executed = ca.ensure_region_scope_columns(conn)
        assert any("region_scope" in s for s in executed)
        assert any("role" in s for s in executed)
        assert "region_scope" in _cols(conn, "cohort_artifacts")
        assert "role" in _cols(conn, "cohort_artifact_inputs")
        # the grouping index is created alongside the column
        idx = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_cohort_artifacts_scope'"
        ).fetchone()
        assert idx is not None
        # Second call is a no-op.
        with casetrack.begin_immediate(conn):
            assert ca.ensure_region_scope_columns(conn) == []
    finally:
        conn.close()
