"""Tests for git state capture in provenance logging.

Each `log_provenance` entry records the git commit, branch, and dirty-tree
flag of the process CWD (or null if not in a repo / git missing / opted out).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from conftest import write_tsv


HEX40 = re.compile(r"^[0-9a-f]{40}$")


# ── fixtures ───────────────────────────────────────────────────────────────────


def _git(*args, cwd: Path, env=None):
    """Run a git command with deterministic identity."""
    base_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=base_env,
        check=True,
    )


@pytest.fixture
def clean_git_cache():
    """Ensure the per-process _GIT_STATE_CACHE does not leak across tests."""
    casetrack._GIT_STATE_CACHE.clear()
    yield
    casetrack._GIT_STATE_CACHE.clear()


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """A freshly initialized git repo with one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    (repo / "README").write_text("hello\n")
    _git("add", "README", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


@pytest.fixture
def not_a_repo(tmp_path: Path, monkeypatch) -> Path:
    """A directory that is NOT a git repo; block upward traversal so git
    cannot latch onto an ancestor repo (e.g. the casetrack checkout)."""
    d = tmp_path / "no_repo"
    d.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return d


# ── _git_state direct ──────────────────────────────────────────────────────────


def test_git_state_in_repo(tmp_git_repo: Path, clean_git_cache):
    state = casetrack._git_state(cwd=str(tmp_git_repo), use_cache=False)
    assert state is not None
    assert HEX40.match(state["commit"])
    assert state["branch"] == "main"
    assert state["dirty"] is False
    assert Path(state["toplevel"]).resolve() == tmp_git_repo.resolve()


def test_git_state_detects_dirty(tmp_git_repo: Path, clean_git_cache):
    (tmp_git_repo / "scratch.txt").write_text("work in progress")
    state = casetrack._git_state(cwd=str(tmp_git_repo), use_cache=False)
    assert state["dirty"] is True


def test_git_state_outside_repo(not_a_repo: Path, clean_git_cache):
    state = casetrack._git_state(cwd=str(not_a_repo), use_cache=False)
    assert state is None


def test_git_state_respects_env_opt_out(tmp_git_repo: Path,
                                        monkeypatch: pytest.MonkeyPatch,
                                        clean_git_cache):
    monkeypatch.setenv("CASETRACK_NO_GIT", "1")
    assert casetrack._git_state(cwd=str(tmp_git_repo), use_cache=False) is None


def test_git_state_missing_git_binary(tmp_git_repo: Path,
                                      monkeypatch: pytest.MonkeyPatch,
                                      tmp_path: Path, clean_git_cache):
    """Empty PATH → FileNotFoundError → _git_state returns None gracefully."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty_bin"))
    assert casetrack._git_state(cwd=str(tmp_git_repo), use_cache=False) is None


def test_git_state_cache_returns_same_object(tmp_git_repo: Path, clean_git_cache):
    first = casetrack._git_state(cwd=str(tmp_git_repo))
    # Modify the working tree, but cached value is still returned.
    (tmp_git_repo / "x").write_text("x")
    cached = casetrack._git_state(cwd=str(tmp_git_repo))
    assert cached == first
    # use_cache=False forces a re-check and now reflects dirty state.
    fresh = casetrack._git_state(cwd=str(tmp_git_repo), use_cache=False)
    assert fresh["dirty"] is True


# ── Integration with log_provenance / append ───────────────────────────────────


def test_log_provenance_includes_git_field(tmp_path: Path, clean_git_cache):
    manifest = tmp_path / "m.tsv"
    casetrack.log_provenance(str(manifest), {"action": "noop"})
    entry = json.loads(
        Path(str(manifest) + casetrack.PROVENANCE_SUFFIX).read_text().strip()
    )
    # "git" is always present — value is dict or null. We run from the
    # casetrack repo in CI/local; either case is acceptable for the key.
    assert "git" in entry


def test_append_records_git_state_in_repo(tmp_git_repo: Path, samples_file: Path,
                                          monkeypatch: pytest.MonkeyPatch,
                                          clean_git_cache):
    """Running `cmd_append` from inside a repo captures its HEAD."""
    # Put the manifest and samples file inside the repo, and run from there.
    shutil.copy(samples_file, tmp_git_repo / "samples.txt")
    monkeypatch.chdir(tmp_git_repo)

    manifest = tmp_git_repo / "manifest.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(tmp_git_repo / "samples.txt"),
        key="sample_id", metadata=None, cols=None, force=False,
    ))

    r = tmp_git_repo / "r.tsv"
    write_tsv(r, pd.DataFrame({"sample_id": ["SAMPLE_01"], "v": [0.5]}))
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(manifest), results=str(r),
        key="sample_id", analysis="test",
        overwrite=False, allow_new=False,
    ))

    prov_lines = Path(str(manifest) + casetrack.PROVENANCE_SUFFIX) \
        .read_text().splitlines()
    append_entry = [
        json.loads(l) for l in prov_lines if json.loads(l).get("action") == "append"
    ][0]
    git = append_entry["git"]
    assert git is not None
    assert HEX40.match(git["commit"])
    assert git["branch"] == "main"
    # Dirty is True because cmd_init itself just wrote manifest.tsv into the
    # repo — that's exactly the kind of drift we want the flag to surface.
    assert git["dirty"] is True
    assert Path(git["toplevel"]).resolve() == tmp_git_repo.resolve()


def test_append_git_null_when_opted_out(tmp_git_repo: Path, samples_file: Path,
                                        monkeypatch: pytest.MonkeyPatch,
                                        clean_git_cache):
    monkeypatch.setenv("CASETRACK_NO_GIT", "1")
    shutil.copy(samples_file, tmp_git_repo / "samples.txt")
    monkeypatch.chdir(tmp_git_repo)
    manifest = tmp_git_repo / "manifest.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(tmp_git_repo / "samples.txt"),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    prov_line = Path(str(manifest) + casetrack.PROVENANCE_SUFFIX) \
        .read_text().strip().splitlines()[0]
    assert json.loads(prov_line)["git"] is None


def test_dashboard_surfaces_git_commit(tmp_git_repo: Path, samples_file: Path,
                                       monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Path, clean_git_cache):
    """The rendered dashboard should show a short commit hash in the timeline."""
    shutil.copy(samples_file, tmp_git_repo / "samples.txt")
    monkeypatch.chdir(tmp_git_repo)
    manifest = tmp_git_repo / "manifest.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(tmp_git_repo / "samples.txt"),
        key="sample_id", metadata=None, cols=None, force=False,
    ))

    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(argparse.Namespace(
        manifest=str(manifest), output=str(out), key="sample_id",
    ))
    doc = out.read_text()

    # Fetch the real commit to compare against.
    real = _git("rev-parse", "HEAD", cwd=tmp_git_repo).stdout.strip()[:8]
    assert real in doc
    assert "@main" in doc
