"""Tests for the vectorized smart-merge helper `fill_nan_cells`.

Covers correctness vs. the original iterrows() semantics plus a size-scaling
test that would time out if the inner loop were O(N_rows * N_cols).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import casetrack
from conftest import write_tsv


# ── Unit: fill_nan_cells ──────────────────────────────────────────────────────


def test_fill_nan_fills_only_missing():
    manifest = pd.DataFrame(
        {
            "sample_id": ["A", "B", "C"],
            "meth": [0.1, np.nan, np.nan],
        }
    )
    results = pd.DataFrame(
        {"sample_id": ["A", "B", "C"], "meth": [0.99, 0.5, 0.7]}
    )
    out = casetrack.fill_nan_cells(manifest, results, "sample_id", ["meth"])

    # A is preserved (non-NaN); B and C get filled.
    assert out.set_index("sample_id").loc["A", "meth"] == 0.1
    assert out.set_index("sample_id").loc["B", "meth"] == 0.5
    assert out.set_index("sample_id").loc["C", "meth"] == 0.7


def test_fill_nan_ignores_missing_keys():
    manifest = pd.DataFrame({"sample_id": ["A", "B"], "v": [np.nan, np.nan]})
    results = pd.DataFrame({"sample_id": ["X"], "v": [42.0]})
    out = casetrack.fill_nan_cells(manifest, results, "sample_id", ["v"])
    # Neither A nor B is in results → both remain NaN.
    assert out["v"].isna().all()


def test_fill_nan_skips_columns_not_in_results():
    manifest = pd.DataFrame({"sample_id": ["A"], "m": [np.nan], "other": [np.nan]})
    results = pd.DataFrame({"sample_id": ["A"], "m": [1.0]})
    out = casetrack.fill_nan_cells(manifest, results, "sample_id", ["m", "other"])
    assert out.loc[0, "m"] == 1.0
    assert pd.isna(out.loc[0, "other"])


def test_fill_nan_handles_duplicate_keys_in_results():
    """If results has duplicate keys, keep-first is used (deterministic)."""
    manifest = pd.DataFrame({"sample_id": ["A"], "v": [np.nan]})
    results = pd.DataFrame({"sample_id": ["A", "A"], "v": [1.0, 2.0]})
    out = casetrack.fill_nan_cells(manifest, results, "sample_id", ["v"])
    assert out.loc[0, "v"] == 1.0


def test_fill_nan_empty_cols_list_noop():
    manifest = pd.DataFrame({"sample_id": ["A"], "v": [np.nan]})
    results = pd.DataFrame({"sample_id": ["A"], "v": [1.0]})
    out = casetrack.fill_nan_cells(manifest, results, "sample_id", [])
    pd.testing.assert_frame_equal(out, manifest)


def test_fill_nan_string_key_coercion():
    """Manifest keys as ints and results as strings should still match
    (matches the prior behavior of str()-comparing keys).
    """
    manifest = pd.DataFrame({"sample_id": [1, 2, 3], "v": [np.nan, np.nan, np.nan]})
    results = pd.DataFrame({"sample_id": ["1", "2", "3"], "v": [10, 20, 30]})
    out = casetrack.fill_nan_cells(manifest, results, "sample_id", ["v"])
    assert list(out["v"]) == [10, 20, 30]


# ── End-to-end via cmd_append ──────────────────────────────────────────────────


def test_append_smart_merge_perf_large(tmp_project: Path):
    """5000-sample, 10-column fill must complete quickly post-vectorization.

    Before the fix this would be O(rows * cols) Python-level iterations.
    A generous 15s budget keeps the test robust on slow CI.
    """
    n = 5000
    sample_ids = [f"S{i:05d}" for i in range(n)]
    samples = tmp_project / "samples.txt"
    samples.write_text("\n".join(sample_ids) + "\n")

    manifest = tmp_project / "manifest.tsv"
    casetrack.cmd_init(
        argparse.Namespace(
            manifest=str(manifest),
            samples=str(samples),
            key="sample_id",
            metadata=None,
            cols=None,
            force=False,
        )
    )

    # Batch 1: first half populates all 10 columns (creates them in manifest).
    cols = [f"m_{i}" for i in range(10)]
    r1 = tmp_project / "r1.tsv"
    df1 = pd.DataFrame({"sample_id": sample_ids[: n // 2]})
    for c in cols:
        df1[c] = np.random.rand(n // 2)
    write_tsv(r1, df1)

    ns1 = argparse.Namespace(
        manifest=str(manifest),
        results=str(r1),
        key="sample_id",
        analysis="bulk",
        overwrite=False,
        allow_new=False,
    )
    casetrack.cmd_append(ns1)

    # Batch 2: second half fills the NaN cells. This is the smart-merge path.
    r2 = tmp_project / "r2.tsv"
    df2 = pd.DataFrame({"sample_id": sample_ids[n // 2:]})
    for c in cols:
        df2[c] = np.random.rand(n - n // 2)
    write_tsv(r2, df2)

    ns2 = argparse.Namespace(
        manifest=str(manifest),
        results=str(r2),
        key="sample_id",
        analysis="bulk",
        overwrite=False,
        allow_new=False,
    )

    start = time.perf_counter()
    casetrack.cmd_append(ns2)
    elapsed = time.perf_counter() - start
    assert elapsed < 15.0, f"smart-merge too slow: {elapsed:.2f}s for {n} samples"

    # Correctness: every cell is filled, nothing is NaN.
    final = pd.read_csv(manifest, sep="\t")
    for c in cols:
        assert final[c].notna().all(), f"{c} has unfilled NaN after smart merge"
    assert final["bulk_done"].notna().all()


def test_append_smart_merge_preserves_dtype_where_possible(
    append_args_factory, initialized_manifest, tmp_project
):
    """A first batch sets a float column; a second batch fills remaining floats —
    the column must still be numeric afterward (not coerced to object)."""
    r1 = tmp_project / "r1.tsv"
    write_tsv(
        r1,
        pd.DataFrame(
            {"sample_id": ["SAMPLE_01", "SAMPLE_02"], "modkit_mean_meth": [0.10, 0.20]}
        ),
    )
    casetrack.cmd_append(append_args_factory(results=str(r1), analysis="modkit"))

    r2 = tmp_project / "r2.tsv"
    write_tsv(
        r2,
        pd.DataFrame(
            {"sample_id": ["SAMPLE_03", "SAMPLE_04"], "modkit_mean_meth": [0.30, 0.40]}
        ),
    )
    casetrack.cmd_append(append_args_factory(results=str(r2), analysis="modkit"))

    df = pd.read_csv(initialized_manifest, sep="\t")
    assert pd.api.types.is_numeric_dtype(df["modkit_mean_meth"])
    vals = df.set_index("sample_id")["modkit_mean_meth"]
    assert vals["SAMPLE_01"] == 0.10
    assert vals["SAMPLE_04"] == 0.40
