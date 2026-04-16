"""Tests for `casetrack rerun --project-dir` and `casetrack projects --root`
with v0.3 project detection.

Author: Samuel Ahuno (ekwame001=gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import pytest

import casetrack


# ── helpers ───────────────────────────────────────────────────────────────────


def _init_ns(project_dir: Path, template: str = "hgsoc") -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None, key="sample_id",
        metadata=None, cols=None, from_template=template, project_name=None, force=False,
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


def _rerun_ns(project_dir: Path, *, analysis: str, script: str | None = None,
              level: str | None = None, list_only: bool = False,
              submit: bool = False, extra: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir),
        analysis=analysis, script=script, key="sample_id",
        level=level, list_only=list_only, submit=submit, extra=extra,
    )


def _projects_ns(root: Path, pattern: str = "manifest.tsv",
                 max_depth: int = 4, fmt: str = "table") -> argparse.Namespace:
    return argparse.Namespace(
        root=str(root), pattern=pattern, max_depth=max_depth,
        key="sample_id", fmt=fmt,
    )


@pytest.fixture
def three_assay_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    casetrack.cmd_register(_reg_ns(proj, level="patient", id="P1"))
    casetrack.cmd_register(_reg_ns(proj, level="specimen", id="S1", parent="P1",
                                   meta="tissue_site=tumor"))
    for aid in ("A1", "A2", "A3"):
        casetrack.cmd_register(_reg_ns(
            proj, level="assay", id=aid, parent="S1", meta="assay_type=WGS"
        ))
    # modkit done on A1 only.
    results = proj / "modkit.tsv"
    pd.DataFrame({"assay_id": ["A1"], "mean_meth": [0.7]}).to_csv(
        results, sep="\t", index=False
    )
    casetrack.cmd_append(_append_ns(proj, results, "modkit"))
    return proj


# ── rerun ─────────────────────────────────────────────────────────────────────


def test_rerun_list_only(three_assay_project: Path, capsys):
    casetrack.cmd_rerun(_rerun_ns(three_assay_project, analysis="modkit", list_only=True))
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["A2", "A3"]


def test_rerun_emits_sbatch_with_env(three_assay_project: Path, capsys):
    casetrack.cmd_rerun(_rerun_ns(
        three_assay_project, analysis="modkit", script="./run_modkit.sh",
    ))
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    assert "ASSAY_ID=A2" in out[0]
    assert "ASSAY_ID=A3" in out[1]
    assert str(three_assay_project) in out[0]
    assert "./run_modkit.sh" in out[0]


def test_rerun_with_extra(three_assay_project: Path, capsys):
    casetrack.cmd_rerun(_rerun_ns(
        three_assay_project, analysis="modkit", script="./run.sh",
        extra="--partition short --mem 4G",
    ))
    out = capsys.readouterr().out
    assert "--partition short --mem 4G" in out


def test_rerun_unknown_analysis_treats_all_as_missing(three_assay_project: Path, capsys):
    """If the analysis has never been run, every row is 'missing'."""
    casetrack.cmd_rerun(_rerun_ns(
        three_assay_project, analysis="variant", list_only=True,
    ))
    out = capsys.readouterr().out.strip().splitlines()
    assert set(out) == {"A1", "A2", "A3"}


def test_rerun_no_missing(three_assay_project: Path, tmp_path: Path, capsys):
    """Finish modkit on A2 + A3 too; rerun should report nothing to do."""
    rest = tmp_path / "rest.tsv"
    pd.DataFrame({"assay_id": ["A2", "A3"], "mean_meth": [0.6, 0.5]}).to_csv(
        rest, sep="\t", index=False
    )
    casetrack.cmd_append(_append_ns(three_assay_project, rest, "modkit"))

    casetrack.cmd_rerun(_rerun_ns(three_assay_project, analysis="modkit", list_only=True))
    out = capsys.readouterr().out
    assert "No" in out and "missing" in out


def test_rerun_script_required_unless_list_only(three_assay_project: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_rerun(_rerun_ns(
            three_assay_project, analysis="modkit", script=None, list_only=False,
        ))
    assert excinfo.value.code == 1
    assert "--script is required" in capsys.readouterr().err


def test_rerun_at_patient_level(three_assay_project: Path, tmp_path: Path, capsys):
    """Level override — rerun TMB analysis at patient level."""
    # Append a patient-level analysis.
    tsv = tmp_path / "tmb.tsv"
    pd.DataFrame({"patient_id": ["P1"], "tmb": [12.5]}).to_csv(tsv, sep="\t", index=False)
    casetrack.cmd_append(argparse.Namespace(
        manifest=None, project_dir=str(three_assay_project), results=str(tsv),
        key="sample_id", analysis="tmb_calc", level="patient", col_type=None,
        overwrite=False, allow_new=False, yes=False,
    ))

    # P1 has tmb done; re-run for patients without tmb → zero.
    casetrack.cmd_rerun(_rerun_ns(
        three_assay_project, analysis="tmb_calc", level="patient", list_only=True,
    ))
    out = capsys.readouterr().out
    assert "No patients missing" in out


# ── projects --root with v0.3 detection ───────────────────────────────────────


def test_projects_finds_v03_project(tmp_path: Path, capsys):
    """A fresh v0.3 project shows up in the overview with kind='v0.3'."""
    proj = tmp_path / "demo_project"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    capsys.readouterr()  # drain init's output before isolating the projects stdout

    casetrack.cmd_projects(_projects_ns(tmp_path, fmt="json"))
    data = json.loads(capsys.readouterr().out)
    hit = [p for p in data if p["name"] == "demo_project"]
    assert len(hit) == 1
    assert hit[0]["kind"] == "v0.3"
    assert hit[0]["patients"] == 0
    assert hit[0]["schema_v"] == 1


def test_projects_table_shows_kind_column(three_assay_project: Path, capsys):
    parent = three_assay_project.parent
    casetrack.cmd_projects(_projects_ns(parent))
    out = capsys.readouterr().out
    assert "Kind" in out
    assert "v0.3" in out


def test_projects_tsv_includes_kind(three_assay_project: Path, capsys):
    parent = three_assay_project.parent
    casetrack.cmd_projects(_projects_ns(parent, fmt="tsv"))
    out = capsys.readouterr().out.strip().splitlines()
    header = out[0].split("\t")
    assert "kind" in header
    # The project-mode row uses kind=v0.3.
    kind_idx = header.index("kind")
    assert any(line.split("\t")[kind_idx] == "v0.3" for line in out[1:])


def test_projects_skips_sandbox_source_manifest(tmp_path: Path, capsys):
    """A v0.3 project created by `migrate` contains sandbox/source_manifest.tsv
    — the flat scanner must not count it as a second (v0.2) project."""
    flat = tmp_path / "flat.tsv"
    pd.DataFrame([
        {"patient_id": "P1", "specimen_id": "S1", "assay_id": "A1",
         "age": 55, "assay_type": "WGS"},
    ]).to_csv(flat, sep="\t", index=False)

    casetrack.cmd_migrate(argparse.Namespace(
        flat=str(flat), out_dir=str(tmp_path / "migrated"),
        patient_col="patient_id", specimen_col="specimen_id", assay_col="assay_id",
        metadata_map=None, project_name=None, force=False,
    ))
    capsys.readouterr()  # drain migrate's stdout

    casetrack.cmd_projects(_projects_ns(tmp_path, fmt="json"))
    data = json.loads(capsys.readouterr().out)
    names_kinds = {(p["name"], p["kind"]) for p in data}
    # Exactly one entry for the migrated project — not two (would be a dup from sandbox).
    migrated_entries = [p for p in data if "migrated" in p["name"]]
    assert len(migrated_entries) == 1
    assert migrated_entries[0]["kind"] == "v0.3"


def test_projects_v03_counts_completed_cells(three_assay_project: Path, capsys):
    parent = three_assay_project.parent
    casetrack.cmd_projects(_projects_ns(parent, fmt="json"))
    data = json.loads(capsys.readouterr().out)
    entry = next(p for p in data if p["name"] == three_assay_project.name)
    # 1 analysis (modkit) × 3 assays = 3 cells; 1 completed (A1).
    assert entry["completed_cells"] == 1
    assert entry["total_cells"] == 3
    assert entry["pct"] == pytest.approx(33.3)
