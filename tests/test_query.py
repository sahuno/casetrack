"""Tests for `casetrack query` (DuckDB-backed SQL over manifests).

Covers single-manifest queries, cross-project queries via --root (including
schemas that differ between projects), output formats, the --as alias, the
--output file path, the friendly install-hint when duckdb is absent, bad
SQL, and CLI smoke.

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


duckdb = pytest.importorskip("duckdb")


# ── helpers ────────────────────────────────────────────────────────────────────


def _query_ns(sql: str, **overrides):
    defaults = dict(
        manifest=None,
        root=None,
        sql=sql,
        as_name=None,
        pattern="manifest.tsv",
        max_depth=4,
        fmt="json",
        output=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _seed_manifest(tmp_project: Path, samples_file: Path) -> Path:
    """A manifest with two appended analyses, so the test has real data."""
    manifest = tmp_project / "manifest.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples_file),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    r1 = tmp_project / "r1.tsv"
    write_tsv(r1, pd.DataFrame({
        "sample_id": ["SAMPLE_01", "SAMPLE_02", "SAMPLE_03"],
        "modkit_mean_meth": [0.72, 0.81, 0.55],
    }))
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(manifest), results=str(r1),
        key="sample_id", analysis="modkit",
        overwrite=False, allow_new=False, yes=False,
    ))
    r2 = tmp_project / "r2.tsv"
    write_tsv(r2, pd.DataFrame({
        "sample_id": ["SAMPLE_01", "SAMPLE_02"],
        "tldr_l1_count": [14, 3],
    }))
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(manifest), results=str(r2),
        key="sample_id", analysis="tldr",
        overwrite=False, allow_new=False, yes=False,
    ))
    return manifest


# ── single-manifest queries ────────────────────────────────────────────────────


def test_query_simple_select(tmp_project, samples_file, capsys):
    manifest = _seed_manifest(tmp_project, samples_file)
    capsys.readouterr()
    casetrack.cmd_query(_query_ns(
        "SELECT sample_id, modkit_mean_meth FROM _ ORDER BY sample_id",
        manifest=str(manifest), fmt="json",
    ))
    data = json.loads(capsys.readouterr().out)
    # SAMPLE_01 / 02 / 03 have modkit values; 04 / 05 have NaN
    ids_with_values = [r["sample_id"] for r in data if r["modkit_mean_meth"] is not None]
    assert ids_with_values == ["SAMPLE_01", "SAMPLE_02", "SAMPLE_03"]


def test_query_where_clause(tmp_project, samples_file, capsys):
    manifest = _seed_manifest(tmp_project, samples_file)
    capsys.readouterr()
    casetrack.cmd_query(_query_ns(
        "SELECT sample_id FROM _ WHERE modkit_mean_meth > 0.7",
        manifest=str(manifest), fmt="json",
    ))
    data = json.loads(capsys.readouterr().out)
    assert {r["sample_id"] for r in data} == {"SAMPLE_01", "SAMPLE_02"}


def test_query_aggregate(tmp_project, samples_file, capsys):
    manifest = _seed_manifest(tmp_project, samples_file)
    capsys.readouterr()
    casetrack.cmd_query(_query_ns(
        "SELECT COUNT(*) AS n, AVG(modkit_mean_meth) AS avg_meth FROM _",
        manifest=str(manifest), fmt="json",
    ))
    row = json.loads(capsys.readouterr().out)[0]
    assert row["n"] == 5
    # (0.72 + 0.81 + 0.55) / 3 ≈ 0.693
    assert 0.69 < row["avg_meth"] < 0.70


def test_query_as_alias(tmp_project, samples_file, capsys):
    manifest = _seed_manifest(tmp_project, samples_file)
    capsys.readouterr()
    casetrack.cmd_query(_query_ns(
        "SELECT COUNT(*) AS n FROM manifest",
        manifest=str(manifest), as_name="manifest", fmt="json",
    ))
    row = json.loads(capsys.readouterr().out)[0]
    assert row["n"] == 5


# ── output formats ────────────────────────────────────────────────────────────


def test_query_tsv_format(tmp_project, samples_file, capsys):
    manifest = _seed_manifest(tmp_project, samples_file)
    capsys.readouterr()
    casetrack.cmd_query(_query_ns(
        "SELECT sample_id, modkit_mean_meth FROM _ WHERE modkit_mean_meth IS NOT NULL ORDER BY sample_id",
        manifest=str(manifest), fmt="tsv",
    ))
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "sample_id\tmodkit_mean_meth"
    assert out[1].split("\t")[0] == "SAMPLE_01"


def test_query_csv_format(tmp_project, samples_file, capsys):
    manifest = _seed_manifest(tmp_project, samples_file)
    capsys.readouterr()
    casetrack.cmd_query(_query_ns(
        "SELECT sample_id FROM _ LIMIT 2",
        manifest=str(manifest), fmt="csv",
    ))
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "sample_id"
    assert len(out) == 3  # header + 2 rows


def test_query_table_format_default(tmp_project, samples_file, capsys):
    manifest = _seed_manifest(tmp_project, samples_file)
    capsys.readouterr()
    casetrack.cmd_query(_query_ns(
        "SELECT sample_id FROM _ ORDER BY sample_id LIMIT 3",
        manifest=str(manifest), fmt="table",
    ))
    out = capsys.readouterr().out
    assert "sample_id" in out
    assert "SAMPLE_01" in out
    assert "(3 rows)" in out


def test_query_output_to_file(tmp_project, samples_file, tmp_path):
    manifest = _seed_manifest(tmp_project, samples_file)
    out = tmp_path / "q.json"
    casetrack.cmd_query(_query_ns(
        "SELECT sample_id FROM _ ORDER BY sample_id LIMIT 1",
        manifest=str(manifest), fmt="json", output=str(out),
    ))
    assert out.exists()
    data = json.loads(out.read_text())
    assert data[0]["sample_id"] == "SAMPLE_01"


# ── cross-project (--root) ─────────────────────────────────────────────────────


def _build_multi_project(root: Path):
    """Two projects with overlapping and non-overlapping columns, to exercise
    UNION ALL BY NAME handling."""
    p1 = root / "proj_alpha"
    p1.mkdir()
    s1 = p1 / "samples.txt"; s1.write_text("A1\nA2\n")
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(p1 / "manifest.tsv"), samples=str(s1),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    r1 = p1 / "r.tsv"
    write_tsv(r1, pd.DataFrame({"sample_id": ["A1", "A2"], "modkit_mean": [0.1, 0.2]}))
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(p1 / "manifest.tsv"), results=str(r1),
        key="sample_id", analysis="modkit",
        overwrite=False, allow_new=False, yes=False,
    ))

    p2 = root / "proj_beta"
    p2.mkdir()
    s2 = p2 / "samples.txt"; s2.write_text("B1\nB2\nB3\n")
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(p2 / "manifest.tsv"), samples=str(s2),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    # proj_beta has a column proj_alpha doesn't: tldr_l1_count
    r2 = p2 / "r.tsv"
    write_tsv(r2, pd.DataFrame({"sample_id": ["B1", "B2", "B3"], "tldr_l1_count": [5, 10, 15]}))
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(p2 / "manifest.tsv"), results=str(r2),
        key="sample_id", analysis="tldr",
        overwrite=False, allow_new=False, yes=False,
    ))


def test_query_root_group_by_project(tmp_path, capsys):
    _build_multi_project(tmp_path)
    capsys.readouterr()
    casetrack.cmd_query(_query_ns(
        "SELECT project, COUNT(*) AS n FROM _ GROUP BY project ORDER BY project",
        root=str(tmp_path), fmt="json",
    ))
    data = json.loads(capsys.readouterr().out)
    by_proj = {r["project"]: r["n"] for r in data}
    assert by_proj == {"proj_alpha": 2, "proj_beta": 3}


def test_query_root_handles_schema_divergence(tmp_path, capsys):
    """Column that exists in only one project must come back NULL for the other."""
    _build_multi_project(tmp_path)
    capsys.readouterr()
    casetrack.cmd_query(_query_ns(
        "SELECT project, sample_id, tldr_l1_count FROM _ WHERE tldr_l1_count IS NOT NULL ORDER BY sample_id",
        root=str(tmp_path), fmt="json",
    ))
    data = json.loads(capsys.readouterr().out)
    projs = {r["project"] for r in data}
    # Only proj_beta has tldr_l1_count populated.
    assert projs == {"proj_beta"}
    assert len(data) == 3


def test_query_root_empty_exits_with_message(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit):
        casetrack.cmd_query(_query_ns(
            "SELECT 1", root=str(empty), fmt="json",
        ))
    err = capsys.readouterr().err
    assert "No manifests found" in err


# ── error paths ────────────────────────────────────────────────────────────────


def test_query_missing_manifest_exits(tmp_project):
    with pytest.raises(SystemExit):
        casetrack.cmd_query(_query_ns(
            "SELECT 1",
            manifest=str(tmp_project / "nope.tsv"), fmt="json",
        ))


def test_query_missing_root_exits(tmp_path):
    with pytest.raises(SystemExit):
        casetrack.cmd_query(_query_ns(
            "SELECT 1", root=str(tmp_path / "does_not_exist"), fmt="json",
        ))


def test_query_requires_exactly_one_target(tmp_project, samples_file):
    """Both --manifest and --root → exits. Neither → exits."""
    manifest = _seed_manifest(tmp_project, samples_file)
    with pytest.raises(SystemExit):
        casetrack.cmd_query(_query_ns(
            "SELECT 1", manifest=str(manifest), root=str(tmp_project),
            fmt="json",
        ))
    with pytest.raises(SystemExit):
        casetrack.cmd_query(_query_ns("SELECT 1", fmt="json"))


def test_query_bad_sql_exit_2(tmp_project, samples_file, capsys):
    manifest = _seed_manifest(tmp_project, samples_file)
    capsys.readouterr()
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_query(_query_ns(
            "SELECT this_column_does_not_exist FROM _",
            manifest=str(manifest), fmt="json",
        ))
    assert excinfo.value.code == 2
    assert "SQL failed" in capsys.readouterr().err


def test_query_missing_duckdb_friendly_error(
    tmp_project, samples_file, monkeypatch, capsys
):
    """When duckdb isn't installed, casetrack query must print a helpful
    install hint, not a traceback."""
    manifest = _seed_manifest(tmp_project, samples_file)
    monkeypatch.setitem(sys.modules, "duckdb", None)  # future import raises
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_query(_query_ns(
            "SELECT 1", manifest=str(manifest), fmt="json",
        ))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "duckdb is required" in err
    assert "pip install" in err
    assert "casetrack[query]" in err


# ── CLI smoke ──────────────────────────────────────────────────────────────────


def test_query_cli_smoke(tmp_project, samples_file):
    manifest = _seed_manifest(tmp_project, samples_file)
    res = subprocess.run(
        [sys.executable, str(Path(casetrack.__file__)), "query",
         "--manifest", str(manifest), "--fmt", "json",
         "SELECT sample_id FROM _ ORDER BY sample_id LIMIT 2"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert [r["sample_id"] for r in data] == ["SAMPLE_01", "SAMPLE_02"]
