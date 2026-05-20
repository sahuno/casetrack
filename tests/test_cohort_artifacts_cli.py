"""Tests for the cohort-artifact CLI commands.

Proposal 0009 §6.3: migrate-cohort, append-cohort, cohort-artifacts.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-20
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import casetrack
from casetrack_qc import cohort_artifacts as ca
from casetrack_qc.cohort_artifacts_cli import (
    cmd_append_cohort,
    cmd_cohort_artifacts,
    cmd_migrate_cohort,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _init_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    ns = argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    )
    casetrack.cmd_init(ns)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES ('P1'), ('P2');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) VALUES "
                "  ('P1-t', 'P1', 'tumor'), ('P2-t', 'P2', 'tumor');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('P1-t-ONT', 'P1-t', 'ONT'),"
                "  ('P2-t-ONT', 'P2-t', 'ONT');"
            )
    finally:
        conn.close()
    return proj


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    return _init_project(tmp_path)


def _provenance(proj: Path) -> list[dict]:
    lines = (proj / "provenance.jsonl").read_text().splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def _append_ns(proj, **kw):
    base = dict(
        project_dir=str(proj), analysis="joint_genotype", run_tag="run1",
        path="/cohort.vcf.gz", inputs="P1-t-ONT,P2-t-ONT", inputs_from=None,
        stats=None, checksum=None, created_by=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


# ── append-cohort (init already creates the schema) ─────────────────────────


def test_append_cohort_creates_artifact_and_inputs(proj, capsys):
    cmd_append_cohort(_append_ns(proj))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "joint_genotype", "run1")
        assert art is not None
        assert art.n_inputs == 2
        assert ca.artifact_inputs(conn, art.artifact_id) == ["P1-t-ONT", "P2-t-ONT"]
    finally:
        conn.close()


def test_append_cohort_writes_provenance(proj):
    cmd_append_cohort(_append_ns(proj))
    last = _provenance(proj)[-1]
    assert last["action"] == "append_cohort"
    assert last["analysis"] == "joint_genotype"
    assert last["run_tag"] == "run1"
    assert sorted(last["inputs"]) == ["P1-t-ONT", "P2-t-ONT"]


def test_append_cohort_inputs_from_file(proj, tmp_path):
    manifest = tmp_path / "inputs.txt"
    manifest.write_text("assay_id\nP1-t-ONT\nP2-t-ONT\n")
    cmd_append_cohort(_append_ns(proj, inputs=None, inputs_from=str(manifest)))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "joint_genotype", "run1")
        assert ca.artifact_inputs(conn, art.artifact_id) == ["P1-t-ONT", "P2-t-ONT"]
    finally:
        conn.close()


def test_append_cohort_stats_file(proj, tmp_path):
    stats = tmp_path / "stats.json"
    stats.write_text('{"ti_tv": 2.04, "n_variants": 4800000}')
    cmd_append_cohort(_append_ns(proj, stats=str(stats)))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "joint_genotype", "run1")
        assert json.loads(art.stats_json)["ti_tv"] == 2.04
    finally:
        conn.close()


def test_append_cohort_duplicate_run_tag_exits(proj):
    cmd_append_cohort(_append_ns(proj))
    with pytest.raises(SystemExit):
        cmd_append_cohort(_append_ns(proj, path="/other.vcf.gz"))


def test_append_cohort_unknown_assay_exits(proj):
    with pytest.raises(SystemExit):
        cmd_append_cohort(_append_ns(proj, inputs="P1-t-ONT,ghost"))


# ── cohort-artifacts (read + staleness) ──────────────────────────────────────


def test_cohort_artifacts_lists_fresh(proj, capsys):
    cmd_append_cohort(_append_ns(proj))
    capsys.readouterr()  # drain the append-cohort banner
    cmd_cohort_artifacts(argparse.Namespace(
        project_dir=str(proj), fmt="json", stale_only=False))
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["run_tag"] == "run1"
    assert out[0]["stale"] is False
    assert out[0]["n_censored_inputs"] == 0


def test_cohort_artifacts_flags_stale(proj, capsys):
    cmd_append_cohort(_append_ns(proj))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute(
                "UPDATE assays SET qc_status='censored' WHERE assay_id='P1-t-ONT'"
            )
    finally:
        conn.close()
    capsys.readouterr()  # drain prior output
    cmd_cohort_artifacts(argparse.Namespace(
        project_dir=str(proj), fmt="json", stale_only=False))
    out = json.loads(capsys.readouterr().out)
    assert out[0]["stale"] is True
    assert out[0]["n_censored_inputs"] == 1
    assert out[0]["censored_inputs"] == ["P1-t-ONT"]


def test_cohort_artifacts_stale_only_filters(proj, capsys):
    cmd_append_cohort(_append_ns(proj))
    capsys.readouterr()  # drain the append-cohort banner
    cmd_cohort_artifacts(argparse.Namespace(
        project_dir=str(proj), fmt="json", stale_only=True))
    out = json.loads(capsys.readouterr().out)
    assert out == []


# ── migrate-cohort (existing project lacking the tables) ────────────────────


def test_migrate_cohort_creates_tables(tmp_path):
    proj = _init_project(tmp_path)
    # Drop the tables to simulate a pre-0009 project.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute("DROP TABLE cohort_artifact_inputs")
            conn.execute("DROP TABLE cohort_artifacts")
    finally:
        conn.close()
    cmd_migrate_cohort(argparse.Namespace(project_dir=str(proj), dry_run=False))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert ca.cohort_artifacts_schema_exists(conn)
    finally:
        conn.close()
    assert _provenance(proj)[-1]["action"] == "migrate_cohort"


def test_migrate_cohort_dry_run_no_change(tmp_path):
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute("DROP TABLE cohort_artifact_inputs")
            conn.execute("DROP TABLE cohort_artifacts")
    finally:
        conn.close()
    cmd_migrate_cohort(argparse.Namespace(project_dir=str(proj), dry_run=True))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert not ca.cohort_artifacts_schema_exists(conn)
    finally:
        conn.close()
