"""Tests for `casetrack doctor --project-dir` (proposal 0001 §9.3).

Fork-based concurrency smoke test. Relies on multiprocessing, so the tests
run a few workers × a few writes rather than the production defaults.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

import casetrack


def _init_ns(project_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="blank", project_name=None, force=False,
    )


def _doctor_ns(project_dir: Path, *, workers: int = 4, writes: int = 20) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir), workers=workers, writes=writes,
    )


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(p))
    return p


# ── Happy path ────────────────────────────────────────────────────────────────


def test_doctor_reports_healthy(proj: Path, capsys):
    casetrack.cmd_doctor_project(_doctor_ns(proj, workers=4, writes=10))
    out = capsys.readouterr().out
    assert "Testing SQLite concurrency" in out
    assert "Successful commits:    40/40" in out
    assert "healthy" in out


def test_doctor_leaves_no_scratch_table(proj: Path, capsys):
    casetrack.cmd_doctor_project(_doctor_ns(proj, workers=2, writes=5))
    capsys.readouterr()
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    finally:
        conn.close()
    assert "__doctor_scratch" not in names


def test_doctor_committed_rows_match_writes(proj: Path, capsys):
    casetrack.cmd_doctor_project(_doctor_ns(proj, workers=3, writes=7))
    out = capsys.readouterr().out
    assert "Rows committed in DB:  21" in out  # 3 × 7


def test_doctor_concurrent_workers(proj: Path, capsys):
    """Higher worker count + higher write count — still healthy locally."""
    casetrack.cmd_doctor_project(_doctor_ns(proj, workers=8, writes=15))
    out = capsys.readouterr().out
    assert "Successful commits:    120/120" in out


def test_doctor_cleans_up_leftover_scratch(proj: Path, capsys):
    """If a prior run aborted mid-test, DROP TABLE IF EXISTS should handle it."""
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    conn.execute("CREATE TABLE __doctor_scratch (x INT)")
    conn.commit()
    conn.close()

    casetrack.cmd_doctor_project(_doctor_ns(proj, workers=2, writes=5))
    assert "healthy" in capsys.readouterr().out


# ── Filesystem detection ──────────────────────────────────────────────────────


def test_filesystem_name_returns_something(tmp_path: Path):
    name = casetrack._filesystem_name(tmp_path)
    assert isinstance(name, str) and name
