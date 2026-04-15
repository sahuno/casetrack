"""Tests for `status`, `validate`, `log`, `schema`, `export`.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from conftest import write_tsv


# ── helpers ────────────────────────────────────────────────────────────────────


def _append_modkit_tldr(initialized_manifest: Path, tmp_project: Path):
    """Append modkit (2/5 samples) and tldr (3/5 samples). Return manifest path."""
    r1 = tmp_project / "r1.tsv"
    write_tsv(
        r1,
        pd.DataFrame(
            {"sample_id": ["SAMPLE_01", "SAMPLE_02"], "modkit_mean_meth": [0.1, 0.2]}
        ),
    )
    ns1 = argparse.Namespace(
        manifest=str(initialized_manifest),
        results=str(r1),
        key="sample_id",
        analysis="modkit",
        overwrite=False,
        allow_new=False,
    )
    casetrack.cmd_append(ns1)

    r2 = tmp_project / "r2.tsv"
    write_tsv(
        r2,
        pd.DataFrame(
            {
                "sample_id": ["SAMPLE_01", "SAMPLE_02", "SAMPLE_03"],
                "tldr_l1_count": [1, 2, 3],
            }
        ),
    )
    ns2 = argparse.Namespace(
        manifest=str(initialized_manifest),
        results=str(r2),
        key="sample_id",
        analysis="tldr",
        overwrite=False,
        allow_new=False,
    )
    casetrack.cmd_append(ns2)


# ── status ─────────────────────────────────────────────────────────────────────


def test_status_json(initialized_manifest, tmp_project, capsys):
    _append_modkit_tldr(initialized_manifest, tmp_project)
    capsys.readouterr()  # drain append's stdout

    ns = argparse.Namespace(
        manifest=str(initialized_manifest), key="sample_id", analysis=None, fmt="json"
    )
    casetrack.cmd_status(ns)
    out = capsys.readouterr().out
    data = json.loads(out)

    assert data["modkit"]["completed"] == 2
    assert data["modkit"]["total"] == 5
    assert data["modkit"]["pct"] == 40.0
    assert set(data["modkit"]["missing"]) == {"SAMPLE_03", "SAMPLE_04", "SAMPLE_05"}

    assert data["tldr"]["completed"] == 3
    assert data["tldr"]["pct"] == 60.0


def test_status_tsv(initialized_manifest, tmp_project, capsys):
    _append_modkit_tldr(initialized_manifest, tmp_project)
    capsys.readouterr()
    ns = argparse.Namespace(
        manifest=str(initialized_manifest), key="sample_id", analysis=None, fmt="tsv"
    )
    casetrack.cmd_status(ns)
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "analysis\tcompleted\ttotal\tpct"
    rows = {line.split("\t")[0]: line.split("\t") for line in lines[1:]}
    assert rows["modkit"][1:] == ["2", "5", "40.0"]
    assert rows["tldr"][1:] == ["3", "5", "60.0"]


def test_status_table_renders(initialized_manifest, tmp_project, capsys):
    _append_modkit_tldr(initialized_manifest, tmp_project)
    capsys.readouterr()
    ns = argparse.Namespace(
        manifest=str(initialized_manifest), key="sample_id", analysis=None, fmt="table"
    )
    casetrack.cmd_status(ns)
    out = capsys.readouterr().out
    assert "modkit" in out
    assert "tldr" in out
    assert "40.0%" in out


def test_status_filter_by_analysis(initialized_manifest, tmp_project, capsys):
    _append_modkit_tldr(initialized_manifest, tmp_project)
    capsys.readouterr()
    ns = argparse.Namespace(
        manifest=str(initialized_manifest), key="sample_id", analysis="modkit", fmt="json"
    )
    casetrack.cmd_status(ns)
    data = json.loads(capsys.readouterr().out)
    assert list(data.keys()) == ["modkit"]


def test_status_unknown_analysis_exits(initialized_manifest, tmp_project):
    _append_modkit_tldr(initialized_manifest, tmp_project)
    ns = argparse.Namespace(
        manifest=str(initialized_manifest), key="sample_id", analysis="nope", fmt="json"
    )
    with pytest.raises(SystemExit):
        casetrack.cmd_status(ns)


def test_status_missing_manifest_exits(tmp_project):
    ns = argparse.Namespace(
        manifest=str(tmp_project / "nope.tsv"),
        key="sample_id",
        analysis=None,
        fmt="json",
    )
    with pytest.raises(SystemExit):
        casetrack.cmd_status(ns)


# ── validate ───────────────────────────────────────────────────────────────────


def test_validate_ok(initialized_manifest, tmp_project, capsys):
    # Add an analysis so schema exists and passes
    _append_modkit_tldr(initialized_manifest, tmp_project)
    capsys.readouterr()
    ns = argparse.Namespace(manifest=str(initialized_manifest), key="sample_id")
    casetrack.cmd_validate(ns)
    out = capsys.readouterr().out
    assert "Manifest OK" in out


def test_validate_detects_duplicate_keys(initialized_manifest, tmp_project):
    # Corrupt the manifest to have duplicates
    df = pd.read_csv(initialized_manifest, sep="\t")
    df = pd.concat([df, df.iloc[[0]]], ignore_index=True)
    df.to_csv(initialized_manifest, sep="\t", index=False)
    ns = argparse.Namespace(manifest=str(initialized_manifest), key="sample_id")
    with pytest.raises(SystemExit):
        casetrack.cmd_validate(ns)


def test_validate_detects_null_keys(initialized_manifest, tmp_project):
    df = pd.read_csv(initialized_manifest, sep="\t")
    df.loc[len(df)] = [pd.NA]
    df.to_csv(initialized_manifest, sep="\t", index=False)
    ns = argparse.Namespace(manifest=str(initialized_manifest), key="sample_id")
    with pytest.raises(SystemExit):
        casetrack.cmd_validate(ns)


def test_validate_detects_missing_key_column(initialized_manifest):
    df = pd.read_csv(initialized_manifest, sep="\t")
    df = df.rename(columns={"sample_id": "patient"})
    df.to_csv(initialized_manifest, sep="\t", index=False)
    ns = argparse.Namespace(manifest=str(initialized_manifest), key="sample_id")
    with pytest.raises(SystemExit):
        casetrack.cmd_validate(ns)


def test_validate_detects_empty_columns(initialized_manifest):
    df = pd.read_csv(initialized_manifest, sep="\t")
    df["totally_empty"] = pd.NA
    df.to_csv(initialized_manifest, sep="\t", index=False)
    ns = argparse.Namespace(manifest=str(initialized_manifest), key="sample_id")
    with pytest.raises(SystemExit):
        casetrack.cmd_validate(ns)


def test_validate_detects_orphan_done_column(initialized_manifest):
    """_done column with no paired data column should be flagged."""
    df = pd.read_csv(initialized_manifest, sep="\t")
    df["orphan_done"] = "2026-04-15"
    df.to_csv(initialized_manifest, sep="\t", index=False)
    ns = argparse.Namespace(manifest=str(initialized_manifest), key="sample_id")
    with pytest.raises(SystemExit):
        casetrack.cmd_validate(ns)


# ── log ────────────────────────────────────────────────────────────────────────


def test_log_prints_init_and_append(initialized_manifest, tmp_project, capsys):
    _append_modkit_tldr(initialized_manifest, tmp_project)
    capsys.readouterr()
    ns = argparse.Namespace(manifest=str(initialized_manifest), last=None)
    casetrack.cmd_log(ns)
    out = capsys.readouterr().out
    assert "INIT" in out
    assert "APPEND" in out
    assert "modkit" in out
    assert "tldr" in out


def test_log_last_n(initialized_manifest, tmp_project, capsys):
    _append_modkit_tldr(initialized_manifest, tmp_project)
    capsys.readouterr()
    ns = argparse.Namespace(manifest=str(initialized_manifest), last=1)
    casetrack.cmd_log(ns)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert "tldr" in out[0]  # the most recent append


def test_log_missing_exits(tmp_project):
    ns = argparse.Namespace(manifest=str(tmp_project / "nope.tsv"), last=None)
    with pytest.raises(SystemExit):
        casetrack.cmd_log(ns)


# ── schema ─────────────────────────────────────────────────────────────────────


def test_schema_json(initialized_manifest, tmp_project, capsys):
    _append_modkit_tldr(initialized_manifest, tmp_project)
    capsys.readouterr()
    ns = argparse.Namespace(manifest=str(initialized_manifest), fmt="json")
    casetrack.cmd_schema(ns)
    data = json.loads(capsys.readouterr().out)
    assert set(data.keys()) == {"modkit", "tldr"}


def test_schema_table(initialized_manifest, tmp_project, capsys):
    _append_modkit_tldr(initialized_manifest, tmp_project)
    capsys.readouterr()
    ns = argparse.Namespace(manifest=str(initialized_manifest), fmt="table")
    casetrack.cmd_schema(ns)
    out = capsys.readouterr().out
    assert "modkit" in out and "tldr" in out


def test_schema_missing_exits(tmp_project, initialized_manifest):
    # No append has happened — schema file should not exist.
    ns = argparse.Namespace(manifest=str(initialized_manifest), fmt="table")
    with pytest.raises(SystemExit):
        casetrack.cmd_schema(ns)


# ── export ─────────────────────────────────────────────────────────────────────


def test_export_csv(initialized_manifest, tmp_project):
    out = tmp_project / "out.csv"
    ns = argparse.Namespace(manifest=str(initialized_manifest), output=str(out))
    casetrack.cmd_export(ns)
    assert out.exists()
    df = pd.read_csv(out)
    assert "sample_id" in df.columns
    assert len(df) == 5


def test_export_json(initialized_manifest, tmp_project):
    out = tmp_project / "out.json"
    ns = argparse.Namespace(manifest=str(initialized_manifest), output=str(out))
    casetrack.cmd_export(ns)
    data = json.loads(out.read_text())
    assert isinstance(data, list)
    assert len(data) == 5
    assert data[0]["sample_id"] == "SAMPLE_01"


def test_export_xlsx(initialized_manifest, tmp_project):
    pytest.importorskip("openpyxl")
    out = tmp_project / "out.xlsx"
    ns = argparse.Namespace(manifest=str(initialized_manifest), output=str(out))
    casetrack.cmd_export(ns)
    assert out.exists() and out.stat().st_size > 0


def test_export_parquet(initialized_manifest, tmp_project):
    pytest.importorskip("pyarrow")
    out = tmp_project / "out.parquet"
    ns = argparse.Namespace(manifest=str(initialized_manifest), output=str(out))
    casetrack.cmd_export(ns)
    df = pd.read_parquet(out)
    assert len(df) == 5


def test_export_unsupported_exits(initialized_manifest, tmp_project):
    out = tmp_project / "out.xyz"
    ns = argparse.Namespace(manifest=str(initialized_manifest), output=str(out))
    with pytest.raises(SystemExit):
        casetrack.cmd_export(ns)
