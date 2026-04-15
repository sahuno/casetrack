"""Tests for `casetrack rerun`.

Uses a stub `sbatch` script placed on PATH so we can exercise the subprocess
submission path without needing a real SLURM cluster.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from conftest import write_tsv


# ── helpers ────────────────────────────────────────────────────────────────────


def _append_modkit(initialized_manifest: Path, tmp_project: Path, sample_ids):
    """Mark `sample_ids` as completed for the 'modkit' analysis."""
    r = tmp_project / f"r_{'_'.join(sample_ids)}.tsv"
    write_tsv(
        r,
        pd.DataFrame(
            {
                "sample_id": list(sample_ids),
                "modkit_mean_meth": [0.5] * len(sample_ids),
            }
        ),
    )
    ns = argparse.Namespace(
        manifest=str(initialized_manifest),
        results=str(r),
        key="sample_id",
        analysis="modkit",
        overwrite=False,
        allow_new=False,
    )
    casetrack.cmd_append(ns)


def _rerun_ns(initialized_manifest: Path, script: str = "run.sh", **overrides):
    defaults = dict(
        manifest=str(initialized_manifest),
        analysis="modkit",
        script=script,
        key="sample_id",
        submit=False,
        list_only=False,
        extra=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _install_fake_sbatch(tmp_path: Path, monkeypatch, *, exit_code: int = 0,
                         stderr: str = "") -> Path:
    """Create a fake `sbatch` on PATH that logs its argv and emits a job id."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = bin_dir / "sbatch.log"

    if exit_code == 0:
        script = (
            "#!/usr/bin/env bash\n"
            f'echo "$@" >> "{log_file}"\n'
            # Deterministic-ish id per call: count lines in the log.
            f'n=$(wc -l < "{log_file}")\n'
            'id=$((1000 + n))\n'
            'echo "Submitted batch job $id"\n'
        )
    else:
        script = (
            "#!/usr/bin/env bash\n"
            f'echo "$@" >> "{log_file}"\n'
            f'echo "{stderr}" >&2\n'
            f"exit {exit_code}\n"
        )

    sbatch = bin_dir / "sbatch"
    sbatch.write_text(script)
    sbatch.chmod(sbatch.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log_file


# ── Dry-run / listing behavior ─────────────────────────────────────────────────


def test_rerun_all_complete_prints_nothing_to_do(
    initialized_manifest: Path, tmp_project: Path, capsys
):
    _append_modkit(
        initialized_manifest, tmp_project,
        ["SAMPLE_01", "SAMPLE_02", "SAMPLE_03", "SAMPLE_04", "SAMPLE_05"],
    )
    capsys.readouterr()
    casetrack.cmd_rerun(_rerun_ns(initialized_manifest))
    out = capsys.readouterr().out
    assert "Nothing to do" in out


def test_rerun_prints_sbatch_commands_for_missing(
    initialized_manifest: Path, tmp_project: Path, capsys
):
    _append_modkit(initialized_manifest, tmp_project, ["SAMPLE_01", "SAMPLE_02"])
    capsys.readouterr()
    casetrack.cmd_rerun(_rerun_ns(initialized_manifest, script="run_modkit.sh"))
    captured = capsys.readouterr()
    # Summary goes to stderr, commands to stdout.
    assert "3 sample(s) incomplete" in captured.err
    lines = [l for l in captured.out.strip().splitlines() if l]
    assert len(lines) == 3
    manifest_abs = os.path.abspath(str(initialized_manifest))
    expected = {
        f"sbatch run_modkit.sh SAMPLE_03 {manifest_abs}",
        f"sbatch run_modkit.sh SAMPLE_04 {manifest_abs}",
        f"sbatch run_modkit.sh SAMPLE_05 {manifest_abs}",
    }
    assert set(lines) == expected


def test_rerun_no_done_column_treats_all_as_incomplete(
    initialized_manifest: Path, capsys
):
    # No modkit append has happened — modkit_done doesn't exist.
    casetrack.cmd_rerun(_rerun_ns(initialized_manifest, script="run_modkit.sh"))
    captured = capsys.readouterr()
    assert "no 'modkit_done' column yet" in captured.err
    lines = [l for l in captured.out.strip().splitlines() if l]
    assert len(lines) == 5  # all 5 samples


def test_rerun_list_only_prints_bare_ids(
    initialized_manifest: Path, tmp_project: Path, capsys
):
    _append_modkit(initialized_manifest, tmp_project, ["SAMPLE_01"])
    capsys.readouterr()
    casetrack.cmd_rerun(_rerun_ns(initialized_manifest, list_only=True))
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["SAMPLE_02", "SAMPLE_03", "SAMPLE_04", "SAMPLE_05"]


def test_rerun_extra_args_append(initialized_manifest: Path, capsys):
    casetrack.cmd_rerun(
        _rerun_ns(initialized_manifest, script="run.sh", extra="--partition gpu --time 01:00:00")
    )
    lines = [l for l in capsys.readouterr().out.strip().splitlines() if l]
    assert all(line.endswith("--partition gpu --time 01:00:00") for line in lines)


def test_rerun_missing_manifest_exits(tmp_project: Path):
    ns = argparse.Namespace(
        manifest=str(tmp_project / "nope.tsv"),
        analysis="modkit",
        script="run.sh",
        key="sample_id",
        submit=False,
        list_only=False,
        extra=None,
    )
    with pytest.raises(SystemExit):
        casetrack.cmd_rerun(ns)


# ── --submit with stub sbatch ──────────────────────────────────────────────────


def test_rerun_submit_invokes_sbatch(
    initialized_manifest: Path, tmp_project: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys
):
    _append_modkit(initialized_manifest, tmp_project, ["SAMPLE_01", "SAMPLE_02"])
    capsys.readouterr()

    log_file = _install_fake_sbatch(tmp_path, monkeypatch)
    casetrack.cmd_rerun(_rerun_ns(initialized_manifest, script="run.sh", submit=True))

    captured = capsys.readouterr()
    # Stdout should show "Submitted <sid>: SLURM <id>" lines.
    lines = [l for l in captured.out.splitlines() if l.startswith("Submitted ")]
    assert len(lines) == 3
    for sid in ("SAMPLE_03", "SAMPLE_04", "SAMPLE_05"):
        assert any(sid in l for l in lines)

    # Fake sbatch logged each invocation.
    logged = log_file.read_text().strip().splitlines()
    assert len(logged) == 3
    manifest_abs = os.path.abspath(str(initialized_manifest))
    for line in logged:
        parts = line.split()
        assert parts[0] == "run.sh"
        assert parts[1] in {"SAMPLE_03", "SAMPLE_04", "SAMPLE_05"}
        assert parts[2] == manifest_abs

    # Provenance log captured job IDs.
    prov_lines = Path(str(initialized_manifest) + casetrack.PROVENANCE_SUFFIX) \
        .read_text().splitlines()
    rerun_entries = [json.loads(l) for l in prov_lines if json.loads(l).get("action") == "rerun"]
    assert len(rerun_entries) == 1
    entry = rerun_entries[0]
    assert entry["analysis"] == "modkit"
    assert entry["n_submitted"] == 3
    assert entry["n_failed"] == 0
    job_ids = {s["job_id"] for s in entry["submitted"]}
    assert all(jid.isdigit() for jid in job_ids)


def test_rerun_submit_sbatch_missing_exits(
    initialized_manifest: Path, tmp_project: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch
):
    _append_modkit(initialized_manifest, tmp_project, ["SAMPLE_01"])
    # Empty PATH so sbatch cannot be found.
    monkeypatch.setenv("PATH", str(tmp_path / "nonexistent_dir"))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_rerun(_rerun_ns(initialized_manifest, submit=True))
    assert excinfo.value.code == 1


def test_rerun_submit_sbatch_failures_recorded(
    initialized_manifest: Path, tmp_project: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys
):
    _append_modkit(initialized_manifest, tmp_project, ["SAMPLE_01", "SAMPLE_02", "SAMPLE_03", "SAMPLE_04"])
    capsys.readouterr()

    _install_fake_sbatch(tmp_path, monkeypatch, exit_code=1, stderr="sbatch: invalid partition")

    # Only SAMPLE_05 is incomplete; sbatch always fails.
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_rerun(_rerun_ns(initialized_manifest, submit=True))
    assert excinfo.value.code == 1

    captured = capsys.readouterr()
    assert "FAIL SAMPLE_05" in captured.err

    prov_lines = Path(str(initialized_manifest) + casetrack.PROVENANCE_SUFFIX) \
        .read_text().splitlines()
    rerun_entries = [json.loads(l) for l in prov_lines if json.loads(l).get("action") == "rerun"]
    assert rerun_entries[-1]["n_failed"] == 1
    assert rerun_entries[-1]["n_submitted"] == 0
    assert rerun_entries[-1]["failed"][0]["sample_id"] == "SAMPLE_05"


# ── CLI subprocess smoke ───────────────────────────────────────────────────────


def test_rerun_cli_dry_run(tmp_project: Path, samples_file: Path):
    """Hit the CLI entry point to confirm argparse wiring for the new subcommand."""
    import subprocess
    import sys

    manifest = tmp_project / "manifest.tsv"
    # init
    subprocess.run(
        [sys.executable, str(Path(casetrack.__file__)), "init",
         "--manifest", str(manifest), "--samples", str(samples_file)],
        check=True, capture_output=True, text=True,
    )

    # rerun dry-run for an analysis that has never been appended
    res = subprocess.run(
        [sys.executable, str(Path(casetrack.__file__)), "rerun",
         "--manifest", str(manifest), "--analysis", "tldr", "--script", "run_tldr.sh"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    lines = [l for l in res.stdout.strip().splitlines() if l]
    assert len(lines) == 5
    for line in lines:
        assert line.startswith("sbatch run_tldr.sh ")
