"""Tests for examples/claude/post_analysis_hook.sh.

We stub the `claude` CLI with a bash script on PATH that emits whatever
TSV we need, and invoke the hook end-to-end. This keeps the contract
between the hook, the prompt template, and casetrack locked.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from conftest import write_tsv


REPO_ROOT = Path(casetrack.__file__).resolve().parent
HOOK = REPO_ROOT / "examples" / "claude" / "post_analysis_hook.sh"
PROMPT = REPO_ROOT / "examples" / "claude" / "qc_review_prompt.md"


# ── static checks ──────────────────────────────────────────────────────────────


def test_hook_exists_and_is_executable():
    assert HOOK.is_file()
    assert HOOK.stat().st_mode & stat.S_IXUSR, "hook must be executable"


def test_prompt_template_present_and_has_placeholders():
    assert PROMPT.is_file()
    body = PROMPT.read_text()
    for token in ("__SAMPLE_ID__", "__ANALYSIS__", "__RESULTS_TSV__"):
        assert token in body, f"prompt missing placeholder {token}"


def test_hook_bash_syntax_valid():
    res = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


# ── helpers ────────────────────────────────────────────────────────────────────


def _install_stub_claude(tmp_path: Path, *, stdout_body: str,
                         exit_code: int = 0) -> Path:
    """Write a bash script that prints `stdout_body` and exits with
    `exit_code`, and put its dir first on PATH. Returns the bin dir."""
    bin_dir = tmp_path / "stub_bin"
    bin_dir.mkdir(exist_ok=True)
    # Escape single quotes for embedding in the heredoc-less form.
    safe_body = stdout_body.replace("'", "'\"'\"'")
    stub = bin_dir / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        # Ignore all args; emit fixed body.
        f"printf '%s' '{safe_body}'\n"
        f"exit {exit_code}\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run_hook(env: dict, *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        env=env, cwd=str(cwd),
        capture_output=True, text=True,
    )


def _base_env(tmp_path: Path, manifest: Path, results: Path,
              stub_bin: Path, sample: str, analysis: str) -> dict:
    # Point CASETRACK_BIN at our local casetrack.py via python; keep the
    # stub claude first on PATH.
    casetrack_cmd = f"{sys.executable} {REPO_ROOT / 'casetrack.py'}"
    # We wrap the casetrack invocation in a tiny shim since the hook runs
    # "$CASETRACK_BIN" as a single argv element. Using a script keeps it
    # simple.
    shim = tmp_path / "stub_bin" / "casetrack_shim"
    shim.parent.mkdir(exist_ok=True)
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'exec {casetrack_cmd} "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return {
        **os.environ,
        "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "SAMPLE_ID": sample,
        "ANALYSIS": analysis,
        "MANIFEST": str(manifest),
        "RESULTS_TSV": str(results),
        "CASETRACK_BIN": str(shim),
        "REVIEW_DIR": str(tmp_path),
        "PROMPT_FILE": str(PROMPT),
    }


# ── end-to-end happy path ─────────────────────────────────────────────────────


def test_hook_appends_qc_columns(tmp_project: Path, samples_file: Path,
                                 tmp_path: Path):
    manifest = tmp_project / "manifest.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples_file),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    # Seed a modkit append so the main analysis column exists.
    r = tmp_project / "r.tsv"
    write_tsv(
        r, pd.DataFrame({"sample_id": ["SAMPLE_01"], "modkit_mean_meth": [0.72]}),
    )
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(manifest), results=str(r),
        key="sample_id", analysis="modkit",
        overwrite=False, allow_new=False,
    ))

    # Stub claude emits a well-formed review TSV for SAMPLE_01 / modkit.
    review = "sample_id\tcc_modkit_qc_pass\tcc_modkit_qc_note\nSAMPLE_01\tTrue\tmethylation in expected range\n"
    stub_bin = _install_stub_claude(tmp_path, stdout_body=review)

    env = _base_env(tmp_path, manifest, r, stub_bin, "SAMPLE_01", "modkit")
    res = _run_hook(env, cwd=tmp_project)
    assert res.returncode == 0, res.stderr

    df = pd.read_csv(manifest, sep="\t").set_index("sample_id")
    assert df.loc["SAMPLE_01", "cc_modkit_qc_pass"] == True  # noqa: E712
    assert "methylation" in df.loc["SAMPLE_01", "cc_modkit_qc_note"]
    assert pd.notna(df.loc["SAMPLE_01", "cc_modkit_review_done"])

    # Provenance got an append action for the review.
    prov = (manifest.parent / (manifest.name + casetrack.PROVENANCE_SUFFIX)).read_text()
    lines = [json.loads(l) for l in prov.strip().splitlines()]
    analyses = [e.get("analysis") for e in lines if e.get("action") == "append"]
    assert "cc_modkit_review" in analyses


# ── failure modes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", ["SAMPLE_ID", "ANALYSIS", "MANIFEST", "RESULTS_TSV"])
def test_hook_missing_env_fails(tmp_project: Path, samples_file: Path,
                                tmp_path: Path, missing: str):
    manifest = tmp_project / "m.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples_file),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    r = tmp_project / "r.tsv"
    write_tsv(r, pd.DataFrame({"sample_id": ["SAMPLE_01"], "v": [1.0]}))
    stub_bin = _install_stub_claude(tmp_path, stdout_body="irrelevant")

    env = _base_env(tmp_path, manifest, r, stub_bin, "SAMPLE_01", "modkit")
    del env[missing]
    res = _run_hook(env, cwd=tmp_project)
    assert res.returncode != 0
    assert missing in res.stderr


def test_hook_claude_nonzero_exit(tmp_project: Path, samples_file: Path, tmp_path: Path):
    manifest = tmp_project / "m.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples_file),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    r = tmp_project / "r.tsv"
    write_tsv(r, pd.DataFrame({"sample_id": ["SAMPLE_01"], "v": [1.0]}))
    stub_bin = _install_stub_claude(tmp_path, stdout_body="boom", exit_code=2)

    env = _base_env(tmp_path, manifest, r, stub_bin, "SAMPLE_01", "modkit")
    res = _run_hook(env, cwd=tmp_project)
    assert res.returncode == 3, res.stderr


def test_hook_bad_header_rejected(tmp_project: Path, samples_file: Path, tmp_path: Path):
    manifest = tmp_project / "m.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples_file),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    r = tmp_project / "r.tsv"
    write_tsv(r, pd.DataFrame({"sample_id": ["SAMPLE_01"], "v": [1.0]}))

    # Wrong header (columns named incorrectly).
    bad = "sample_id\tqc_pass\tqc_note\nSAMPLE_01\tTrue\tok\n"
    stub_bin = _install_stub_claude(tmp_path, stdout_body=bad)

    env = _base_env(tmp_path, manifest, r, stub_bin, "SAMPLE_01", "modkit")
    res = _run_hook(env, cwd=tmp_project)
    assert res.returncode == 4, res.stderr
    # Manifest untouched — no cc_modkit_qc_pass column should exist.
    df = pd.read_csv(manifest, sep="\t")
    assert "cc_modkit_qc_pass" not in df.columns


def test_hook_empty_review_rejected(tmp_project: Path, samples_file: Path, tmp_path: Path):
    manifest = tmp_project / "m.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples_file),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    r = tmp_project / "r.tsv"
    write_tsv(r, pd.DataFrame({"sample_id": ["SAMPLE_01"], "v": [1.0]}))

    # Header only, no data rows.
    body = "sample_id\tcc_modkit_qc_pass\tcc_modkit_qc_note\n"
    stub_bin = _install_stub_claude(tmp_path, stdout_body=body)

    env = _base_env(tmp_path, manifest, r, stub_bin, "SAMPLE_01", "modkit")
    res = _run_hook(env, cwd=tmp_project)
    assert res.returncode == 5, res.stderr


def test_hook_prompt_substitution_happens(tmp_project: Path, samples_file: Path,
                                          tmp_path: Path):
    """The stub captures the prompt it was invoked with; verify all three
    placeholders were substituted before the LLM was called."""
    manifest = tmp_project / "m.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples_file),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    r = tmp_project / "SAMPLE_01_modkit.tsv"
    write_tsv(r, pd.DataFrame({"sample_id": ["SAMPLE_01"], "v": [1.0]}))

    bin_dir = tmp_path / "stub_bin"
    bin_dir.mkdir()
    prompt_log = tmp_path / "captured_prompt.txt"
    # This stub writes the second arg (the prompt body) to prompt_log,
    # then emits a valid review.
    stub = bin_dir / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        # The hook calls: claude --print "$prompt"
        # $1='--print', $2=<prompt body>
        f'printf "%s" "$2" > {prompt_log}\n'
        "printf 'sample_id\\tcc_modkit_qc_pass\\tcc_modkit_qc_note\\n"
        "SAMPLE_01\\tTrue\\tok\\n'\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    env = _base_env(tmp_path, manifest, r, bin_dir, "SAMPLE_01", "modkit")
    res = _run_hook(env, cwd=tmp_project)
    assert res.returncode == 0, res.stderr

    captured = prompt_log.read_text()
    assert "__SAMPLE_ID__" not in captured
    assert "__ANALYSIS__" not in captured
    assert "__RESULTS_TSV__" not in captured
    assert "SAMPLE_01" in captured
    assert "modkit" in captured
    assert str(r) in captured
