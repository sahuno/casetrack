"""Concurrency + end-to-end integration tests.

Simulates multiple SLURM array tasks appending to the same manifest in parallel
to verify POSIX flock + smart merge cooperate correctly.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from multiprocessing import Process
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from conftest import write_tsv


REPO_ROOT = Path(casetrack.__file__).resolve().parent


def _append_worker(manifest: str, results: str, analysis: str):
    """Child-process entry point for cmd_append."""
    ns = argparse.Namespace(
        manifest=manifest,
        results=results,
        key="sample_id",
        analysis=analysis,
        overwrite=False,
        allow_new=False,
    )
    casetrack.cmd_append(ns)


def test_parallel_appends_converge(initialized_manifest: Path, tmp_project: Path):
    """Five parallel appends — one per sample — must all land in the manifest."""
    sample_ids = [f"SAMPLE_0{i}" for i in range(1, 6)]
    results_files = []
    for i, sid in enumerate(sample_ids):
        rpath = tmp_project / f"r_{sid}.tsv"
        write_tsv(
            rpath,
            pd.DataFrame({"sample_id": [sid], "modkit_mean_meth": [0.1 * (i + 1)]}),
        )
        results_files.append(rpath)

    procs = [
        Process(target=_append_worker, args=(str(initialized_manifest), str(r), "modkit"))
        for r in results_files
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker failed with exit {p.exitcode}"

    df = pd.read_csv(initialized_manifest, sep="\t").set_index("sample_id")
    assert "modkit_mean_meth" in df.columns
    assert "modkit_done" in df.columns

    # All five samples must have received their value.
    for i, sid in enumerate(sample_ids):
        assert df.loc[sid, "modkit_mean_meth"] == pytest.approx(0.1 * (i + 1))
        assert pd.notna(df.loc[sid, "modkit_done"])


def test_parallel_appends_different_analyses(initialized_manifest: Path, tmp_project: Path):
    """Appends from different analyses in parallel shouldn't corrupt each other."""
    r_modkit = tmp_project / "modkit.tsv"
    write_tsv(
        r_modkit,
        pd.DataFrame(
            {"sample_id": ["SAMPLE_01", "SAMPLE_02"], "modkit_mean_meth": [0.5, 0.6]}
        ),
    )
    r_tldr = tmp_project / "tldr.tsv"
    write_tsv(
        r_tldr,
        pd.DataFrame(
            {"sample_id": ["SAMPLE_02", "SAMPLE_03"], "tldr_l1_count": [10, 20]}
        ),
    )

    p1 = Process(target=_append_worker, args=(str(initialized_manifest), str(r_modkit), "modkit"))
    p2 = Process(target=_append_worker, args=(str(initialized_manifest), str(r_tldr), "tldr"))
    p1.start()
    p2.start()
    p1.join(60)
    p2.join(60)
    assert p1.exitcode == 0 and p2.exitcode == 0

    df = pd.read_csv(initialized_manifest, sep="\t").set_index("sample_id")
    expected = {"modkit_mean_meth", "modkit_done", "tldr_l1_count", "tldr_done"}
    assert expected.issubset(set(df.columns))
    assert df.loc["SAMPLE_01", "modkit_mean_meth"] == 0.5
    assert df.loc["SAMPLE_02", "tldr_l1_count"] == 10
    assert df.loc["SAMPLE_03", "tldr_l1_count"] == 20


# ── CLI integration (subprocess) ───────────────────────────────────────────────


def _run_cli(*cli_args: str, cwd: Path) -> subprocess.CompletedProcess:
    script = REPO_ROOT / "casetrack.py"
    return subprocess.run(
        [sys.executable, str(script), *cli_args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )


def test_cli_end_to_end(tmp_project: Path, samples_file: Path):
    """init → append × 2 → status → validate — all via subprocess entry point."""
    manifest = tmp_project / "manifest.tsv"

    res = _run_cli(
        "init",
        "--manifest", str(manifest),
        "--samples", str(samples_file),
        cwd=tmp_project,
    )
    assert res.returncode == 0, res.stderr

    # First analysis
    r1 = tmp_project / "r1.tsv"
    write_tsv(r1, pd.DataFrame({"sample_id": ["SAMPLE_01"], "modkit_mean_meth": [0.7]}))
    res = _run_cli(
        "append",
        "--manifest", str(manifest),
        "--results", str(r1),
        "--key", "sample_id",
        "--analysis", "modkit",
        cwd=tmp_project,
    )
    assert res.returncode == 0, res.stderr

    # Second analysis
    r2 = tmp_project / "r2.tsv"
    write_tsv(r2, pd.DataFrame({"sample_id": ["SAMPLE_02"], "tldr_l1_count": [5]}))
    res = _run_cli(
        "append",
        "--manifest", str(manifest),
        "--results", str(r2),
        "--key", "sample_id",
        "--analysis", "tldr",
        cwd=tmp_project,
    )
    assert res.returncode == 0, res.stderr

    # Status (json)
    res = _run_cli("status", "--manifest", str(manifest), "--fmt", "json", cwd=tmp_project)
    assert res.returncode == 0, res.stderr
    import json
    data = json.loads(res.stdout)
    assert data["modkit"]["completed"] == 1
    assert data["tldr"]["completed"] == 1

    # Validate should pass
    res = _run_cli("validate", "--manifest", str(manifest), cwd=tmp_project)
    assert res.returncode == 0, res.stderr


def test_cli_no_command_prints_help_and_exits_nonzero(tmp_project: Path):
    res = _run_cli(cwd=tmp_project)
    assert res.returncode == 1
    assert "usage:" in res.stdout.lower() or "usage:" in res.stderr.lower()
