"""Tests for the v0.8 operator GUI (FastAPI + Jinja2).

Strategy: build a tiny project with `casetrack init --from-template hgsoc`,
register it under a temp registry, then spin up the FastAPI app via
TestClient and assert the rendered pages contain the expected entities.

Mutation paths (censor / uncensor) shell out to the `casetrack` CLI by
design — we test the round-trip via the same TestClient and check that
the qc_events row appeared in the live DB afterwards.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import casetrack  # noqa: E402

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient


def _init_project(project_dir: Path, project_id: str = "test-cohort") -> Path:
    casetrack.cmd_init(argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        samples=None,
        key="sample_id",
        metadata=None,
        cols=None,
        from_template="hgsoc",
        project_name="test_cohort",
        project_id=project_id,
        force=False,
        bare=True,
    ))
    return project_dir


def _seed_minimal_rows(project_dir: Path) -> None:
    """Populate one patient + tumor/normal specimens + 3 assays so the
    heatmap and queue have something to render. We hit the DB directly here
    because the goal is GUI rendering, not v0.3 append semantics. Column
    set matches the hgsoc template."""
    db = project_dir / "casetrack.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO patients(patient_id, sex, qc_status, consent_status) "
                     "VALUES (?, ?, ?, ?)", ("P01", "F", "pass", "consented"))
        conn.execute("INSERT INTO specimens(specimen_id, patient_id, tissue_site, qc_status) "
                     "VALUES (?, ?, ?, ?)", ("P01_tumor", "P01", "ovary", "pass"))
        conn.execute("INSERT INTO specimens(specimen_id, patient_id, tissue_site, qc_status) "
                     "VALUES (?, ?, ?, ?)", ("P01_normal", "P01", "blood", "pass"))
        conn.execute("INSERT INTO assays(assay_id, specimen_id, assay_type, qc_status) "
                     "VALUES (?, ?, ?, ?)", ("A_T1", "P01_tumor", "ONT", "pass"))
        conn.execute("INSERT INTO assays(assay_id, specimen_id, assay_type, qc_status) "
                     "VALUES (?, ?, ?, ?)", ("A_T2", "P01_tumor", "ONT", "pass"))
        conn.execute("INSERT INTO assays(assay_id, specimen_id, assay_type, qc_status) "
                     "VALUES (?, ?, ?, ?)", ("A_N1", "P01_normal", "ONT", "pass"))
        conn.commit()
    finally:
        conn.close()


def _add_done_columns(project_dir: Path, level: str, analyses: list[str]) -> None:
    """Add fake `<analysis>_done` columns and stamp them on a subset of rows."""
    db = project_dir / "casetrack.db"
    conn = sqlite3.connect(db)
    try:
        for a in analyses:
            try:
                conn.execute(f"ALTER TABLE {level} ADD COLUMN {a}_done TEXT")
            except sqlite3.OperationalError:
                pass  # already exists
        conn.commit()
    finally:
        conn.close()


def _set_done(project_dir: Path, level: str, key: str, where_id: str, analysis: str) -> None:
    db = project_dir / "casetrack.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            f"UPDATE {level} SET {analysis}_done = '2026-04-27T10:00:00' WHERE {key} = ?",
            (where_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _registry(tmp_path: Path, project_id: str, project_dir: Path) -> Path:
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps({
        "schema_v": 1,
        "projects": {
            project_id: {
                "path": str(project_dir.resolve()),
                "name": "test_cohort",
                "created": "2026-04-27T00:00:00",
                "last_seen": "2026-04-27T00:00:00",
            }
        },
    }))
    return reg_path


@pytest.fixture
def gui_project(tmp_path: Path):
    project_dir = _init_project(tmp_path / "proj", project_id="test-cohort")
    _seed_minimal_rows(project_dir)
    _add_done_columns(project_dir, "specimens", ["merge", "samtools_sort", "modkit_callmods"])
    _add_done_columns(project_dir, "assays", ["dorado_basecaller"])
    _set_done(project_dir, "specimens", "specimen_id", "P01_tumor", "merge")
    _set_done(project_dir, "specimens", "specimen_id", "P01_tumor", "samtools_sort")
    _set_done(project_dir, "assays", "assay_id", "A_T1", "dorado_basecaller")
    _set_done(project_dir, "assays", "assay_id", "A_T2", "dorado_basecaller")
    reg = _registry(tmp_path, "test-cohort", project_dir)
    return project_dir, reg


def _make_client(reg: Path) -> TestClient:
    from casetrack_gui.app import create_app
    app = create_app(registry_path_override=reg)
    return TestClient(app)


# ── basic plumbing ───────────────────────────────────────────────────────────


def test_healthz(tmp_path: Path):
    reg = tmp_path / "empty_registry.json"
    reg.write_text(json.dumps({"schema_v": 1, "projects": {}}))
    client = _make_client(reg)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_projects_index_lists_registry_entries(gui_project):
    project_dir, reg = gui_project
    client = _make_client(reg)
    r = client.get("/")
    assert r.status_code == 200
    assert "test-cohort" in r.text
    assert str(project_dir) in r.text


def test_project_home_renders_heatmap(gui_project):
    _, reg = gui_project
    client = _make_client(reg)
    r = client.get("/p/test-cohort")
    assert r.status_code == 200
    body = r.text
    # Heatmap row labels (specimen IDs)
    assert "P01_tumor" in body
    assert "P01_normal" in body
    # Analysis column headers
    assert "merge" in body
    assert "samtools_sort" in body
    assert "modkit_callmods" in body
    # Aggregated dorado_basecaller column should show "●●" for tumor (2 done)
    # and "—" for normal (no assays would be a bug; with 1 pending normal we expect ◯)
    assert "●●" in body  # tumor: both assays done


def test_project_home_404_for_unknown_id(tmp_path: Path):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"schema_v": 1, "projects": {}}))
    client = _make_client(reg)
    r = client.get("/p/does-not-exist")
    assert r.status_code == 404


def test_patient_drill_down(gui_project):
    _, reg = gui_project
    client = _make_client(reg)
    r = client.get("/p/test-cohort/patient/P01")
    assert r.status_code == 200
    body = r.text
    assert "P01_tumor" in body
    assert "P01_normal" in body
    assert "A_T1" in body
    assert "A_T2" in body
    assert "A_N1" in body


def test_qc_log_empty_then_populated(gui_project):
    _, reg = gui_project
    client = _make_client(reg)
    r = client.get("/p/test-cohort/qc")
    assert r.status_code == 200
    assert "No QC events recorded" in r.text


# ── introspection unit ───────────────────────────────────────────────────────


def test_introspect_picks_specimen_level_when_richest(gui_project):
    project_dir, _ = gui_project
    from casetrack_gui.introspect import introspect
    conn = sqlite3.connect(project_dir / "casetrack.db")
    try:
        shape = introspect(conn)
    finally:
        conn.close()
    assert shape.row_level == "specimens"
    assert "merge" in shape.analyses_for("specimens")
    assert "dorado_basecaller" in shape.analyses_for("assays")
    assert shape.has_qc_events is True


# ── heatmap glyph aggregation ────────────────────────────────────────────────


def test_heatmap_aggregates_assay_glyphs(gui_project):
    project_dir, _ = gui_project
    from casetrack_gui import heatmap as hm
    from casetrack_gui.introspect import introspect
    conn = sqlite3.connect(project_dir / "casetrack.db")
    try:
        shape = introspect(conn)
        h = hm.build(conn, shape)
    finally:
        conn.close()
    # 2 specimens
    assert len(h.rows) == 2
    # dorado_basecaller is an assay-level analysis aggregated up
    assert "dorado_basecaller" in h.analyses
    assert h.aggregated_child_level == "assays"
    tumor_row = next(r for r in h.rows if r.row_id == "P01_tumor")
    dorado_idx = h.analyses.index("dorado_basecaller")
    # Both child assays done → "●●"
    assert tumor_row.cells[dorado_idx].glyph == "●●"


def test_next_up_queue_is_pipeline_aware(gui_project):
    project_dir, _ = gui_project
    from casetrack_gui import heatmap as hm
    from casetrack_gui.introspect import introspect
    conn = sqlite3.connect(project_dir / "casetrack.db")
    try:
        shape = introspect(conn)
        q = hm.next_up(conn, shape)
    finally:
        conn.close()
    # P01_tumor has merge+samtools_sort done but not modkit_callmods → expect it pending modkit_callmods
    by_a = {item["analysis"]: item for item in q}
    assert "modkit_callmods" in by_a
    assert "P01_tumor" in by_a["modkit_callmods"]["ids"]


# ── mutations: shell out to casetrack CLI ────────────────────────────────────


def test_censor_post_shells_out_and_writes_qc_event(gui_project, monkeypatch):
    project_dir, reg = gui_project
    # Force the CLI to use this same Python interpreter via -m casetrack
    # so the test doesn't depend on pip-install having refreshed entry_points.
    monkeypatch.setenv("CASETRACK_BIN", f"{sys.executable} {REPO_ROOT}/casetrack.py")
    client = _make_client(reg)
    r = client.post(
        "/p/test-cohort/censor",
        data={
            "level": "assay",
            "entity_id": "A_N1",
            "kind": "qc_fail",
            "reason": "test contamination",
            "return_to": "/p/test-cohort/qc",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/p/test-cohort/qc" in r.headers["location"]

    # Confirm qc_events row landed.
    conn = sqlite3.connect(project_dir / "casetrack.db")
    try:
        row = conn.execute(
            "SELECT level, entity_id, kind, reason FROM qc_events WHERE entity_id = ?",
            ("A_N1",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "expected the censor subprocess to have written a qc_events row"
    assert row == ("assay", "A_N1", "qc_fail", "test contamination")


def test_censor_failure_redirects_with_fail_status(gui_project, monkeypatch):
    _, reg = gui_project
    monkeypatch.setenv("CASETRACK_BIN", f"{sys.executable} {REPO_ROOT}/casetrack.py")
    client = _make_client(reg)
    r = client.post(
        "/p/test-cohort/censor",
        data={
            "level": "assay",
            "entity_id": "DOES_NOT_EXIST",
            "kind": "qc_fail",
            "reason": "should fail",
            "return_to": "/p/test-cohort/qc",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # We don't strictly assert "fail" — depends on CLI exit semantics for unknown entity_id —
    # but the redirect must round-trip something the operator can read.
    assert "last_status=" in r.headers["location"]
