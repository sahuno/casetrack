"""Tests for v0.3 project-mode read paths (proposal 0001 §7.1).

Covers `status --project-dir` with all four --group-by modes,
`validate --project-dir` (TOML↔DB drift, FK integrity, orphan _done
columns), and `log --project-dir` with --level / --transaction filters.

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
        project_dir=str(project_dir), level=level, id=id, parent=parent,
        meta=meta, allow_new_parent=allow_new_parent, yes=yes,
    )


def _append_ns(project_dir: Path, *, results: Path, analysis: str,
               level: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        results=str(results),
        key="sample_id",
        analysis=analysis,
        level=level,
        col_type=None,
        overwrite=False,
        allow_new=False,
        yes=False,
    )


def _status_ns(project_dir: Path, *, group_by: str | None = None,
               fmt: str = "table") -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        key="sample_id",
        analysis=None,
        group_by=group_by,
        fmt=fmt,
    )


def _validate_ns(project_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), key="sample_id",
    )


def _log_ns(project_dir: Path, *, level: str | None = None,
            transaction: str | None = None, last: int | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir),
        last=last, level=level, transaction=transaction,
    )


@pytest.fixture
def cohort_project(tmp_path: Path) -> Path:
    """2 patients × 3 specimens × 4 assays, modkit done on A1+A2, variant done on A3."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    for pid in ("P001", "P002"):
        casetrack.cmd_register(_reg_ns(proj, level="patient", id=pid, meta="age=55,sex=F"))
    # P001 has specimens S1, S2; P002 has specimen S3.
    spec_plan = [("S1", "P001"), ("S2", "P001"), ("S3", "P002")]
    for sid, pid in spec_plan:
        casetrack.cmd_register(_reg_ns(
            proj, level="specimen", id=sid, parent=pid, meta="tissue_site=tumor"
        ))
    # 4 assays, 2 per specimen for S1, 1 each for S2 and S3.
    assay_plan = [("A1", "S1"), ("A2", "S1"), ("A3", "S2"), ("A4", "S3")]
    for aid, sid in assay_plan:
        casetrack.cmd_register(_reg_ns(
            proj, level="assay", id=aid, parent=sid, meta="assay_type=WGS"
        ))

    # Append modkit to A1+A2.
    mod = proj / "modkit.tsv"
    pd.DataFrame({"assay_id": ["A1", "A2"],
                  "mean_meth": [0.7, 0.6]}).to_csv(mod, sep="\t", index=False)
    casetrack.cmd_append(_append_ns(proj, results=mod, analysis="modkit"))

    # Append variant to A3.
    var = proj / "variant.tsv"
    pd.DataFrame({"assay_id": ["A3"], "n_snvs": [12345]}).to_csv(var, sep="\t", index=False)
    casetrack.cmd_append(_append_ns(proj, results=var, analysis="variant"))

    return proj


# ── status ────────────────────────────────────────────────────────────────────


def test_status_group_by_analysis_default(cohort_project: Path, capsys):
    casetrack.cmd_status(_status_ns(cohort_project))
    out = capsys.readouterr().out
    assert "Group by:  analysis" in out
    assert "modkit" in out
    assert "variant" in out
    assert "50.0%" in out  # modkit: 2 of 4 assays done
    assert "25.0%" in out  # variant: 1 of 4 assays done


def test_status_group_by_analysis_json(cohort_project: Path, capsys):
    casetrack.cmd_status(_status_ns(cohort_project, fmt="json"))
    data = json.loads(capsys.readouterr().out)
    by_analysis = {r["analysis"]: r for r in data}
    assert by_analysis["modkit"]["done"] == 2
    assert by_analysis["modkit"]["total"] == 4
    assert by_analysis["variant"]["done"] == 1


def test_status_group_by_assay(cohort_project: Path, capsys):
    casetrack.cmd_status(_status_ns(cohort_project, group_by="assay", fmt="json"))
    rows = json.loads(capsys.readouterr().out)
    by_assay = {r["assay_id"]: r for r in rows}
    assert set(by_assay["A1"]["analyses_done"]) == {"modkit"}
    assert set(by_assay["A3"]["analyses_done"]) == {"variant"}
    assert by_assay["A4"]["analyses_done"] == []
    assert by_assay["A1"]["n_total"] == 2  # two assay-level analyses exist overall


def test_status_group_by_specimen(cohort_project: Path, capsys):
    casetrack.cmd_status(_status_ns(cohort_project, group_by="specimen", fmt="json"))
    rows = json.loads(capsys.readouterr().out)
    by_spec = {r["specimen_id"]: r for r in rows}
    assert by_spec["S1"]["n_assays"] == 2
    # S1 has 2 assays, both have modkit done:
    assert by_spec["S1"]["assay_analyses_done"]["modkit"] == 2
    assert by_spec["S1"]["assay_analyses_done"]["variant"] == 0
    # S2 has 1 assay, variant done:
    assert by_spec["S2"]["assay_analyses_done"]["variant"] == 1


def test_status_group_by_patient(cohort_project: Path, capsys):
    casetrack.cmd_status(_status_ns(cohort_project, group_by="patient", fmt="json"))
    rows = json.loads(capsys.readouterr().out)
    by_pat = {r["patient_id"]: r for r in rows}
    assert by_pat["P001"]["n_specimens"] == 2
    assert by_pat["P001"]["n_assays"] == 3  # A1, A2, A3
    assert by_pat["P001"]["assay_analyses_done"]["modkit"] == 2
    assert by_pat["P001"]["assay_analyses_done"]["variant"] == 1
    assert by_pat["P002"]["n_assays"] == 1  # just A4


def test_status_group_by_assay_tsv(cohort_project: Path, capsys):
    """TSV output should have a header + one line per entity."""
    casetrack.cmd_status(_status_ns(cohort_project, group_by="assay", fmt="tsv"))
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0].startswith("assay_id")
    # 4 data lines.
    assert len(out) == 5


def test_status_on_empty_project_is_graceful(tmp_path: Path, capsys):
    proj = tmp_path / "empty"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    casetrack.cmd_status(_status_ns(proj))
    out = capsys.readouterr().out
    assert "Counts:    patients=0" in out


# ── validate ──────────────────────────────────────────────────────────────────


def test_validate_clean_project_passes(cohort_project: Path, capsys):
    casetrack.cmd_validate(_validate_ns(cohort_project))
    out = capsys.readouterr().out
    assert "Project OK" in out


def test_validate_detects_fk_orphan(cohort_project: Path, capsys):
    """Manually delete a patient so its specimens become orphans."""
    # Disable FKs just for the destructive surgery.
    conn = sqlite3.connect(str(cohort_project / "casetrack.db"))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM patients WHERE patient_id = 'P001'")
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_validate(_validate_ns(cohort_project))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "orphan" in err.lower()


def test_validate_detects_toml_db_drift(cohort_project: Path, capsys):
    """If the TOML declares a column that doesn't exist in the DB, flag it."""
    toml_path = cohort_project / "casetrack.toml"
    text = toml_path.read_text()
    # Add a never-created column under patient.
    patched = text.replace(
        "[levels.specimen]",
        'ghost_column = { type = "INTEGER" }\n\n[levels.specimen]',
        1,
    )
    toml_path.write_text(patched)

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_validate(_validate_ns(cohort_project))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "ghost_column" in err
    assert "declared in TOML but missing in DB" in err


def test_validate_catches_orphan_done_column(cohort_project: Path, capsys):
    """A _done column with a non-null value but no data companion should flag."""
    conn = casetrack.open_project_db(cohort_project / "casetrack.db")
    conn.execute("ALTER TABLE assays ADD COLUMN ghost_done TEXT")
    conn.execute("UPDATE assays SET ghost_done = '2026-01-01' WHERE assay_id='A1'")
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_validate(_validate_ns(cohort_project))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "ghost_done" in err


def test_collect_analysis_columns_from_provenance(cohort_project: Path):
    cols = casetrack._collect_analysis_columns_from_provenance(
        cohort_project / "provenance.jsonl"
    )
    assert "mean_meth" in cols["modkit"]
    assert "modkit_done" in cols["modkit"]
    assert "n_snvs" in cols["variant"]


# ── log ───────────────────────────────────────────────────────────────────────


def test_log_default_shows_all_entries(cohort_project: Path, capsys):
    casetrack.cmd_log(_log_ns(cohort_project))
    out = capsys.readouterr().out
    assert "INIT_PROJECT" in out
    assert "REGISTER patient" in out
    assert "APPEND assay" in out


def test_log_filter_by_level(cohort_project: Path, capsys):
    casetrack.cmd_log(_log_ns(cohort_project, level="patient"))
    out = capsys.readouterr().out
    assert "REGISTER patient" in out
    # No assay / specimen entries should sneak through.
    assert "REGISTER assay" not in out
    assert "APPEND assay" not in out


def test_log_filter_by_transaction(cohort_project: Path, capsys):
    """Grab a txn_id from an append entry and re-run filtered by it."""
    lines = (cohort_project / "provenance.jsonl").read_text().splitlines()
    txn = next(
        json.loads(ln)["transaction_id"]
        for ln in lines if ln.strip() and json.loads(ln)["action"] == "append"
    )
    casetrack.cmd_log(_log_ns(cohort_project, transaction=txn))
    out = capsys.readouterr().out
    assert out.count(txn) >= 1
    # Exactly one matching line (APPEND).
    assert out.count("APPEND") == 1


def test_log_last_n_truncates(cohort_project: Path, capsys):
    casetrack.cmd_log(_log_ns(cohort_project, last=2))
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2


def test_log_no_matches_message(cohort_project: Path, capsys):
    casetrack.cmd_log(_log_ns(cohort_project, transaction="txn_ghost"))
    out = capsys.readouterr().out
    assert "no matching" in out


def test_log_missing_file_exits(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    (proj / "provenance.jsonl").unlink()

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_log(_log_ns(proj))
    assert excinfo.value.code == 1
    assert "No provenance log" in capsys.readouterr().err


# ── Flat-mode dispatch ────────────────────────────────────────────────────────


def test_flat_status_still_works(initialized_manifest: Path, capsys):
    """v0.2 flat path routes through the new dispatcher."""
    ns = argparse.Namespace(
        manifest=str(initialized_manifest),
        project_dir=None,
        key="sample_id",
        analysis=None,
        group_by=None,
        fmt="table",
    )
    casetrack.cmd_status(ns)
    out = capsys.readouterr().out
    assert "Manifest:" in out  # flat-mode banner, not the project-mode one


def test_flat_validate_still_works(initialized_manifest: Path, capsys):
    ns = argparse.Namespace(
        manifest=str(initialized_manifest),
        project_dir=None,
        key="sample_id",
    )
    casetrack.cmd_validate(ns)
    out = capsys.readouterr().out
    assert "Manifest OK" in out


def test_flat_log_still_works(initialized_manifest: Path, capsys):
    ns = argparse.Namespace(
        manifest=str(initialized_manifest),
        project_dir=None,
        last=None,
        level=None,
        transaction=None,
    )
    casetrack.cmd_log(ns)
    out = capsys.readouterr().out
    assert "INIT" in out.upper()
