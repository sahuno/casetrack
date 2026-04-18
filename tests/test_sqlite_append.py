"""Tests for `casetrack append --project-dir` (v0.3 / proposal 0001 Q7).

Covers dynamic ALTER TABLE ADD COLUMN, type inference + --col-type overrides,
fill-only vs --overwrite semantics, strict key enforcement (exit 2 on
unknown IDs), transactional rollback, and provenance shape.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import casetrack


# ── fixtures ──────────────────────────────────────────────────────────────────


def _init_ns(project_dir: Path, template: str = "hgsoc") -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        samples=None,
        key="sample_id",
        metadata=None,
        cols=None,
        from_template=template,
        project_name=None,
        force=False,
    )


def _reg_ns(project_dir: Path, *, level: str, id: str, parent: str | None = None,
            meta: str | None = None, allow_new_parent: bool = False,
            yes: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir),
        level=level,
        id=id,
        parent=parent,
        meta=meta,
        allow_new_parent=allow_new_parent,
        yes=yes,
    )


def _append_ns(project_dir: Path, *, results: Path, analysis: str,
               level: str | None = None, col_type: str | None = None,
               column_prefix: str | None = None,
               overwrite: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        results=str(results),
        key="sample_id",
        analysis=analysis,
        level=level,
        col_type=col_type,
        column_prefix=column_prefix,
        overwrite=overwrite,
        allow_new=False,
        yes=False,
    )


@pytest.fixture
def seeded_project(tmp_path: Path) -> Path:
    """Project with 1 patient, 1 specimen, 2 assays registered."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P001", meta="age=55,sex=F"))
    casetrack.cmd_register(_reg_ns(
        proj, level="specimen", id="S001", parent="P001", meta="tissue_site=tumor"
    ))
    casetrack.cmd_register(_reg_ns(
        proj, level="assay", id="A001", parent="S001", meta="assay_type=WGS"
    ))
    casetrack.cmd_register(_reg_ns(
        proj, level="assay", id="A002", parent="S001", meta="assay_type=ATAC"
    ))
    return proj


def _conn(project_dir: Path) -> sqlite3.Connection:
    return casetrack.open_project_db(project_dir / "casetrack.db")


def _write_summary(path: Path, df: pd.DataFrame) -> Path:
    df.to_csv(path, sep="\t", index=False)
    return path


# ── _parse_col_type_overrides ─────────────────────────────────────────────────


def test_parse_col_type_basic():
    out = casetrack._parse_col_type_overrides("a:INTEGER,b:REAL")
    assert out == {"a": "INTEGER", "b": "REAL"}


def test_parse_col_type_upcases():
    assert casetrack._parse_col_type_overrides("a:real") == {"a": "REAL"}


def test_parse_col_type_empty_returns_empty():
    assert casetrack._parse_col_type_overrides(None) == {}
    assert casetrack._parse_col_type_overrides("") == {}


def test_parse_col_type_rejects_unknown_type():
    with pytest.raises(ValueError, match="unsupported type"):
        casetrack._parse_col_type_overrides("a:MAGIC")


def test_parse_col_type_rejects_malformed():
    with pytest.raises(ValueError, match="expected 'col:TYPE'"):
        casetrack._parse_col_type_overrides("no_colon")


# ── Happy path ────────────────────────────────────────────────────────────────


def test_append_adds_columns_and_updates_rows(seeded_project: Path, tmp_path: Path):
    summary = _write_summary(tmp_path / "modkit.tsv", pd.DataFrame({
        "assay_id": ["A001", "A002"],
        "mean_meth": [0.72, 0.65],
        "n_reads": [2_345_678, 1_234_567],
    }))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=summary, analysis="modkit",
    ))

    with _conn(seeded_project) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(assays)").fetchall()]
        rows = dict(c.execute("SELECT assay_id, mean_meth FROM assays").fetchall())

    assert "mean_meth" in cols
    assert "n_reads" in cols
    assert "modkit_done" in cols
    assert rows["A001"] == 0.72
    assert rows["A002"] == 0.65


def test_append_infers_types_from_pandas_dtype(seeded_project: Path, tmp_path: Path):
    summary = _write_summary(tmp_path / "mix.tsv", pd.DataFrame({
        "assay_id": ["A001"],
        "int_col": [42],
        "real_col": [3.14],
        "text_col": ["hello"],
    }))
    casetrack.cmd_append(_append_ns(seeded_project, results=summary, analysis="mix"))

    with _conn(seeded_project) as c:
        types = {r[1]: r[2] for r in c.execute("PRAGMA table_info(assays)").fetchall()}
    assert types["int_col"] == "INTEGER"
    assert types["real_col"] == "REAL"
    assert types["text_col"] == "TEXT"


def test_col_type_override_beats_inference(seeded_project: Path, tmp_path: Path):
    """Pandas infers int64, but user asks for REAL (e.g., to allow future NaNs)."""
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"],
        "counted": [100],
    }))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=summary, analysis="ct", col_type="counted:REAL",
    ))
    with _conn(seeded_project) as c:
        types = {r[1]: r[2] for r in c.execute("PRAGMA table_info(assays)").fetchall()}
    assert types["counted"] == "REAL"


def test_auto_done_column_when_absent(seeded_project: Path, tmp_path: Path):
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"], "val": [1.0],
    }))
    casetrack.cmd_append(_append_ns(seeded_project, results=summary, analysis="m"))
    with _conn(seeded_project) as c:
        (done,) = c.execute("SELECT m_done FROM assays WHERE assay_id='A001'").fetchone()
    assert done is not None and len(done) >= 10  # looks like a timestamp


def test_explicit_done_column_preserved(seeded_project: Path, tmp_path: Path):
    """If the summary TSV already has an `{analysis}_done` column, use its values."""
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"],
        "val": [1.0],
        "m_done": ["2026-01-01T00:00:00"],
    }))
    casetrack.cmd_append(_append_ns(seeded_project, results=summary, analysis="m"))
    with _conn(seeded_project) as c:
        (done,) = c.execute("SELECT m_done FROM assays WHERE assay_id='A001'").fetchone()
    assert done == "2026-01-01T00:00:00"


# ── Fill-only vs --overwrite ──────────────────────────────────────────────────


def test_default_is_fill_only(seeded_project: Path, tmp_path: Path):
    """Second append with a different value should NOT overwrite the first."""
    s1 = _write_summary(tmp_path / "s1.tsv", pd.DataFrame({
        "assay_id": ["A001"], "val": [1.0],
    }))
    s2 = _write_summary(tmp_path / "s2.tsv", pd.DataFrame({
        "assay_id": ["A001"], "val": [9.9],
    }))
    casetrack.cmd_append(_append_ns(seeded_project, results=s1, analysis="a"))
    casetrack.cmd_append(_append_ns(seeded_project, results=s2, analysis="a"))

    with _conn(seeded_project) as c:
        (val,) = c.execute("SELECT val FROM assays WHERE assay_id='A001'").fetchone()
    assert val == 1.0  # first write wins under fill-only


def test_overwrite_replaces_existing(seeded_project: Path, tmp_path: Path):
    s1 = _write_summary(tmp_path / "s1.tsv", pd.DataFrame({
        "assay_id": ["A001"], "val": [1.0],
    }))
    s2 = _write_summary(tmp_path / "s2.tsv", pd.DataFrame({
        "assay_id": ["A001"], "val": [9.9],
    }))
    casetrack.cmd_append(_append_ns(seeded_project, results=s1, analysis="a"))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=s2, analysis="a", overwrite=True
    ))

    with _conn(seeded_project) as c:
        (val,) = c.execute("SELECT val FROM assays WHERE assay_id='A001'").fetchone()
    assert val == 9.9


# ── Strict key enforcement ────────────────────────────────────────────────────


def test_unknown_key_exits_two(seeded_project: Path, tmp_path: Path, capsys):
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001", "A_GHOST"], "val": [1.0, 2.0],
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_append(_append_ns(
            seeded_project, results=summary, analysis="a",
        ))
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "A_GHOST" in err
    assert "Register them first" in err


def test_unknown_key_rolls_back_alter_table(seeded_project: Path, tmp_path: Path):
    """If the key check fails, any already-executed ALTER TABLE must be undone."""
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001", "A_GHOST"], "new_measurement": [1.0, 2.0],
    }))
    with pytest.raises(SystemExit):
        casetrack.cmd_append(_append_ns(
            seeded_project, results=summary, analysis="a",
        ))
    with _conn(seeded_project) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(assays)").fetchall()}
    assert "new_measurement" not in cols
    assert "a_done" not in cols


# ── Per-level routing ─────────────────────────────────────────────────────────


def test_append_at_patient_level(seeded_project: Path, tmp_path: Path):
    summary = _write_summary(tmp_path / "p.tsv", pd.DataFrame({
        "patient_id": ["P001"], "tmb": [12.5],
    }))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=summary, analysis="tmb_calc", level="patient",
    ))
    with _conn(seeded_project) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(patients)").fetchall()}
        (tmb,) = c.execute("SELECT tmb FROM patients WHERE patient_id='P001'").fetchone()
    assert "tmb" in cols
    assert "tmb_calc_done" in cols
    assert tmb == 12.5


def test_append_defaults_to_assay_level(seeded_project: Path, tmp_path: Path):
    """analysis_defaults.default_level is 'assay' in the shipped templates."""
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"], "whatever": [1.0],
    }))
    casetrack.cmd_append(_append_ns(seeded_project, results=summary, analysis="w"))
    with _conn(seeded_project) as c:
        assays_cols = {r[1] for r in c.execute("PRAGMA table_info(assays)").fetchall()}
        patients_cols = {r[1] for r in c.execute("PRAGMA table_info(patients)").fetchall()}
    assert "whatever" in assays_cols
    assert "whatever" not in patients_cols


def test_key_column_mismatch_errors(seeded_project: Path, tmp_path: Path, capsys):
    """TSV with patient_id but --level defaults to assay → key-column missing."""
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "patient_id": ["P001"], "val": [1.0],
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_append(_append_ns(seeded_project, results=summary, analysis="v"))
    assert excinfo.value.code == 1
    assert "assay_id" in capsys.readouterr().err


# ── Error-path coverage ───────────────────────────────────────────────────────


def test_missing_results_file_exits(seeded_project: Path, tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_append(_append_ns(
            seeded_project, results=tmp_path / "nope.tsv", analysis="x",
        ))
    assert excinfo.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_unknown_col_type_override_errors(seeded_project: Path, tmp_path: Path, capsys):
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"], "val": [1.0],
    }))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_append(_append_ns(
            seeded_project, results=summary, analysis="a",
            col_type="typo_col:REAL",
        ))
    assert excinfo.value.code == 1
    assert "typo_col" in capsys.readouterr().err


def test_results_tsv_with_only_key_column_errors(seeded_project: Path, tmp_path: Path, capsys):
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({"assay_id": ["A001"]}))
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_append(_append_ns(seeded_project, results=summary, analysis="x"))
    assert excinfo.value.code == 1
    assert "no columns besides" in capsys.readouterr().err


# ── Provenance ────────────────────────────────────────────────────────────────


def test_provenance_append_entry_shape(seeded_project: Path, tmp_path: Path):
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001", "A002"],
        "mean_meth": [0.72, 0.65],
    }))
    casetrack.cmd_append(_append_ns(seeded_project, results=summary, analysis="modkit"))

    entries = [
        json.loads(ln)
        for ln in (seeded_project / "provenance.jsonl").read_text().splitlines()
    ]
    ap = next(e for e in entries if e["action"] == "append")
    assert ap["level"] == "assay"
    assert ap["analysis"] == "modkit"
    assert set(ap["columns_added"]) == {"mean_meth", "modkit_done"}
    assert ap["rows_affected"] == 2
    assert ap["results_checksum"] and len(ap["results_checksum"]) == 32
    assert ap["transaction_id"].startswith("txn_")
    assert any("ALTER TABLE" in s for s in ap["sql"])
    assert any("UPDATE" in s for s in ap["sql"])


def test_failed_append_leaves_no_provenance(seeded_project: Path, tmp_path: Path):
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A_GHOST"], "val": [1.0],
    }))
    with pytest.raises(SystemExit):
        casetrack.cmd_append(_append_ns(seeded_project, results=summary, analysis="g"))
    entries = [
        json.loads(ln)
        for ln in (seeded_project / "provenance.jsonl").read_text().splitlines()
    ]
    assert all(e["action"] != "append" for e in entries)


# ── Dispatch / flat compatibility ─────────────────────────────────────────────


def test_flat_append_still_works(tmp_path: Path, initialized_manifest: Path,
                                  append_args_factory):
    """v0.2 flat-mode append path must remain untouched after dispatch refactor."""
    results = tmp_path / "results.tsv"
    pd.DataFrame({"sample_id": ["SAMPLE_01", "SAMPLE_02"],
                  "val": [1.0, 2.0]}).to_csv(results, sep="\t", index=False)
    ns = append_args_factory(results=str(results), analysis="a")
    casetrack.cmd_append(ns)

    df = pd.read_csv(initialized_manifest, sep="\t")
    assert "val" in df.columns
    assert "a_done" in df.columns


# ── --column-prefix (analysis-scoped columns) ─────────────────────────────────


def test_column_prefix_renames_analysis_cols(seeded_project: Path, tmp_path: Path):
    """Every analysis column in the TSV lands with {prefix}_ prepended."""
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"],
        "mean_meth": [0.7],
        "n_cpg": [1234],
    }))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=summary, analysis="modkit_merged",
        column_prefix="merged",
    ))

    with _conn(seeded_project) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(assays)").fetchall()}
    assert "merged_mean_meth" in cols
    assert "merged_n_cpg" in cols
    # Unprefixed originals must NOT be written.
    assert "mean_meth" not in cols
    assert "n_cpg" not in cols


def test_column_prefix_skips_key_and_done_col(seeded_project: Path, tmp_path: Path):
    """Key column and {analysis}_done are never prefixed."""
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"], "val": [1.0],
    }))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=summary, analysis="modkit_merged",
        column_prefix="merged",
    ))

    with _conn(seeded_project) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(assays)").fetchall()}
    # Key column is untouched (join key).
    assert "assay_id" in cols
    assert "merged_assay_id" not in cols
    # {analysis}_done auto-added without prefix (already scoped by analysis name).
    assert "modkit_merged_done" in cols
    assert "merged_modkit_merged_done" not in cols


def test_column_prefix_skips_autoflag_columns(seeded_project: Path, tmp_path: Path):
    """qc_pass / qc_fail_reason / qc_warn are consumed by the autoflag path
    and must never be prefixed — the consumer looks them up by exact name,
    and a prefix would prevent it from firing at all."""
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"],
        "val": [1.0],
        "qc_pass": [False],
        "qc_fail_reason": ["synthetic fail for test"],
    }))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=summary, analysis="modkit_merged",
        column_prefix="merged",
    ))

    with _conn(seeded_project) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(assays)").fetchall()}
        (n_events,) = c.execute(
            "SELECT COUNT(*) FROM qc_events WHERE entity_id='A001' AND source='slurm'"
        ).fetchone()
    # Data columns DID get prefixed.
    assert "merged_val" in cols
    # Autoflag columns DID NOT get prefixed (prefix would break the consumer).
    assert "merged_qc_pass" not in cols
    assert "merged_qc_fail_reason" not in cols
    # Proof the autoflag consumer fired: one slurm-source event on A001.
    assert n_events == 1


def test_column_prefix_col_type_uses_tsv_names(seeded_project: Path, tmp_path: Path):
    """--col-type matches the ORIGINAL TSV column names, not the prefixed ones."""
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"],
        "counted": [100],  # would infer INTEGER
    }))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=summary, analysis="mx",
        column_prefix="p1",
        col_type="counted:REAL",  # reference the TSV name, pre-prefix
    ))

    with _conn(seeded_project) as c:
        types = {r[1]: r[2] for r in c.execute("PRAGMA table_info(assays)").fetchall()}
    assert types["p1_counted"] == "REAL"


def test_column_prefix_rejects_invalid_identifier(seeded_project: Path, tmp_path: Path, capsys):
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"], "val": [1.0],
    }))
    # Empty string is a legitimate "no prefix" signal and must NOT error.
    for bad in ("1merged", "merged space", "me-rged", "merged;drop"):
        with pytest.raises(SystemExit) as excinfo:
            casetrack.cmd_append(_append_ns(
                seeded_project, results=summary, analysis="x",
                column_prefix=bad,
            ))
        assert excinfo.value.code == 1
        assert "--column-prefix" in capsys.readouterr().err


def test_column_prefix_lets_two_analyses_coexist(seeded_project: Path, tmp_path: Path):
    """The whole point: run 'modkit' twice at different scopes, both sets of
    columns land cleanly, neither clobbers the other."""
    s1 = _write_summary(tmp_path / "s1.tsv", pd.DataFrame({
        "assay_id": ["A001"], "n_cpg": [2_400_000], "mean_meth": [0.65],
    }))
    s2 = _write_summary(tmp_path / "s2.tsv", pd.DataFrame({
        "assay_id": ["A001"], "n_cpg": [57_000_000], "mean_meth": [0.67],
    }))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=s1, analysis="modkit_chr17",
        column_prefix="chr17",
    ))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=s2, analysis="modkit_merged",
        column_prefix="merged",
    ))

    with _conn(seeded_project) as c:
        row = c.execute(
            "SELECT chr17_n_cpg, chr17_mean_meth, merged_n_cpg, merged_mean_meth "
            "FROM assays WHERE assay_id='A001'"
        ).fetchone()
    assert row == (2_400_000, 0.65, 57_000_000, 0.67)


def test_column_prefix_logged_to_provenance(seeded_project: Path, tmp_path: Path):
    import json as _json
    summary = _write_summary(tmp_path / "s.tsv", pd.DataFrame({
        "assay_id": ["A001"], "val": [1.0],
    }))
    casetrack.cmd_append(_append_ns(
        seeded_project, results=summary, analysis="m",
        column_prefix="merged",
    ))
    entries = [
        _json.loads(ln)
        for ln in (seeded_project / "provenance.jsonl").read_text().splitlines()
    ]
    ap = next(e for e in entries if e["action"] == "append")
    assert ap["column_prefix"] == "merged"
    assert ap["prefix_rename"] == {"val": "merged_val"}
