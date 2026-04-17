"""Consent column update + ethics-override regex + invariant check.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import casetrack
from casetrack_qc import consent as consent_mod
from casetrack_qc import events as events_mod
from casetrack_qc.consent import (
    consent_event_invariant_violations,
    ethics_override_reason_ok,
    get_patient_consent,
    set_patient_consent,
)


# ── ethics override regex ─────────────────────────────────────────────────────


def test_ethics_override_rejects_empty_reason():
    assert ethics_override_reason_ok("") is False
    assert ethics_override_reason_ok("   ") is False


def test_ethics_override_accepts_irb_reference():
    assert ethics_override_reason_ok("re-consent signed, IRB ref 2026-042") is True
    assert ethics_override_reason_ok("irb-approved") is True
    assert ethics_override_reason_ok("ethics committee waiver") is True


def test_ethics_override_accepts_reconsent_phrasing():
    assert ethics_override_reason_ok("Patient re-consented after new protocol") is True
    assert ethics_override_reason_ok("reconsent on file") is True


def test_ethics_override_accepts_iso_date():
    assert ethics_override_reason_ok("resolved on 2026-05-01") is True


def test_ethics_override_rejects_vague_reason():
    assert ethics_override_reason_ok("changed my mind") is False
    assert ethics_override_reason_ok("all good now") is False


# ── set_patient_consent ───────────────────────────────────────────────────────


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
            conn.execute("INSERT INTO patients (patient_id) VALUES ('HGSOC002')")
    finally:
        conn.close()
    return proj


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    return _init_project(tmp_path)


def test_set_patient_consent_revoked_requires_withdrawal_date(seeded: Path):
    conn = casetrack.open_project_db(seeded / "casetrack.db")
    try:
        with pytest.raises(ValueError, match="withdrawal_date"):
            with casetrack.begin_immediate(conn):
                set_patient_consent(
                    conn, "HGSOC002",
                    consent_status="revoked",
                )
    finally:
        conn.close()


def test_set_patient_consent_non_revoked_rejects_withdrawal_date(seeded: Path):
    conn = casetrack.open_project_db(seeded / "casetrack.db")
    try:
        with pytest.raises(ValueError, match="must not carry"):
            with casetrack.begin_immediate(conn):
                set_patient_consent(
                    conn, "HGSOC002",
                    consent_status="consented",
                    withdrawal_date="2026-03-15",
                )
    finally:
        conn.close()


def test_set_patient_consent_roundtrip(seeded: Path):
    conn = casetrack.open_project_db(seeded / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            set_patient_consent(
                conn, "HGSOC002",
                consent_status="revoked",
                withdrawal_date="2026-03-15",
            )
        got = get_patient_consent(conn, "HGSOC002")
        assert got == {
            "consent_status": "revoked",
            "consent_date": None,
            "withdrawal_date": "2026-03-15",
        }
    finally:
        conn.close()


def test_set_patient_consent_rejects_unknown_status(seeded: Path):
    conn = casetrack.open_project_db(seeded / "casetrack.db")
    try:
        with pytest.raises(ValueError, match="invalid consent_status"):
            with casetrack.begin_immediate(conn):
                set_patient_consent(
                    conn, "HGSOC002",
                    consent_status="not_a_status",
                )
    finally:
        conn.close()


# ── invariant check ───────────────────────────────────────────────────────────


def test_invariant_detects_missing_consent_revoked_event(seeded: Path):
    """Direct SQL mutation (bypassing the CLI) → drift detected by validator."""
    conn = casetrack.open_project_db(seeded / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute(
                "UPDATE patients SET consent_status='revoked', "
                "withdrawal_date='2026-03-15' WHERE patient_id='HGSOC002'"
            )
        violations = consent_event_invariant_violations(conn)
    finally:
        conn.close()
    kinds = {v["kind"] for v in violations}
    assert "missing_consent_revoked_event" in kinds


def test_invariant_detects_spurious_withdrawal_date(seeded: Path):
    conn = casetrack.open_project_db(seeded / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute(
                "UPDATE patients SET withdrawal_date='2026-03-15' "
                "WHERE patient_id='HGSOC002'"
            )
        violations = consent_event_invariant_violations(conn)
    finally:
        conn.close()
    kinds = {v["kind"] for v in violations}
    assert "spurious_withdrawal_date" in kinds


def test_invariant_happy_path_no_violations(seeded: Path):
    conn = casetrack.open_project_db(seeded / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            # Proper revocation: event + column update in one transaction.
            events_mod.insert_event(
                conn, level="patient", entity_id="HGSOC002",
                kind="consent_revoked", reason="withdrew",
                source="manual", created_by="me",
                transaction_id="txn_x",
            )
            set_patient_consent(
                conn, "HGSOC002",
                consent_status="revoked",
                withdrawal_date="2026-03-15",
            )
        violations = consent_event_invariant_violations(conn)
    finally:
        conn.close()
    assert violations == []
