"""Tests for the tool-first results layout + path inference (v0.5).

Covers:
  * casetrack.toml [layout] / [analyses] validation (happy + error paths)
  * casetrack_qc.path_infer.find_project_root walk-up
  * casetrack_qc.path_infer.infer_from_path — level match, tool unknown,
    level mismatch, outside results_root, deepest-first template match
  * `casetrack append --infer-from-path` end-to-end: inferred project_dir,
    level, analysis, column_prefix, results; injected run_tag column;
    provenance shape.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-18
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from casetrack_qc.path_infer import (
    InferenceError,
    find_project_root,
    infer_from_path,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


def _init_ns(project_dir: Path, template: str = "hgsoc") -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None,
        key="sample_id", metadata=None, cols=None, from_template=template,
        project_name=None, force=False, bare=True,
    )


def _reg_ns(project_dir: Path, *, level: str, id: str,
            parent: str | None = None, meta: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir), level=level, id=id, parent=parent,
        meta=meta, allow_new_parent=False, yes=False,
    )


def _append_ns(**overrides) -> argparse.Namespace:
    defaults = dict(
        manifest=None, project_dir=None, results=None, key="sample_id",
        analysis=None, level=None, col_type=None, column_prefix=None,
        overwrite=False, allow_new=False, yes=False,
        force_append_on_censored=False, infer_from_path=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def seeded_project(tmp_path: Path) -> Path:
    """Project with one P01 patient / primary specimen / ONT1 assay,
    plus [analyses.modkit_pileup] declared."""
    proj = tmp_path / "proj"
    casetrack.cmd_init_project(_init_ns(proj, template="hgsoc"))

    # Append a concrete tool declaration to the TOML.
    toml = proj / "casetrack.toml"
    toml.write_text(
        toml.read_text()
        + '\n[analyses.modkit_pileup]\n'
          'level         = "assay"\n'
          'column_prefix = "modkit"\n'
          'summary_tsv   = "modkit_summary.tsv"\n'
          '\n[analyses.cohort_dmr]\n'
          'level         = "patient"\n'
          'column_prefix = "dmr"\n'
          'summary_tsv   = "dmr_cohort.tsv"\n'
    )

    for level, id_, parent, meta in [
        ("patient", "P01", None, "age=55,sex=F"),
        ("specimen", "P01_primary", "P01", "tissue_site=tumor"),
        ("assay", "P01_primary_ONT1", "P01_primary", "assay_type=ONT"),
    ]:
        casetrack.cmd_register(_reg_ns(
            proj, level=level, id=id_, parent=parent, meta=meta
        ))
    return proj


def _make_leaf(proj: Path, tool: str, run_tag: str, *ids: str) -> Path:
    leaf = proj / "results" / tool / run_tag / Path(*ids)
    leaf.mkdir(parents=True)
    return leaf


# ── [layout] / [analyses] validation ──────────────────────────────────────────


def test_layout_happy_path(seeded_project: Path) -> None:
    """The hgsoc template + a single [analyses.<tool>] validates cleanly."""
    schema = casetrack.load_schema(seeded_project / "casetrack.toml")
    assert schema["layout"]["results_dir"] == "results"
    assert "assay" in schema["layout"]["path_templates"]
    assert schema["analyses"]["modkit_pileup"]["column_prefix"] == "modkit"


def test_layout_rejects_unknown_placeholder(tmp_path: Path) -> None:
    toml = tmp_path / "casetrack.toml"
    toml.write_text(
        '[project]\nname="x"\nschema_v=1\n'
        '[levels.patient]\nkey="patient_id"\n'
        '[levels.patient.columns]\npatient_id={type="TEXT",required=true,unique=true}\n'
        '[levels.specimen]\nkey="specimen_id"\nparent="patient"\nparent_key="patient_id"\n'
        '[levels.specimen.columns]\n'
        'specimen_id={type="TEXT",required=true,unique=true}\n'
        'patient_id={type="TEXT",required=true}\n'
        '[levels.assay]\nkey="assay_id"\nparent="specimen"\nparent_key="specimen_id"\n'
        '[levels.assay.columns]\n'
        'assay_id={type="TEXT",required=true,unique=true}\n'
        'specimen_id={type="TEXT",required=true}\n'
        '[layout]\nresults_dir="results"\n'
        '[layout.path_templates]\n'
        'patient="{tool}/{run_tag}/{patient_id}"\n'
        'specimen="{tool}/{run_tag}/{patient_id}/{specimen_id}"\n'
        'assay="{tool}/{run_tag}/{bogus}/{assay_id}"\n'
    )
    with pytest.raises(casetrack.SchemaError, match="unknown placeholder"):
        casetrack.load_schema(toml)


def test_layout_requires_tool_placeholder(tmp_path: Path) -> None:
    toml = tmp_path / "casetrack.toml"
    base = casetrack.TEMPLATES["blank"]("x")
    # Remove {tool} from the assay template
    broken = base.replace(
        'assay    = "{tool}/{run_tag}/{patient_id}/{specimen_id}/{assay_id}"',
        'assay    = "{run_tag}/{patient_id}/{specimen_id}/{assay_id}"',
    )
    toml.write_text(broken)
    with pytest.raises(casetrack.SchemaError, match=r"\{tool\} placeholder"):
        casetrack.load_schema(toml)


def test_analyses_rejects_bad_level(tmp_path: Path) -> None:
    toml = tmp_path / "casetrack.toml"
    toml.write_text(
        casetrack.TEMPLATES["blank"]("x")
        + '\n[analyses.foo]\nlevel = "cohort"\n'
    )
    with pytest.raises(casetrack.SchemaError, match="level='cohort'"):
        casetrack.load_schema(toml)


def test_analyses_rejects_bad_column_prefix(tmp_path: Path) -> None:
    toml = tmp_path / "casetrack.toml"
    toml.write_text(
        casetrack.TEMPLATES["blank"]("x")
        + '\n[analyses.foo]\nlevel = "assay"\ncolumn_prefix = "1bad"\n'
    )
    with pytest.raises(casetrack.SchemaError, match="column_prefix"):
        casetrack.load_schema(toml)


# ── find_project_root ─────────────────────────────────────────────────────────


def test_find_project_root_from_leaf(seeded_project: Path) -> None:
    leaf = _make_leaf(
        seeded_project, "modkit_pileup", "20260418_hg38_v1",
        "P01", "P01_primary", "P01_primary_ONT1",
    )
    assert find_project_root(leaf) == seeded_project.resolve()


def test_find_project_root_from_file(seeded_project: Path) -> None:
    leaf = _make_leaf(
        seeded_project, "modkit_pileup", "20260418_hg38_v1",
        "P01", "P01_primary", "P01_primary_ONT1",
    )
    f = leaf / "modkit_summary.tsv"
    f.write_text("x\n")
    assert find_project_root(f) == seeded_project.resolve()


def test_find_project_root_missing(tmp_path: Path) -> None:
    with pytest.raises(InferenceError, match="no casetrack.toml found"):
        find_project_root(tmp_path / "nowhere")


# ── infer_from_path ───────────────────────────────────────────────────────────


def test_infer_assay_level(seeded_project: Path) -> None:
    leaf = _make_leaf(
        seeded_project, "modkit_pileup", "20260418_hg38_v1",
        "P01", "P01_primary", "P01_primary_ONT1",
    )
    schema = casetrack.load_schema(seeded_project / "casetrack.toml")
    out = infer_from_path(seeded_project, leaf, schema)
    assert out["tool"] == "modkit_pileup"
    assert out["level"] == "assay"
    assert out["run_tag"] == "20260418_hg38_v1"
    assert out["patient_id"] == "P01"
    assert out["specimen_id"] == "P01_primary"
    assert out["assay_id"] == "P01_primary_ONT1"
    assert out["column_prefix"] == "modkit"


def test_infer_patient_level(seeded_project: Path) -> None:
    """A patient-level analysis matches a shorter path."""
    leaf = _make_leaf(
        seeded_project, "cohort_dmr", "20260418_hg38_v1", "P01",
    )
    schema = casetrack.load_schema(seeded_project / "casetrack.toml")
    out = infer_from_path(seeded_project, leaf, schema)
    assert out["level"] == "patient"
    assert out["patient_id"] == "P01"
    assert "specimen_id" not in out and "assay_id" not in out


def test_infer_deepest_template_wins(seeded_project: Path) -> None:
    """A path matching multiple templates picks the deepest (most specific)."""
    leaf = _make_leaf(
        seeded_project, "modkit_pileup", "20260418_hg38_v1",
        "P01", "P01_primary", "P01_primary_ONT1",
    )
    schema = casetrack.load_schema(seeded_project / "casetrack.toml")
    out = infer_from_path(seeded_project, leaf, schema)
    assert out["level"] == "assay"


def test_infer_unknown_tool(seeded_project: Path) -> None:
    leaf = _make_leaf(
        seeded_project, "not_in_toml", "20260418_hg38_v1",
        "P01", "P01_primary", "P01_primary_ONT1",
    )
    schema = casetrack.load_schema(seeded_project / "casetrack.toml")
    with pytest.raises(InferenceError, match="unknown tool"):
        infer_from_path(seeded_project, leaf, schema)


def test_infer_level_mismatch(seeded_project: Path) -> None:
    """modkit_pileup is assay-level; a patient-depth path for it is an error."""
    leaf = _make_leaf(seeded_project, "modkit_pileup", "20260418_hg38_v1", "P01")
    schema = casetrack.load_schema(seeded_project / "casetrack.toml")
    with pytest.raises(InferenceError, match="implies level='patient'"):
        infer_from_path(seeded_project, leaf, schema)


def test_infer_outside_results_root(seeded_project: Path, tmp_path: Path) -> None:
    schema = casetrack.load_schema(seeded_project / "casetrack.toml")
    with pytest.raises(InferenceError, match="not under results root"):
        infer_from_path(seeded_project, tmp_path / "elsewhere", schema)


def test_infer_from_sub_file(seeded_project: Path) -> None:
    """A file deeper than the template leaf still resolves (trailing /.*)."""
    leaf = _make_leaf(
        seeded_project, "modkit_pileup", "20260418_hg38_v1",
        "P01", "P01_primary", "P01_primary_ONT1",
    )
    sub = leaf / "logs"
    sub.mkdir()
    schema = casetrack.load_schema(seeded_project / "casetrack.toml")
    out = infer_from_path(seeded_project, sub, schema)
    assert out["level"] == "assay"
    assert out["leaf_dir"] == leaf.resolve()


# ── append --infer-from-path end-to-end ──────────────────────────────────────


def test_append_infer_from_path_end_to_end(seeded_project: Path, monkeypatch) -> None:
    leaf = _make_leaf(
        seeded_project, "modkit_pileup", "20260418_hg38_v1",
        "P01", "P01_primary", "P01_primary_ONT1",
    )
    pd.DataFrame({
        "assay_id":  ["P01_primary_ONT1"],
        "mean_meth": [0.72],
        "n_reads":   [1500000],
    }).to_csv(leaf / "modkit_summary.tsv", sep="\t", index=False)

    # Simulate `cd leaf; casetrack append --infer-from-path`.
    monkeypatch.chdir(leaf)
    casetrack.cmd_append(_append_ns(infer_from_path=""))

    conn = sqlite3.connect(seeded_project / "casetrack.db")
    row = dict(zip(
        [d[0] for d in conn.execute("SELECT * FROM assays").description],
        conn.execute("SELECT * FROM assays").fetchone(),
    ))
    conn.close()
    assert row["modkit_mean_meth"] == pytest.approx(0.72)
    assert row["modkit_n_reads"] == 1500000
    assert row["modkit_run_tag"] == "20260418_hg38_v1"
    assert row["modkit_pileup_done"]

    prov = [
        json.loads(line) for line in
        (seeded_project / "provenance.jsonl").read_text().splitlines()
    ]
    last = prov[-1]
    assert last["action"] == "append"
    assert last["analysis"] == "modkit_pileup"
    assert last["column_prefix"] == "modkit"
    assert last["run_tag"] == "20260418_hg38_v1"


def test_append_infer_missing_summary_tsv(seeded_project: Path, monkeypatch, capsys) -> None:
    leaf = _make_leaf(
        seeded_project, "modkit_pileup", "20260418_hg38_v1",
        "P01", "P01_primary", "P01_primary_ONT1",
    )
    # Deliberately don't write the summary TSV.
    monkeypatch.chdir(leaf)
    with pytest.raises(SystemExit):
        casetrack.cmd_append(_append_ns(infer_from_path=""))
    err = capsys.readouterr().err
    assert "expected summary TSV not found" in err


def test_append_infer_explicit_flags_override(seeded_project: Path, monkeypatch) -> None:
    """When both --infer-from-path and explicit flags are given, explicit wins."""
    leaf = _make_leaf(
        seeded_project, "modkit_pileup", "20260418_hg38_v1",
        "P01", "P01_primary", "P01_primary_ONT1",
    )
    pd.DataFrame({
        "assay_id":  ["P01_primary_ONT1"],
        "mean_meth": [0.99],
    }).to_csv(leaf / "modkit_summary.tsv", sep="\t", index=False)

    monkeypatch.chdir(leaf)
    casetrack.cmd_append(_append_ns(
        infer_from_path="",
        # Override the inferred tool name / prefix.
        analysis="my_custom_modkit",
        column_prefix="custom",
    ))

    conn = sqlite3.connect(seeded_project / "casetrack.db")
    cols = {d[0] for d in conn.execute("SELECT * FROM assays").description}
    conn.close()
    assert "custom_mean_meth" in cols
    assert "my_custom_modkit_done" in cols
    # The auto-injected run_tag still fires even with overrides.
    assert "custom_run_tag" in cols
