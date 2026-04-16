"""Tests for concurrent v0.3 writes under WAL + BEGIN IMMEDIATE.

Fork multiple writers against a tmpdir SQLite project. Verifies that
`cmd_append_project`'s transactional semantics + the busy_timeout pragma
together let N independent processes converge to a consistent DB state
without corruption.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import casetrack


def _init_ns(project_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="blank", project_name=None, force=False,
    )


def _reg_ns(project_dir: Path, *, level: str, id: str,
            parent: str | None = None, meta: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir), level=level, id=id, parent=parent,
        meta=meta, allow_new_parent=False, yes=False,
    )


def _worker_append(project_dir: str, analysis: str, assays: list, worker_id: int) -> str | None:
    """Run cmd_append_project from a forked subprocess.

    Returns None on success or a string error message on failure.
    """
    try:
        tsv = Path(project_dir) / f"worker_{worker_id}.tsv"
        pd.DataFrame({
            "assay_id": assays,
            f"val_{worker_id}": [float(worker_id)] * len(assays),
        }).to_csv(tsv, sep="\t", index=False)

        ns = argparse.Namespace(
            manifest=None, project_dir=project_dir, results=str(tsv),
            key="sample_id", analysis=analysis, level=None, col_type=None,
            overwrite=False, allow_new=False, yes=False,
        )
        casetrack.cmd_append_project(ns)
        return None
    except SystemExit as e:
        return f"worker {worker_id} exit {e.code}"
    except Exception as e:  # noqa: BLE001
        return f"worker {worker_id} {type(e).__name__}: {e}"


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P1"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S1", parent="P1"))
    # Register 12 assays — each worker will write to all of them.
    for i in range(12):
        casetrack.cmd_register(_reg_ns(
            proj, level="assay", id=f"A{i:02d}", parent="S1", meta="assay_type=TEXT",
        ))
    return proj


def test_concurrent_appends_different_analyses(seeded: Path):
    """6 workers each append their own analysis name in parallel — they should
    all converge, each adding two new columns (val_<id> and <analysis>_done)."""
    assays = [f"A{i:02d}" for i in range(12)]

    ctx = mp.get_context("fork")
    with ctx.Pool(processes=6) as pool:
        results = pool.starmap(
            _worker_append,
            [(str(seeded), f"a{w}", assays, w) for w in range(6)],
        )

    failures = [r for r in results if r is not None]
    assert not failures, f"concurrent append failures: {failures}"

    conn = sqlite3.connect(str(seeded / "casetrack.db"))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(assays)").fetchall()}
        # Each worker added two columns: val_N + aN_done.
        for w in range(6):
            assert f"val_{w}" in cols, f"val_{w} missing"
            assert f"a{w}_done" in cols, f"a{w}_done missing"
        # Every row has every worker's value.
        for w in range(6):
            rows = conn.execute(
                f'SELECT COUNT(*) FROM assays WHERE "val_{w}" IS NOT NULL'
            ).fetchone()[0]
            assert rows == 12, f"worker {w} only wrote {rows}/12 rows"
    finally:
        conn.close()


def test_no_partial_commits_under_concurrent_load(seeded: Path):
    """Each worker's append is atomic — we should never see half-written rows
    (a row with the `_done` timestamp but NULL in the value column)."""
    assays = [f"A{i:02d}" for i in range(12)]
    ctx = mp.get_context("fork")
    with ctx.Pool(processes=4) as pool:
        pool.starmap(
            _worker_append,
            [(str(seeded), f"tx{w}", assays, w) for w in range(4)],
        )

    conn = sqlite3.connect(str(seeded / "casetrack.db"))
    try:
        for w in range(4):
            # No assay should have `done` but NULL value — that's a partial commit.
            (inconsistent,) = conn.execute(
                f'SELECT COUNT(*) FROM assays '
                f'WHERE "tx{w}_done" IS NOT NULL AND "val_{w}" IS NULL'
            ).fetchone()
            assert inconsistent == 0, (
                f"worker {w} left {inconsistent} partial-commit rows"
            )
    finally:
        conn.close()
