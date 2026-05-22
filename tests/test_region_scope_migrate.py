"""Tests for `casetrack migrate-region-scope` (proposal 0013).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import casetrack
from casetrack_qc.cohort_artifacts_cli import cmd_migrate_region_scope


def _pre0013_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    ))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    with casetrack.begin_immediate(conn):
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
    conn.close()
    return proj


def _cols(proj: Path, table: str) -> set[str]:
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    finally:
        conn.close()


def test_migrate_region_scope_adds_columns(tmp_path: Path, capsys):
    proj = _pre0013_project(tmp_path)
    assert "region_scope" not in _cols(proj, "cohort_artifacts")
    cmd_migrate_region_scope(argparse.Namespace(project_dir=str(proj), dry_run=False))
    assert "region_scope" in _cols(proj, "cohort_artifacts")
    assert "role" in _cols(proj, "cohort_artifact_inputs")


def test_migrate_region_scope_dry_run_changes_nothing(tmp_path: Path, capsys):
    proj = _pre0013_project(tmp_path)
    cmd_migrate_region_scope(argparse.Namespace(project_dir=str(proj), dry_run=True))
    assert "region_scope" not in _cols(proj, "cohort_artifacts")
    assert "dry-run" in capsys.readouterr().out.lower()


def test_migrate_region_scope_is_idempotent(tmp_path: Path, capsys):
    proj = _pre0013_project(tmp_path)
    cmd_migrate_region_scope(argparse.Namespace(project_dir=str(proj), dry_run=False))
    cmd_migrate_region_scope(argparse.Namespace(project_dir=str(proj), dry_run=False))
    out = capsys.readouterr().out.lower()
    assert "no migration needed" in out
    prov = (proj / "provenance.jsonl").read_text().splitlines()
    assert any(json.loads(l).get("action") == "migrate_region_scope" for l in prov)
