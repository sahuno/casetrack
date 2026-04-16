"""Tests for the v0.3 flat-manifest deprecation warning.

Every use of `--manifest` should emit a one-shot deprecation warning to
stderr. The warning is silenced by setting CASETRACK_NO_DEPRECATION=1.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import casetrack


@pytest.fixture(autouse=True)
def reset_warning_latch():
    """Clear the module-level once-per-process latch between tests."""
    casetrack._DEPRECATION_EMITTED = False
    yield
    casetrack._DEPRECATION_EMITTED = False


def _cli(*args, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the casetrack CLI as a subprocess so env / argv are fresh."""
    return subprocess.run(
        [sys.executable, str(Path(casetrack.__file__)), *args],
        capture_output=True, text=True, env=env,
    )


# ── Warning fires ─────────────────────────────────────────────────────────────


def test_warning_fires_on_flat_init(tmp_path: Path):
    samples = tmp_path / "s.txt"
    samples.write_text("S1\nS2\n")
    res = _cli("init", "--manifest", str(tmp_path / "m.tsv"), "--samples", str(samples))
    assert res.returncode == 0
    assert "DeprecationWarning" in res.stderr
    assert "--manifest" in res.stderr
    assert "--project-dir" in res.stderr


def test_warning_fires_on_flat_append(tmp_path: Path):
    samples = tmp_path / "s.txt"
    samples.write_text("S1\n")
    manifest = tmp_path / "m.tsv"
    _cli("init", "--manifest", str(manifest), "--samples", str(samples),
         env={"CASETRACK_NO_DEPRECATION": "1", "PATH": subprocess.os.environ.get("PATH", "")})

    results = tmp_path / "r.tsv"
    pd.DataFrame({"sample_id": ["S1"], "val": [1.0]}).to_csv(results, sep="\t", index=False)
    res = _cli("append", "--manifest", str(manifest),
               "--results", str(results), "--analysis", "a")
    assert res.returncode == 0
    assert "DeprecationWarning" in res.stderr


def test_warning_fires_on_flat_status(initialized_manifest: Path):
    res = _cli("status", "--manifest", str(initialized_manifest))
    assert res.returncode == 0
    assert "DeprecationWarning" in res.stderr


def test_warning_fires_on_flat_validate(initialized_manifest: Path):
    res = _cli("validate", "--manifest", str(initialized_manifest))
    assert res.returncode == 0
    assert "DeprecationWarning" in res.stderr


def test_warning_fires_on_flat_log(initialized_manifest: Path):
    res = _cli("log", "--manifest", str(initialized_manifest))
    assert res.returncode == 0
    assert "DeprecationWarning" in res.stderr


# ── Warning does NOT fire in project mode ─────────────────────────────────────


def test_no_warning_on_project_init(tmp_path: Path):
    proj = tmp_path / "proj"
    res = _cli("init", "--project-dir", str(proj))
    assert res.returncode == 0
    assert "DeprecationWarning" not in res.stderr


def test_no_warning_on_project_status(tmp_path: Path):
    proj = tmp_path / "proj"
    _cli("init", "--project-dir", str(proj))
    res = _cli("status", "--project-dir", str(proj))
    assert res.returncode == 0
    assert "DeprecationWarning" not in res.stderr


# ── Silencing ─────────────────────────────────────────────────────────────────


def test_env_var_silences_warning(tmp_path: Path):
    samples = tmp_path / "s.txt"
    samples.write_text("S1\n")
    res = _cli(
        "init", "--manifest", str(tmp_path / "m.tsv"), "--samples", str(samples),
        env={
            "CASETRACK_NO_DEPRECATION": "1",
            "PATH": subprocess.os.environ.get("PATH", ""),
        },
    )
    assert res.returncode == 0
    assert "DeprecationWarning" not in res.stderr


# ── Once-per-process latch ────────────────────────────────────────────────────


def test_warning_emitted_once_per_invocation(initialized_manifest: Path, capsys):
    """In-process: two flat-mode calls in the same Python session emit the
    warning once, not twice. Subprocess tests above cover the per-invocation
    case."""
    # `initialized_manifest` fixture also calls cmd_init (flat) which already
    # tripped the latch during setup. Reset it so this test can observe the
    # first emission in isolation.
    casetrack._DEPRECATION_EMITTED = False
    capsys.readouterr()  # drain any stderr from fixture setup

    ns = argparse.Namespace(
        manifest=str(initialized_manifest), project_dir=None,
        key="sample_id", analysis=None, group_by=None, fmt="table",
    )
    casetrack.cmd_status(ns)
    first_err = capsys.readouterr().err
    casetrack.cmd_status(ns)
    second_err = capsys.readouterr().err
    assert first_err.count("DeprecationWarning") == 1
    assert "DeprecationWarning" not in second_err
