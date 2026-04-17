"""End-to-end CLI tests for casetrack censor / uncensor / qc-history.

Uses the HGSOC002 failed-normal-ONT-RNA case from proposal §4.5 as the
worked example.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import casetrack


CASETRACK_BIN = [sys.executable, str(Path(__file__).resolve().parent.parent / "casetrack.py")]


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        CASETRACK_BIN + args,
        check=check,
        capture_output=True,
        text=True,
    )


def _seed_cohort(proj: Path) -> None:
    ns = argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="hgsoc_test", force=False,
    )
    casetrack.cmd_init(ns)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES "
                "  ('HGSOC002'), ('HGSOC006');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES "
                "  ('HGSOC002-tumor',  'HGSOC002', 'tumor'),"
                "  ('HGSOC002-normal', 'HGSOC002', 'normal'),"
                "  ('HGSOC006-tumor',  'HGSOC006', 'tumor'),"
                "  ('HGSOC006-normal', 'HGSOC006', 'normal');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('HGSOC002-normal-ONT-RNA', 'HGSOC002-normal', 'ONT'),"
                "  ('HGSOC002-tumor-ONT-RNA',  'HGSOC002-tumor',  'ONT'),"
                "  ('HGSOC006-normal-ONT-RNA', 'HGSOC006-normal', 'ONT'),"
                "  ('HGSOC006-tumor-ONT-RNA',  'HGSOC006-tumor',  'ONT');"
            )
    finally:
        conn.close()


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    _seed_cohort(proj)
    return proj


# ── censor: single event ──────────────────────────────────────────────────────


def test_censor_assay_sets_qc_status(seeded: Path):
    res = _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "library_prep_failed",
        "--reason", "cDNA yield 8 ng, need >100",
    ])
    assert "Censored assay" in res.stdout

    conn = sqlite3.connect(str(seeded / "casetrack.db"))
    try:
        (status,) = conn.execute(
            "SELECT qc_status FROM assays WHERE assay_id='HGSOC002-normal-ONT-RNA'"
        ).fetchone()
        assert status == "fail"
        # sibling tumor assay stays pass
        (sib_status,) = conn.execute(
            "SELECT qc_status FROM assays WHERE assay_id='HGSOC002-tumor-ONT-RNA'"
        ).fetchone()
        assert sib_status == "pass"
        rows = conn.execute(
            "SELECT level, entity_id, kind, source, resolved_at FROM qc_events"
        ).fetchall()
        assert len(rows) == 1
        level, eid, kind, source, resolved = rows[0]
        assert (level, eid, kind, source) == (
            "assay", "HGSOC002-normal-ONT-RNA", "library_prep_failed", "manual"
        )
        assert resolved is None
    finally:
        conn.close()


def test_censor_logs_provenance(seeded: Path):
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    lines = (seeded / "provenance.jsonl").read_text().splitlines()
    actions = [json.loads(l)["action"] for l in lines]
    assert "censor" in actions
    censor_entry = next(json.loads(l) for l in lines if json.loads(l)["action"] == "censor")
    assert censor_entry["kind"] == "qc_fail"
    assert censor_entry["entity_id"] == "HGSOC002-normal-ONT-RNA"
    assert censor_entry["new_qc_status"] == "fail"


def test_censor_rejects_unknown_kind(seeded: Path):
    res = _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "not_a_kind", "--reason", "r",
    ], check=False)
    assert res.returncode == 2
    assert "unknown qc kind" in res.stderr


def test_censor_rejects_wrong_level_for_kind(seeded: Path):
    """consent_revoked at assay-level must be rejected (§5.3 kind_scopes)."""
    res = _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "consent_revoked", "--reason", "r",
    ], check=False)
    assert res.returncode == 2
    assert "not allowed at level" in res.stderr


def test_censor_rejects_unknown_entity(seeded: Path):
    res = _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "NONEXISTENT",
        "--kind", "qc_fail", "--reason", "r",
    ], check=False)
    assert res.returncode == 2
    assert "not found" in res.stderr


def test_censor_rejects_duplicate_active(seeded: Path):
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    res = _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r2",
    ], check=False)
    assert res.returncode == 2
    assert "already active" in res.stderr


def test_censor_patient_consent_revoked_sets_columns(seeded: Path):
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "patient", "--id", "HGSOC006",
        "--kind", "consent_revoked",
        "--reason", "withdrew",
        "--withdrawal-date", "2026-03-15",
    ])
    conn = sqlite3.connect(str(seeded / "casetrack.db"))
    try:
        status, cdate, wdate = conn.execute(
            "SELECT consent_status, consent_date, withdrawal_date "
            "FROM patients WHERE patient_id='HGSOC006'"
        ).fetchone()
        assert status == "revoked"
        assert wdate == "2026-03-15"
        (qc_status,) = conn.execute(
            "SELECT qc_status FROM patients WHERE patient_id='HGSOC006'"
        ).fetchone()
        assert qc_status == "consent_revoked"
    finally:
        conn.close()


# ── censor --from file ────────────────────────────────────────────────────────


def test_censor_bulk_from_tsv(seeded: Path, tmp_path: Path):
    src = tmp_path / "bulk.tsv"
    pd.DataFrame([
        {"level": "assay", "entity_id": "HGSOC002-normal-ONT-RNA",
         "kind": "library_prep_failed", "reason": "r1"},
        {"level": "assay", "entity_id": "HGSOC002-tumor-ONT-RNA",
         "kind": "qc_warn", "reason": "r2"},
    ]).to_csv(src, sep="\t", index=False)

    res = _run([
        "censor", "--project-dir", str(seeded),
        "--from", str(src),
    ])
    assert "Bulk-censored 2" in res.stdout

    conn = sqlite3.connect(str(seeded / "casetrack.db"))
    try:
        (cnt,) = conn.execute("SELECT COUNT(*) FROM qc_events").fetchone()
        assert cnt == 2
        # Both events share the same transaction_id (§12 Q7).
        (distinct_txns,) = conn.execute(
            "SELECT COUNT(DISTINCT transaction_id) FROM qc_events"
        ).fetchone()
        assert distinct_txns == 1
    finally:
        conn.close()


def test_censor_bulk_missing_columns_fails(seeded: Path, tmp_path: Path):
    src = tmp_path / "bad.tsv"
    src.write_text("level\tentity_id\nassay\tHGSOC002-normal-ONT-RNA\n")
    res = _run([
        "censor", "--project-dir", str(seeded),
        "--from", str(src),
    ], check=False)
    assert res.returncode == 1
    assert "missing required columns" in res.stderr


# ── uncensor ──────────────────────────────────────────────────────────────────


def test_uncensor_by_event_id_resolves(seeded: Path):
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    conn = sqlite3.connect(str(seeded / "casetrack.db"))
    try:
        (eid,) = conn.execute("SELECT id FROM qc_events").fetchone()
    finally:
        conn.close()

    _run([
        "uncensor", "--project-dir", str(seeded),
        "--event-id", str(eid), "--reason", "re-sequenced 2026-04-10, passes",
    ])
    conn = sqlite3.connect(str(seeded / "casetrack.db"))
    try:
        resolved_at, resolved_by = conn.execute(
            "SELECT resolved_at, resolved_by FROM qc_events WHERE id=?", (eid,)
        ).fetchone()
        (status,) = conn.execute(
            "SELECT qc_status FROM assays WHERE assay_id='HGSOC002-normal-ONT-RNA'"
        ).fetchone()
        assert resolved_at is not None
        assert status == "pass"
    finally:
        conn.close()


def test_uncensor_by_level_id_sugar(seeded: Path):
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    _run([
        "uncensor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--reason", "re-seq passed 2026-04-11",
    ])
    conn = sqlite3.connect(str(seeded / "casetrack.db"))
    try:
        (resolved_at,) = conn.execute(
            "SELECT resolved_at FROM qc_events "
            "WHERE entity_id='HGSOC002-normal-ONT-RNA'"
        ).fetchone()
        assert resolved_at is not None
    finally:
        conn.close()


def test_uncensor_requires_reason(seeded: Path):
    res = _run([
        "uncensor", "--project-dir", str(seeded),
        "--event-id", "1", "--reason", "",
    ], check=False)
    assert res.returncode == 1


def test_uncensor_of_consent_requires_ethics_gate(seeded: Path):
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "patient", "--id", "HGSOC006",
        "--kind", "consent_revoked", "--reason", "withdrew",
        "--withdrawal-date", "2026-03-15",
    ])
    # Without --ethics-override --yes → exit 2.
    res = _run([
        "uncensor", "--project-dir", str(seeded),
        "--level", "patient", "--id", "HGSOC006",
        "--reason", "re-consent signed 2026-05-01",
    ], check=False)
    assert res.returncode == 2
    assert "ethics-override" in res.stderr


def test_uncensor_of_consent_requires_irb_or_date_in_reason(seeded: Path):
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "patient", "--id", "HGSOC006",
        "--kind", "consent_revoked", "--reason", "withdrew",
        "--withdrawal-date", "2026-03-15",
    ])
    res = _run([
        "uncensor", "--project-dir", str(seeded),
        "--level", "patient", "--id", "HGSOC006",
        "--ethics-override", "--yes",
        "--reason", "changed my mind",
    ], check=False)
    assert res.returncode == 2


def test_uncensor_consent_accepts_irb_reason(seeded: Path):
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "patient", "--id", "HGSOC006",
        "--kind", "consent_revoked", "--reason", "withdrew",
        "--withdrawal-date", "2026-03-15",
    ])
    _run([
        "uncensor", "--project-dir", str(seeded),
        "--level", "patient", "--id", "HGSOC006",
        "--ethics-override", "--yes",
        "--reason", "re-consent signed, IRB ref 2026-042",
    ])
    conn = sqlite3.connect(str(seeded / "casetrack.db"))
    try:
        (status,) = conn.execute(
            "SELECT consent_status FROM patients WHERE patient_id='HGSOC006'"
        ).fetchone()
        assert status == "consented"
        (qc_status,) = conn.execute(
            "SELECT qc_status FROM patients WHERE patient_id='HGSOC006'"
        ).fetchone()
        assert qc_status == "pass"
    finally:
        conn.close()
    # Provenance gets an ethics_override action.
    actions = {
        json.loads(l)["action"]
        for l in (seeded / "provenance.jsonl").read_text().splitlines()
    }
    assert "ethics_override" in actions


# ── qc-history ────────────────────────────────────────────────────────────────


def test_qc_history_lists_events_for_entity(seeded: Path):
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    res = _run([
        "qc-history", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--fmt", "json",
    ])
    data = json.loads(res.stdout)
    assert len(data) == 1
    assert data[0]["kind"] == "qc_fail"
    assert data[0]["entity_id"] == "HGSOC002-normal-ONT-RNA"


def test_qc_history_without_id_lists_all_active(seeded: Path):
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-normal-ONT-RNA",
        "--kind", "qc_fail", "--reason", "r",
    ])
    _run([
        "censor", "--project-dir", str(seeded),
        "--level", "assay", "--id", "HGSOC002-tumor-ONT-RNA",
        "--kind", "qc_warn", "--reason", "r",
    ])
    res = _run([
        "qc-history", "--project-dir", str(seeded),
        "--fmt", "json",
    ])
    data = json.loads(res.stdout)
    assert len(data) == 2
