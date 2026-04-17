"""End-to-end test of examples/giab_chr21/run_qc_demo.sh.

Runs the demo script against a tmpdir project and asserts DB state at each
milestone. Tracks GH #14 and exercises every v0.4 QC / censoring / consent
code path introduced by proposal 0002.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import casetrack


REPO_ROOT = Path(casetrack.__file__).parent
DEMO_DIR = REPO_ROOT / "examples" / "giab_chr21"
QC_DEMO_SCRIPT = DEMO_DIR / "run_qc_demo.sh"


@pytest.fixture(scope="module")
def qc_demo_project(tmp_path_factory):
    """Run the full run_qc_demo.sh once and share the resulting project dir."""
    if shutil.which("casetrack") is None:
        pytest.skip("casetrack CLI not on PATH — skipping live demo test")
    if shutil.which("sqlite3") is None:
        pytest.skip("sqlite3 CLI not on PATH — skipping live demo test")

    proj = tmp_path_factory.mktemp("giab_qc_demo")
    # Ensure the subprocess finds the same `python3` + `casetrack` that the
    # test runner was started with — pytest may be invoked from a specific
    # conda env whose bin dir isn't first on PATH.
    env = dict(os.environ)
    env["PATH"] = f"{os.path.dirname(sys.executable)}{os.pathsep}{env.get('PATH', '')}"
    res = subprocess.run(
        ["bash", str(QC_DEMO_SCRIPT), str(proj)],
        capture_output=True, text=True, env=env,
    )
    if res.returncode != 0:
        pytest.fail(
            f"run_qc_demo.sh exited {res.returncode}\n"
            f"STDOUT:\n{res.stdout[-4000:]}\n"
            f"STDERR:\n{res.stderr[-2000:]}"
        )
    return proj, res.stdout


@pytest.fixture(scope="module")
def qc_db(qc_demo_project):
    proj, _stdout = qc_demo_project
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    yield conn
    conn.close()


# ── Post-demo DB state ────────────────────────────────────────────────────────


def test_demo_runs_to_completion(qc_demo_project):
    _proj, stdout = qc_demo_project
    assert "DONE — all v0.4 QC features exercised" in stdout


def test_project_has_expected_entities(qc_db):
    """2 patients, 3 specimens (incl. matched-normal added in step 6), 5 assays."""
    assert qc_db.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 2
    assert qc_db.execute("SELECT COUNT(*) FROM specimens").fetchone()[0] == 3
    assert qc_db.execute("SELECT COUNT(*) FROM assays").fetchone()[0] == 5


def test_autoflag_produced_slurm_event(qc_db):
    """Step 1 — one qc_events row with source='slurm' for HG006_PAY77227."""
    rows = qc_db.execute(
        "SELECT level, entity_id, kind, source "
        "FROM qc_events WHERE source='slurm'"
    ).fetchall()
    assert len(rows) == 1
    level, entity_id, kind, source = rows[0]
    assert level == "assay"
    assert entity_id == "HG006_PAY77227"
    assert kind == "qc_fail"


def test_manual_contamination_event_was_resolved(qc_db):
    """Step 2 censored, step 5 uncensored — row should be present with
    resolved_at set."""
    rows = qc_db.execute(
        "SELECT resolved_at, resolved_reason "
        "FROM qc_events "
        "WHERE entity_id='HG006_PBA16846' AND kind='contamination'"
    ).fetchall()
    assert len(rows) == 1
    resolved_at, resolved_reason = rows[0]
    assert resolved_at is not None and resolved_at != ""
    assert "within spec" in resolved_reason.lower()


def test_consent_revocation_was_ethics_overridden(qc_db):
    """Step 7–8: HG002 was consent_revoked, then reversed via ethics override."""
    rows = qc_db.execute(
        "SELECT resolved_at, resolved_reason "
        "FROM qc_events "
        "WHERE entity_id='HG002' AND kind='consent_revoked'"
    ).fetchall()
    assert len(rows) == 1
    resolved_at, resolved_reason = rows[0]
    assert resolved_at is not None
    # Ethics override requires a reason mentioning IRB / re-consent / ISO date.
    assert "irb" in resolved_reason.lower() or "re-consent" in resolved_reason.lower()


def test_consent_status_reverted_to_consented(qc_db):
    """After ethics override, HG002's consent_status should NOT be 'revoked'."""
    (status,) = qc_db.execute(
        "SELECT consent_status FROM patients WHERE patient_id='HG002'"
    ).fetchone()
    assert status != "revoked"


def test_active_qc_warn_remains_on_HG002_PAW70337(qc_db):
    """Step 9 added a qc_warn at the end of the demo; it should stay active."""
    rows = qc_db.execute(
        "SELECT kind, resolved_at FROM qc_events "
        "WHERE entity_id='HG002_PAW70337' AND kind='qc_warn'"
    ).fetchall()
    assert len(rows) == 1
    kind, resolved_at = rows[0]
    assert kind == "qc_warn"
    assert resolved_at is None


def test_matched_normal_specimen_registered(qc_db):
    """Step 6 registered HG002_gDNA_normal + one WGS assay on it."""
    specs = {r[0] for r in qc_db.execute(
        "SELECT specimen_id FROM specimens WHERE patient_id='HG002'"
    ).fetchall()}
    assert "HG002_gDNA_normal" in specs
    (n_normal_assays,) = qc_db.execute(
        "SELECT COUNT(*) FROM assays WHERE specimen_id='HG002_gDNA_normal'"
    ).fetchone()
    assert n_normal_assays == 1


def test_tissue_site_partitioned_cleanly(qc_db):
    """tissue_tag append should have populated tissue_site across all specimens."""
    rows = qc_db.execute(
        "SELECT specimen_id, tissue_site FROM specimens ORDER BY specimen_id"
    ).fetchall()
    sites = {sid: ts for sid, ts in rows}
    assert sites["HG002_gDNA_normal"] == "normal"
    # The other two specimens were tagged tumor by the demo script.
    assert sites["HG002_gDNA"] == "tumor"
    assert sites["HG006_gDNA"] == "tumor"


# ── CLI stdout spot-checks ────────────────────────────────────────────────────


def test_demo_stdout_exercises_every_step(qc_demo_project):
    """The section headers from sep() anchor every step. If a section is
    missing the demo didn't reach it."""
    _proj, stdout = qc_demo_project
    for marker in (
        "1. SLURM autoflag",
        "2. manual censor HG006_PBA16846",
        "3. qc-history for HG006_PBA16846",
        "4. status --usable",
        "5. uncensor HG006_PBA16846",
        "6. cohort readiness",
        "7. consent revocation",
        "8a. uncensor WITHOUT --ethics-override",
        "8b. uncensor WITH --ethics-override",
        "9. _active view row counts",
        "10. dashboard",
        "11. validate",
        "12. recover round-trip",
    ):
        assert marker in stdout, f"demo missing step: {marker!r}"


def test_ethics_gate_refused_without_override(qc_demo_project):
    _proj, stdout = qc_demo_project
    assert "correctly refused with exit 2" in stdout


def test_recover_round_trip_shows_byte_equivalence(qc_demo_project):
    _proj, stdout = qc_demo_project
    assert "qc_events content matches byte-equivalently" in stdout


def test_dashboard_has_qc_sections(qc_demo_project):
    proj, _stdout = qc_demo_project
    html = (proj / "dashboard_qc.html").read_text()
    # Dashboard QC feature surface (proposal 0002 §11):
    assert "qc-chips" in html
    assert "Excluded (active QC events)" in html
    assert "HG006_PAY77227" in html  # slurm-flagged assay shows up
