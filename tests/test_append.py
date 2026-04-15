"""Tests for `casetrack append` including smart-merge behavior.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from conftest import write_tsv


def _read(mpath: Path) -> pd.DataFrame:
    return pd.read_csv(mpath, sep="\t")


def test_append_adds_new_columns_and_done(append_args_factory, initialized_manifest, tmp_project):
    results = tmp_project / "results.tsv"
    write_tsv(
        results,
        pd.DataFrame(
            {
                "sample_id": ["SAMPLE_01", "SAMPLE_02"],
                "modkit_mean_meth": [0.72, 0.81],
            }
        ),
    )
    casetrack.cmd_append(append_args_factory(analysis="modkit"))

    df = _read(initialized_manifest)
    assert "modkit_mean_meth" in df.columns
    assert "modkit_done" in df.columns
    row = df.set_index("sample_id").loc["SAMPLE_01"]
    assert row["modkit_mean_meth"] == 0.72
    assert pd.notna(row["modkit_done"])
    # Rows not in results stay NaN
    assert pd.isna(df.set_index("sample_id").loc["SAMPLE_05", "modkit_mean_meth"])


def test_append_respects_caller_supplied_done(append_args_factory, initialized_manifest, tmp_project):
    """If results already contains the _done column, casetrack should not overwrite it."""
    results = tmp_project / "results.tsv"
    write_tsv(
        results,
        pd.DataFrame(
            {
                "sample_id": ["SAMPLE_01"],
                "modkit_mean_meth": [0.5],
                "modkit_done": ["2025-01-01"],
            }
        ),
    )
    casetrack.cmd_append(append_args_factory(analysis="modkit"))
    df = _read(initialized_manifest).set_index("sample_id")
    assert df.loc["SAMPLE_01", "modkit_done"] == "2025-01-01"


def test_append_smart_merge_fills_nan_without_overwrite(
    append_args_factory, initialized_manifest, tmp_project
):
    """First task creates columns; second task fills the NaN cells for its own samples."""
    # First batch: SAMPLE_01, SAMPLE_02
    r1 = tmp_project / "r1.tsv"
    write_tsv(
        r1,
        pd.DataFrame(
            {"sample_id": ["SAMPLE_01", "SAMPLE_02"], "modkit_mean_meth": [0.1, 0.2]}
        ),
    )
    casetrack.cmd_append(append_args_factory(results=str(r1), analysis="modkit"))

    # Second batch: SAMPLE_03 — columns already exist from first call
    r2 = tmp_project / "r2.tsv"
    write_tsv(
        r2,
        pd.DataFrame({"sample_id": ["SAMPLE_03"], "modkit_mean_meth": [0.3]}),
    )
    casetrack.cmd_append(append_args_factory(results=str(r2), analysis="modkit"))

    df = _read(initialized_manifest).set_index("sample_id")
    assert df.loc["SAMPLE_01", "modkit_mean_meth"] == 0.1
    assert df.loc["SAMPLE_02", "modkit_mean_meth"] == 0.2
    assert df.loc["SAMPLE_03", "modkit_mean_meth"] == 0.3
    # SAMPLE_04/05 remain NaN
    assert pd.isna(df.loc["SAMPLE_04", "modkit_mean_meth"])


def test_append_smart_merge_does_not_clobber_existing_nonnan(
    append_args_factory, initialized_manifest, tmp_project
):
    """Without --overwrite, existing non-NaN cells must be preserved."""
    r1 = tmp_project / "r1.tsv"
    write_tsv(r1, pd.DataFrame({"sample_id": ["SAMPLE_01"], "modkit_mean_meth": [0.1]}))
    casetrack.cmd_append(append_args_factory(results=str(r1), analysis="modkit"))

    r2 = tmp_project / "r2.tsv"
    write_tsv(r2, pd.DataFrame({"sample_id": ["SAMPLE_01"], "modkit_mean_meth": [0.99]}))
    casetrack.cmd_append(append_args_factory(results=str(r2), analysis="modkit"))

    df = _read(initialized_manifest).set_index("sample_id")
    assert df.loc["SAMPLE_01", "modkit_mean_meth"] == 0.1  # original preserved


def test_append_overwrite_replaces_existing(
    append_args_factory, initialized_manifest, tmp_project
):
    r1 = tmp_project / "r1.tsv"
    write_tsv(r1, pd.DataFrame({"sample_id": ["SAMPLE_01"], "modkit_mean_meth": [0.1]}))
    casetrack.cmd_append(append_args_factory(results=str(r1), analysis="modkit"))

    r2 = tmp_project / "r2.tsv"
    write_tsv(r2, pd.DataFrame({"sample_id": ["SAMPLE_01"], "modkit_mean_meth": [0.99]}))
    casetrack.cmd_append(append_args_factory(results=str(r2), analysis="modkit", overwrite=True))

    df = _read(initialized_manifest).set_index("sample_id")
    assert df.loc["SAMPLE_01", "modkit_mean_meth"] == 0.99


def test_append_allow_new_adds_rows(append_args_factory, initialized_manifest, tmp_project):
    results = tmp_project / "r.tsv"
    write_tsv(
        results,
        pd.DataFrame({"sample_id": ["NEW_42"], "modkit_mean_meth": [0.5]}),
    )
    casetrack.cmd_append(
        append_args_factory(results=str(results), analysis="modkit", allow_new=True)
    )

    df = _read(initialized_manifest)
    assert "NEW_42" in df["sample_id"].tolist()
    assert df.set_index("sample_id").loc["NEW_42", "modkit_mean_meth"] == 0.5


def test_append_unknown_sample_without_allow_new_does_not_add(
    append_args_factory, initialized_manifest, tmp_project, capsys
):
    results = tmp_project / "r.tsv"
    write_tsv(
        results,
        pd.DataFrame(
            {
                "sample_id": ["SAMPLE_01", "TYPO_99"],
                "modkit_mean_meth": [0.1, 0.9],
            }
        ),
    )
    casetrack.cmd_append(append_args_factory(results=str(results), analysis="modkit"))

    df = _read(initialized_manifest)
    assert "TYPO_99" not in df["sample_id"].tolist()
    # Known sample still updated
    assert df.set_index("sample_id").loc["SAMPLE_01", "modkit_mean_meth"] == 0.1


def test_append_missing_manifest_exits(append_args_factory, tmp_project):
    results = tmp_project / "r.tsv"
    write_tsv(results, pd.DataFrame({"sample_id": ["X"], "c": [1]}))
    ns = append_args_factory(manifest=str(tmp_project / "nope.tsv"))
    with pytest.raises(SystemExit):
        casetrack.cmd_append(ns)


def test_append_missing_results_exits(append_args_factory, initialized_manifest, tmp_project):
    ns = append_args_factory(results=str(tmp_project / "nope.tsv"))
    with pytest.raises(SystemExit):
        casetrack.cmd_append(ns)


def test_append_results_missing_key_exits(append_args_factory, initialized_manifest, tmp_project):
    results = tmp_project / "r.tsv"
    write_tsv(results, pd.DataFrame({"wrong_key": ["SAMPLE_01"], "value": [1]}))
    with pytest.raises(SystemExit):
        casetrack.cmd_append(append_args_factory(results=str(results)))


def test_append_results_only_key_column_exits(append_args_factory, initialized_manifest, tmp_project):
    results = tmp_project / "r.tsv"
    write_tsv(results, pd.DataFrame({"sample_id": ["SAMPLE_01"]}))
    with pytest.raises(SystemExit):
        casetrack.cmd_append(append_args_factory(results=str(results)))


def test_append_writes_schema_and_provenance(
    append_args_factory, initialized_manifest, tmp_project
):
    results = tmp_project / "r.tsv"
    write_tsv(
        results,
        pd.DataFrame({"sample_id": ["SAMPLE_01"], "modkit_mean_meth": [0.5]}),
    )
    casetrack.cmd_append(append_args_factory(results=str(results), analysis="modkit"))

    schema = json.loads(Path(str(initialized_manifest) + casetrack.SCHEMA_SUFFIX).read_text())
    assert "modkit" in schema
    assert "modkit_mean_meth" in schema["modkit"]["columns"]
    assert "modkit_done" in schema["modkit"]["columns"]

    prov_lines = Path(str(initialized_manifest) + casetrack.PROVENANCE_SUFFIX).read_text().splitlines()
    append_entries = [json.loads(l) for l in prov_lines if json.loads(l).get("action") == "append"]
    assert len(append_entries) == 1
    entry = append_entries[0]
    assert entry["analysis"] == "modkit"
    assert entry["samples_updated"] == 1
    assert entry["results_checksum"]
    assert "modkit_done" in entry["columns_added"]


def test_append_multiple_analyses_accumulate(
    append_args_factory, initialized_manifest, tmp_project
):
    r1 = tmp_project / "r1.tsv"
    write_tsv(
        r1, pd.DataFrame({"sample_id": ["SAMPLE_01"], "modkit_mean_meth": [0.5]})
    )
    casetrack.cmd_append(append_args_factory(results=str(r1), analysis="modkit"))

    r2 = tmp_project / "r2.tsv"
    write_tsv(r2, pd.DataFrame({"sample_id": ["SAMPLE_01"], "tldr_l1_count": [3]}))
    casetrack.cmd_append(append_args_factory(results=str(r2), analysis="tldr"))

    df = _read(initialized_manifest)
    expected = {"sample_id", "modkit_mean_meth", "modkit_done", "tldr_l1_count", "tldr_done"}
    assert expected.issubset(set(df.columns))

    schema = json.loads(Path(str(initialized_manifest) + casetrack.SCHEMA_SUFFIX).read_text())
    assert set(schema.keys()) == {"modkit", "tldr"}
