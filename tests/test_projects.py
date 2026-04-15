"""Tests for `casetrack projects`.

Builds a small hierarchy of manifests in different states and verifies the
cross-project summary.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from conftest import write_tsv


# ── helpers ────────────────────────────────────────────────────────────────────


def _init(manifest_path: Path, sample_ids: list):
    samples = manifest_path.parent / "samples.txt"
    samples.write_text("\n".join(sample_ids) + "\n")
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest_path), samples=str(samples),
        key="sample_id", metadata=None, cols=None, force=False,
    ))


def _append(manifest_path: Path, analysis: str, sample_ids: list):
    r = manifest_path.parent / f"r_{analysis}.tsv"
    write_tsv(
        r,
        pd.DataFrame({"sample_id": sample_ids, f"{analysis}_val": [0.5] * len(sample_ids)}),
    )
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(manifest_path), results=str(r),
        key="sample_id", analysis=analysis,
        overwrite=False, allow_new=False,
    ))


def _build_projects_tree(root: Path) -> dict:
    """Lay out three project dirs under `root` with known completion stats.
    Returns the per-project expectations for assertions.
    """
    # Project A: 4 samples, 1 analysis, 100% complete.
    a = root / "alzheimers_rnaseq"
    a.mkdir()
    _init(a / "manifest.tsv", ["S1", "S2", "S3", "S4"])
    _append(a / "manifest.tsv", "star", ["S1", "S2", "S3", "S4"])

    # Project B: 3 samples, 2 analyses, mixed completion.
    b = root / "brca_immune"
    b.mkdir()
    _init(b / "manifest.tsv", ["P1", "P2", "P3"])
    _append(b / "manifest.tsv", "maxquant", ["P1", "P2"])   # 2/3
    _append(b / "manifest.tsv", "netmhc",   ["P1"])         # 1/3

    # Project C: 2 samples, 0 analyses.
    c = root / "l1_mouse_ont"
    c.mkdir()
    _init(c / "manifest.tsv", ["M1", "M2"])

    return {
        "alzheimers_rnaseq": {"samples": 4, "analyses": 1, "pct": 100.0},
        "brca_immune":       {"samples": 3, "analyses": 2, "pct": 50.0},  # 3/6
        "l1_mouse_ont":      {"samples": 2, "analyses": 0, "pct": 0.0},
    }


def _proj_ns(root: Path, **overrides):
    defaults = dict(
        root=str(root),
        pattern="manifest.tsv",
        max_depth=4,
        key="sample_id",
        fmt="json",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── Core ───────────────────────────────────────────────────────────────────────


def test_projects_json_summary(tmp_path: Path, capsys):
    expected = _build_projects_tree(tmp_path)
    capsys.readouterr()

    casetrack.cmd_projects(_proj_ns(tmp_path, fmt="json"))
    data = json.loads(capsys.readouterr().out)
    by_name = {p["name"]: p for p in data}
    assert set(by_name) == set(expected)
    for name, exp in expected.items():
        assert by_name[name]["samples"] == exp["samples"]
        assert by_name[name]["analyses"] == exp["analyses"]
        assert by_name[name]["pct"] == exp["pct"]


def test_projects_tsv_summary(tmp_path: Path, capsys):
    _build_projects_tree(tmp_path)
    capsys.readouterr()
    casetrack.cmd_projects(_proj_ns(tmp_path, fmt="tsv"))
    out = capsys.readouterr().out.strip().splitlines()
    header = out[0].split("\t")
    assert header == ["project", "path", "samples", "analyses",
                      "completed_cells", "total_cells", "pct"]
    rows = {line.split("\t")[0]: line.split("\t") for line in out[1:]}
    assert rows["alzheimers_rnaseq"][2] == "4"   # samples
    assert rows["brca_immune"][3] == "2"         # analyses
    assert rows["l1_mouse_ont"][6] == "0.0"      # pct


def test_projects_table_renders(tmp_path: Path, capsys):
    _build_projects_tree(tmp_path)
    capsys.readouterr()
    casetrack.cmd_projects(_proj_ns(tmp_path, fmt="table"))
    out = capsys.readouterr().out
    assert "alzheimers_rnaseq" in out
    assert "100.0%" in out
    assert "3 project(s) under" in out


def test_projects_sorted_by_name(tmp_path: Path, capsys):
    _build_projects_tree(tmp_path)
    capsys.readouterr()
    casetrack.cmd_projects(_proj_ns(tmp_path, fmt="json"))
    data = json.loads(capsys.readouterr().out)
    names = [p["name"] for p in data]
    assert names == sorted(names)


# ── Filtering / skipping ───────────────────────────────────────────────────────


def test_projects_skips_hidden_and_sandbox(tmp_path: Path, capsys):
    _build_projects_tree(tmp_path)

    # Add a hidden dir with a manifest — must be skipped.
    hidden = tmp_path / ".archive"
    hidden.mkdir()
    _init(hidden / "manifest.tsv", ["H1"])

    # Add a sandbox dir with a manifest — must be skipped.
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _init(sandbox / "manifest.tsv", ["SB1"])

    capsys.readouterr()
    casetrack.cmd_projects(_proj_ns(tmp_path, fmt="json"))
    data = json.loads(capsys.readouterr().out)
    names = {p["name"] for p in data}
    assert ".archive" not in names
    assert "sandbox" not in names
    assert names == {"alzheimers_rnaseq", "brca_immune", "l1_mouse_ont"}


def test_projects_max_depth_respected(tmp_path: Path, capsys):
    # A nested project at depth 3.
    nested = tmp_path / "lab" / "group" / "deep_project"
    nested.mkdir(parents=True)
    _init(nested / "manifest.tsv", ["D1"])

    # Another at depth 1.
    shallow = tmp_path / "shallow_project"
    shallow.mkdir()
    _init(shallow / "manifest.tsv", ["S1"])

    capsys.readouterr()
    # max_depth=1 means only parent-of-file depth 1 => projects directly under root.
    # "manifest.tsv" under shallow_project has rel parts [shallow_project, manifest.tsv] → depth 1.
    # Nested rel parts have depth 3, which exceeds max_depth=1.
    casetrack.cmd_projects(_proj_ns(tmp_path, max_depth=1, fmt="json"))
    data = json.loads(capsys.readouterr().out)
    names = {p["name"] for p in data}
    assert names == {"shallow_project"}

    # Raising max_depth picks up the nested one too.
    capsys.readouterr()
    casetrack.cmd_projects(_proj_ns(tmp_path, max_depth=4, fmt="json"))
    data = json.loads(capsys.readouterr().out)
    names = {p["name"] for p in data}
    assert names == {"shallow_project", "deep_project"}


def test_projects_custom_pattern(tmp_path: Path, capsys):
    cohort = tmp_path / "my_cohort"
    cohort.mkdir()
    _init(cohort / "cohort.tsv", ["C1", "C2"])

    capsys.readouterr()
    casetrack.cmd_projects(_proj_ns(tmp_path, pattern="cohort.tsv", fmt="json"))
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["name"] == "my_cohort"
    assert data[0]["samples"] == 2


# ── Resilience ─────────────────────────────────────────────────────────────────


def test_projects_corrupted_manifest_warns_and_continues(tmp_path: Path, capsys):
    ok = tmp_path / "ok_project"
    ok.mkdir()
    _init(ok / "manifest.tsv", ["A", "B"])

    bad = tmp_path / "bad_project"
    bad.mkdir()
    # A "manifest" that pandas can't parse as TSV.
    (bad / "manifest.tsv").write_bytes(b"\x00\xff\x00\xff binary garbage \x00")

    capsys.readouterr()
    casetrack.cmd_projects(_proj_ns(tmp_path, fmt="json"))
    captured = capsys.readouterr()
    assert "Warning: failed to summarize" in captured.err
    data = json.loads(captured.out)
    names = {p["name"] for p in data}
    assert "ok_project" in names
    assert "bad_project" not in names


def test_projects_empty_root_message(tmp_path: Path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    capsys.readouterr()
    casetrack.cmd_projects(_proj_ns(empty, fmt="table"))
    out = capsys.readouterr().out
    assert "No manifests found" in out


def test_projects_invalid_root_exits(tmp_path: Path):
    with pytest.raises(SystemExit):
        casetrack.cmd_projects(_proj_ns(tmp_path / "does_not_exist", fmt="table"))


def test_projects_root_is_file_exits(tmp_path: Path):
    f = tmp_path / "not_a_dir"
    f.write_text("hello")
    with pytest.raises(SystemExit):
        casetrack.cmd_projects(_proj_ns(f, fmt="table"))


# ── CLI smoke ──────────────────────────────────────────────────────────────────


def test_projects_cli_smoke(tmp_path: Path):
    _build_projects_tree(tmp_path)
    res = subprocess.run(
        [sys.executable, str(Path(casetrack.__file__)), "projects",
         "--root", str(tmp_path), "--fmt", "json"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert {p["name"] for p in data} == {
        "alzheimers_rnaseq", "brca_immune", "l1_mouse_ont"
    }
