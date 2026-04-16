"""Tests for v0.3 read paths: `query --project-dir` and `export --project-dir`.

query: DuckDB ATTACH of casetrack.db, with helper views for each level
and the `_` join. Tables are read-only so queries can never corrupt
a live WAL.

export: --shape {tables,joined}, --tables subset, --sql passthrough,
format inferred from --output extension.

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


def _init_ns(project_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc", project_name=None, force=False,
    )


def _reg_ns(project_dir: Path, *, level: str, id: str,
            parent: str | None = None, meta: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir), level=level, id=id, parent=parent,
        meta=meta, allow_new_parent=False, yes=False,
    )


def _append_ns(project_dir: Path, results: Path, analysis: str) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), results=str(results),
        key="sample_id", analysis=analysis, level=None, col_type=None,
        overwrite=False, allow_new=False, yes=False,
    )


def _query_ns(project_dir: Path, sql: str, *,
              fmt: str = "table", output: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir), manifest=None, root=None,
        sql=sql, as_name=None, pattern="manifest.tsv", max_depth=4,
        fmt=fmt, output=output,
    )


def _export_ns(project_dir: Path, *, output: Path, shape: str | None = None,
               tables: str | None = None, sql: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir), manifest=None,
        output=str(output), shape=shape, tables=tables, sql=sql,
    )


@pytest.fixture
def cohort(tmp_path: Path) -> Path:
    """2 patients × 2 specimens × 3 assays with some modkit completions."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P1",
                                    meta="age=55,sex=F,brca_status=brca1"))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P2",
                                    meta="age=60,sex=F,brca_status=wt"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S1", parent="P1",
                                    meta="tissue_site=tumor"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S2", parent="P2",
                                    meta="tissue_site=tumor"))
    for aid, sid in (("A1", "S1"), ("A2", "S1"), ("A3", "S2")):
        casetrack.cmd_register(_reg_ns(
            proj, level="assay", id=aid, parent=sid, meta="assay_type=WGS"
        ))

    mod = proj / "modkit.tsv"
    pd.DataFrame({"assay_id": ["A1", "A2"],
                  "mean_meth": [0.7, 0.6]}).to_csv(mod, sep="\t", index=False)
    casetrack.cmd_append(_append_ns(proj, mod, "modkit"))

    return proj


# ── query ─────────────────────────────────────────────────────────────────────


def test_query_exposes_three_tables(cohort: Path, capsys):
    casetrack.cmd_query(_query_ns(
        cohort, "SELECT COUNT(*) AS n FROM patients", fmt="json",
    ))
    data = json.loads(capsys.readouterr().out)
    assert data == [{"n": 2}]


def test_query_joined_view(cohort: Path, capsys):
    casetrack.cmd_query(_query_ns(
        cohort,
        'SELECT patient_id, assay_id, mean_meth FROM "_" ORDER BY assay_id',
        fmt="json",
    ))
    data = json.loads(capsys.readouterr().out)
    # Joined view = assays⋈specimens⋈patients. 3 assays total.
    assert len(data) == 3
    a1 = next(r for r in data if r["assay_id"] == "A1")
    assert a1["patient_id"] == "P1"
    assert a1["mean_meth"] == 0.7


def test_query_tsv_output(cohort: Path, capsys):
    casetrack.cmd_query(_query_ns(
        cohort,
        "SELECT assay_id, assay_type FROM assays ORDER BY assay_id",
        fmt="tsv",
    ))
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "assay_id\tassay_type"
    assert out[1] == "A1\tWGS"


def test_query_csv_output(cohort: Path, capsys):
    casetrack.cmd_query(_query_ns(
        cohort,
        "SELECT patient_id, age FROM patients ORDER BY patient_id",
        fmt="csv",
    ))
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "patient_id,age"


def test_query_writes_to_output_file(cohort: Path, tmp_path: Path):
    out_path = tmp_path / "q.json"
    casetrack.cmd_query(_query_ns(
        cohort,
        "SELECT COUNT(*) AS n FROM assays", fmt="json", output=str(out_path),
    ))
    data = json.loads(out_path.read_text())
    assert data == [{"n": 3}]


def test_query_bad_sql_exits_two(cohort: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_query(_query_ns(cohort, "SELECT * FROM nonexistent"))
    assert excinfo.value.code == 2
    assert "SQL failed" in capsys.readouterr().err


def test_query_read_only_attach(cohort: Path, capsys):
    """Writes to the attached SQLite must be rejected — the bare views are
    read-only, and INSERT against the attached DB (`proj.patients`) must
    fail because ATTACH was declared READ_ONLY."""
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_query(_query_ns(
            cohort,
            "INSERT INTO proj.patients (patient_id) VALUES ('GHOST')",
        ))
    assert excinfo.value.code == 2
    err = capsys.readouterr().err.lower()
    assert "read" in err and "only" in err


# ── export --shape tables ─────────────────────────────────────────────────────


def test_export_tables_to_directory(cohort: Path, tmp_path: Path):
    out_dir = tmp_path / "exports"
    casetrack.cmd_export(_export_ns(cohort, output=out_dir))
    for name in ("patients.tsv", "specimens.tsv", "assays.tsv"):
        assert (out_dir / name).exists(), name
    assays = pd.read_csv(out_dir / "assays.tsv", sep="\t")
    assert len(assays) == 3
    assert "mean_meth" in assays.columns  # analysis-added column travels with the table


def test_export_tables_with_prefix(cohort: Path, tmp_path: Path):
    """Passing a file with a known extension writes PREFIX.<table>.<ext>."""
    prefix = tmp_path / "pfx.csv"
    casetrack.cmd_export(_export_ns(cohort, output=prefix))
    assert (tmp_path / "pfx.patients.csv").exists()
    assert (tmp_path / "pfx.assays.csv").exists()


def test_export_tables_subset(cohort: Path, tmp_path: Path):
    out_dir = tmp_path / "exports"
    casetrack.cmd_export(_export_ns(cohort, output=out_dir, tables="patients,assays"))
    assert (out_dir / "patients.tsv").exists()
    assert (out_dir / "assays.tsv").exists()
    assert not (out_dir / "specimens.tsv").exists()


def test_export_unknown_table_errors(cohort: Path, tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_export(_export_ns(
            cohort, output=tmp_path / "x/", tables="patients,ghost",
        ))
    assert excinfo.value.code == 1
    assert "ghost" in capsys.readouterr().err


# ── export --shape joined ─────────────────────────────────────────────────────


def test_export_joined_writes_single_file(cohort: Path, tmp_path: Path):
    out = tmp_path / "joined.tsv"
    casetrack.cmd_export(_export_ns(cohort, output=out, shape="joined"))
    df = pd.read_csv(out, sep="\t")
    # 3 assays × all ancestors inlined.
    assert len(df) == 3
    assert "patient_id" in df.columns and "specimen_id" in df.columns
    assert "age" in df.columns  # patient metadata present


# ── export --sql passthrough ──────────────────────────────────────────────────


def test_export_sql_overrides_shape(cohort: Path, tmp_path: Path):
    out = tmp_path / "custom.csv"
    casetrack.cmd_export(_export_ns(
        cohort, output=out,
        sql="SELECT assay_id, mean_meth FROM assays WHERE mean_meth IS NOT NULL",
    ))
    df = pd.read_csv(out)
    assert list(df["assay_id"]) == ["A1", "A2"]


# ── format inference ──────────────────────────────────────────────────────────


def test_export_json_format(cohort: Path, tmp_path: Path):
    out = tmp_path / "joined.json"
    casetrack.cmd_export(_export_ns(cohort, output=out, shape="joined"))
    data = json.loads(out.read_text())
    assert len(data) == 3


def test_export_unsupported_extension(cohort: Path, tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_export(_export_ns(
            cohort, output=tmp_path / "x.yaml", shape="joined",
        ))
    assert excinfo.value.code == 1
    assert "unsupported" in capsys.readouterr().err


# ── flat mode still works ─────────────────────────────────────────────────────


def test_flat_query_still_works(initialized_manifest: Path, capsys):
    ns = argparse.Namespace(
        project_dir=None, manifest=str(initialized_manifest), root=None,
        sql="SELECT COUNT(*) AS n FROM _", as_name=None,
        pattern="manifest.tsv", max_depth=4, fmt="json", output=None,
    )
    casetrack.cmd_query(ns)
    data = json.loads(capsys.readouterr().out)
    assert data[0]["n"] == 5  # samples_file fixture has 5 rows


def test_flat_export_still_works(initialized_manifest: Path, tmp_path: Path):
    out = tmp_path / "flat.csv"
    ns = argparse.Namespace(
        project_dir=None, manifest=str(initialized_manifest),
        output=str(out), shape=None, tables=None, sql=None,
    )
    casetrack.cmd_export(ns)
    df = pd.read_csv(out)
    assert len(df) == 5
