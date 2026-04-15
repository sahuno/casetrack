"""Tests for `casetrack add-metadata`.

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


def _meta_ns(manifest: Path, metadata: Path, **overrides):
    defaults = dict(
        manifest=str(manifest),
        metadata=str(metadata),
        key="sample_id",
        fill_only=False,
        overwrite=False,
        allow_new=False,
        yes=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _read(mpath: Path) -> pd.DataFrame:
    return pd.read_csv(mpath, sep="\t")


# ── Core behavior ──────────────────────────────────────────────────────────────


def test_add_metadata_adds_new_columns(initialized_manifest: Path, tmp_project: Path):
    meta = tmp_project / "clinical.tsv"
    write_tsv(
        meta,
        pd.DataFrame({
            "sample_id": ["SAMPLE_01", "SAMPLE_02", "SAMPLE_03"],
            "tissue": ["tumor", "normal", "tumor"],
            "age": [54, 61, 47],
        }),
    )
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, meta))

    df = _read(initialized_manifest)
    assert {"tissue", "age"}.issubset(df.columns)
    byid = df.set_index("sample_id")
    assert byid.loc["SAMPLE_01", "tissue"] == "tumor"
    assert byid.loc["SAMPLE_02", "age"] == 61
    # No _done column injected — this is metadata, not an analysis.
    assert not any(c.endswith("_done") for c in df.columns)


def test_add_metadata_no_schema_entry(initialized_manifest: Path, tmp_project: Path):
    """Metadata must not appear in the schema file (which tracks analyses)."""
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({"sample_id": ["SAMPLE_01"], "tissue": ["tumor"]}))
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, meta))

    schema_path = Path(str(initialized_manifest) + casetrack.SCHEMA_SUFFIX)
    if schema_path.exists():
        schema = json.loads(schema_path.read_text())
        assert "metadata" not in schema
        assert "tissue" not in schema.get("metadata", {}).get("columns", [])


def test_add_metadata_logs_provenance(initialized_manifest: Path, tmp_project: Path):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({
        "sample_id": ["SAMPLE_01", "SAMPLE_02"], "tissue": ["tumor", "normal"]
    }))
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, meta))

    prov = Path(str(initialized_manifest) + casetrack.PROVENANCE_SUFFIX) \
        .read_text().splitlines()
    entries = [json.loads(l) for l in prov if json.loads(l).get("action") == "add-metadata"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["columns_added"] == ["tissue"]
    assert entry["samples_updated"] == 2
    assert entry["collisions"] == []
    assert entry["collision_policy"] == "none"
    assert entry["metadata_checksum"]  # non-empty


# ── Collision handling ────────────────────────────────────────────────────────


def test_add_metadata_collision_default_exits(
    initialized_manifest: Path, tmp_project: Path
):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({
        "sample_id": ["SAMPLE_01"], "tissue": ["tumor"]
    }))
    # First addition succeeds.
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, meta))
    # Second addition collides and must refuse without a flag.
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, meta))
    assert excinfo.value.code == 1


def test_add_metadata_fill_only_preserves_existing(
    initialized_manifest: Path, tmp_project: Path
):
    # Seed: SAMPLE_01 already has tissue recorded.
    m1 = tmp_project / "m1.tsv"
    write_tsv(m1, pd.DataFrame({"sample_id": ["SAMPLE_01"], "tissue": ["tumor"]}))
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, m1))

    # Fill-only update: try to change SAMPLE_01 (must be preserved) and fill SAMPLE_02.
    m2 = tmp_project / "m2.tsv"
    write_tsv(m2, pd.DataFrame({
        "sample_id": ["SAMPLE_01", "SAMPLE_02"],
        "tissue": ["normal", "tumor"],
    }))
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, m2, fill_only=True))

    df = _read(initialized_manifest).set_index("sample_id")
    assert df.loc["SAMPLE_01", "tissue"] == "tumor"   # preserved (fill-only)
    assert df.loc["SAMPLE_02", "tissue"] == "tumor"   # filled


def test_add_metadata_overwrite_replaces(
    initialized_manifest: Path, tmp_project: Path
):
    m1 = tmp_project / "m1.tsv"
    write_tsv(m1, pd.DataFrame({"sample_id": ["SAMPLE_01"], "tissue": ["tumor"]}))
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, m1))

    m2 = tmp_project / "m2.tsv"
    write_tsv(m2, pd.DataFrame({"sample_id": ["SAMPLE_01"], "tissue": ["normal"]}))
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, m2, overwrite=True))

    df = _read(initialized_manifest).set_index("sample_id")
    assert df.loc["SAMPLE_01", "tissue"] == "normal"


def test_add_metadata_overwrite_and_fill_only_mutually_exclusive(
    initialized_manifest: Path, tmp_project: Path
):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({"sample_id": ["SAMPLE_01"], "tissue": ["tumor"]}))
    with pytest.raises(SystemExit):
        casetrack.cmd_add_metadata(_meta_ns(
            initialized_manifest, meta, overwrite=True, fill_only=True
        ))


def test_add_metadata_collision_plus_new_columns(
    initialized_manifest: Path, tmp_project: Path
):
    """Mixed collision + brand-new columns under --fill-only should both land."""
    m1 = tmp_project / "m1.tsv"
    write_tsv(m1, pd.DataFrame({"sample_id": ["SAMPLE_01"], "tissue": ["tumor"]}))
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, m1))

    m2 = tmp_project / "m2.tsv"
    write_tsv(m2, pd.DataFrame({
        "sample_id": ["SAMPLE_02", "SAMPLE_03"],
        "tissue": ["normal", "tumor"],
        "age": [50, 60],
    }))
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, m2, fill_only=True))

    df = _read(initialized_manifest).set_index("sample_id")
    assert df.loc["SAMPLE_02", "tissue"] == "normal"
    assert df.loc["SAMPLE_03", "age"] == 60
    # SAMPLE_01 still tumor
    assert df.loc["SAMPLE_01", "tissue"] == "tumor"
    # SAMPLE_01 has no age recorded
    assert pd.isna(df.loc["SAMPLE_01", "age"])


# ── Unknown samples / --allow-new ──────────────────────────────────────────────


def test_add_metadata_allow_new_without_yes_refuses(
    initialized_manifest: Path, tmp_project: Path, capsys
):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({
        "sample_id": ["NEW_TYPO_1", "NEW_TYPO_2"],
        "tissue": ["tumor", "normal"],
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_add_metadata(
            _meta_ns(initialized_manifest, meta, allow_new=True, yes=False)
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "Refusing to add 2 new sample(s)" in err
    assert "NEW_TYPO_1" in err and "NEW_TYPO_2" in err
    # Manifest unchanged.
    df = _read(initialized_manifest)
    assert len(df) == 5
    assert "NEW_TYPO_1" not in df["sample_id"].tolist()


def test_add_metadata_allow_new_yes_announces(
    initialized_manifest: Path, tmp_project: Path, capsys
):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({
        "sample_id": ["NEW_42"], "tissue": ["tumor"],
    }))
    casetrack.cmd_add_metadata(
        _meta_ns(initialized_manifest, meta, allow_new=True, yes=True)
    )
    err = capsys.readouterr().err
    assert "Adding 1 new sample(s): NEW_42" in err


def test_add_metadata_unknown_samples_default_skipped(
    initialized_manifest: Path, tmp_project: Path
):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({
        "sample_id": ["SAMPLE_01", "NEW_42"], "tissue": ["tumor", "normal"]
    }))
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, meta))
    df = _read(initialized_manifest)
    assert "NEW_42" not in df["sample_id"].tolist()
    assert df.set_index("sample_id").loc["SAMPLE_01", "tissue"] == "tumor"


def test_add_metadata_allow_new_adds_rows(
    initialized_manifest: Path, tmp_project: Path
):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({
        "sample_id": ["NEW_42"], "tissue": ["tumor"]
    }))
    casetrack.cmd_add_metadata(
        _meta_ns(initialized_manifest, meta, allow_new=True, yes=True)
    )
    df = _read(initialized_manifest)
    assert "NEW_42" in df["sample_id"].tolist()


# ── Error paths ────────────────────────────────────────────────────────────────


def test_add_metadata_missing_manifest_exits(tmp_project: Path):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({"sample_id": ["X"], "v": [1]}))
    with pytest.raises(SystemExit):
        casetrack.cmd_add_metadata(_meta_ns(tmp_project / "nope.tsv", meta))


def test_add_metadata_missing_metadata_exits(initialized_manifest: Path, tmp_project: Path):
    with pytest.raises(SystemExit):
        casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, tmp_project / "nope.tsv"))


def test_add_metadata_wrong_key_exits(initialized_manifest: Path, tmp_project: Path):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({"patient_id": ["SAMPLE_01"], "tissue": ["tumor"]}))
    with pytest.raises(SystemExit):
        casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, meta))


def test_add_metadata_only_key_column_exits(initialized_manifest: Path, tmp_project: Path):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({"sample_id": ["SAMPLE_01"]}))
    with pytest.raises(SystemExit):
        casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, meta))


# ── Interaction with validate + dashboard ─────────────────────────────────────


def test_add_metadata_manifest_still_validates(initialized_manifest: Path, tmp_project: Path):
    meta = tmp_project / "m.tsv"
    write_tsv(meta, pd.DataFrame({
        "sample_id": ["SAMPLE_01", "SAMPLE_02", "SAMPLE_03", "SAMPLE_04", "SAMPLE_05"],
        "tissue": ["tumor"] * 5,
    }))
    casetrack.cmd_add_metadata(_meta_ns(initialized_manifest, meta))
    # validate expects non-null, non-dup keys and no completely empty cols.
    casetrack.cmd_validate(argparse.Namespace(
        manifest=str(initialized_manifest), key="sample_id"
    ))


# ── CLI smoke ──────────────────────────────────────────────────────────────────


def test_add_metadata_cli_smoke(tmp_project: Path, samples_file: Path):
    manifest = tmp_project / "manifest.tsv"
    subprocess.run(
        [sys.executable, str(Path(casetrack.__file__)), "init",
         "--manifest", str(manifest), "--samples", str(samples_file)],
        check=True, capture_output=True, text=True,
    )
    meta = tmp_project / "clinical.tsv"
    write_tsv(meta, pd.DataFrame({
        "sample_id": ["SAMPLE_01", "SAMPLE_02"], "tissue": ["tumor", "normal"]
    }))
    res = subprocess.run(
        [sys.executable, str(Path(casetrack.__file__)), "add-metadata",
         "--manifest", str(manifest), "--metadata", str(meta)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    df = pd.read_csv(manifest, sep="\t").set_index("sample_id")
    assert df.loc["SAMPLE_01", "tissue"] == "tumor"
