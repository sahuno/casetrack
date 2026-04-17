"""Core event CRUD + transaction semantics for casetrack_qc.events.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

import casetrack
from casetrack_qc import events as events_mod
from casetrack_qc.events import (
    QcEventError,
    derive_status,
    entity_exists,
    get_active_event,
    get_event_by_id,
    insert_event,
    list_active_events_for_entity,
    list_events_for_entity,
    recompute_entity_status,
    resolve_event,
    validate_kind_for_level,
)
from casetrack_qc.schema import (
    DEFAULT_QC_KIND_SCOPES,
    DEFAULT_QC_KINDS,
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
    # Populate HGSOC002 + HGSOC006 paired tumor/normal (the §4.5 example).
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES ('HGSOC002'), ('HGSOC006');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "  VALUES ('HGSOC002-tumor',  'HGSOC002', 'tumor'),"
                "         ('HGSOC002-normal', 'HGSOC002', 'normal'),"
                "         ('HGSOC006-tumor',  'HGSOC006', 'tumor'),"
                "         ('HGSOC006-normal', 'HGSOC006', 'normal');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('HGSOC002-normal-ONT-RNA', 'HGSOC002-normal', 'ONT'),"
                "  ('HGSOC002-tumor-ONT-RNA',  'HGSOC002-tumor',  'ONT'),"
                "  ('HGSOC006-normal-ONT-RNA', 'HGSOC006-normal', 'ONT'),"
                "  ('HGSOC006-tumor-ONT-RNA',  'HGSOC006-tumor',  'ONT');"
            )
    finally:
        conn.close()
    return proj


@pytest.fixture
def proj_with_cohort(tmp_path: Path) -> Path:
    return _init_project(tmp_path)


@pytest.fixture
def conn(proj_with_cohort):
    c = casetrack.open_project_db(proj_with_cohort / "casetrack.db")
    try:
        yield c
    finally:
        c.close()


# ── validation ────────────────────────────────────────────────────────────────


def test_validate_kind_for_level_accepts_scoped_kind():
    validate_kind_for_level(
        "library_prep_failed", "assay",
        kinds=DEFAULT_QC_KINDS,
        kind_scopes=DEFAULT_QC_KIND_SCOPES,
    )


def test_validate_kind_for_level_rejects_wrong_level():
    with pytest.raises(QcEventError, match="consent_revoked"):
        validate_kind_for_level(
            "consent_revoked", "assay",
            kinds=DEFAULT_QC_KINDS,
            kind_scopes=DEFAULT_QC_KIND_SCOPES,
        )


def test_validate_kind_for_level_rejects_unknown_kind():
    with pytest.raises(QcEventError, match="unknown qc kind"):
        validate_kind_for_level(
            "not_a_kind", "assay",
            kinds=DEFAULT_QC_KINDS,
            kind_scopes=DEFAULT_QC_KIND_SCOPES,
        )


def test_entity_exists_roundtrip(conn):
    assert entity_exists(conn, "assay", "HGSOC002-normal-ONT-RNA") is True
    assert entity_exists(conn, "assay", "nonexistent") is False
    assert entity_exists(conn, "patient", "HGSOC002") is True


# ── insert / query / resolve ──────────────────────────────────────────────────


def test_insert_event_then_get_by_id(conn):
    with casetrack.begin_immediate(conn):
        eid = insert_event(
            conn,
            level="assay",
            entity_id="HGSOC002-normal-ONT-RNA",
            kind="qc_fail",
            reason="library prep failed",
            source="manual",
            created_by="samuel",
            transaction_id="txn_1",
        )
    assert eid > 0
    ev = get_event_by_id(conn, eid)
    assert ev is not None
    assert ev.level == "assay"
    assert ev.kind == "qc_fail"
    assert ev.resolved_at is None


def test_insert_event_rejects_duplicate_active(conn):
    with casetrack.begin_immediate(conn):
        insert_event(
            conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
            kind="qc_fail", reason="r", source="manual",
            created_by="me", transaction_id="txn_1",
        )
    with pytest.raises(QcEventError, match="already active"):
        with casetrack.begin_immediate(conn):
            insert_event(
                conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
                kind="qc_fail", reason="r2", source="manual",
                created_by="me", transaction_id="txn_2",
            )


def test_insert_event_allows_resolved_then_new(conn):
    """A resolved event on same (entity, kind) must not block a new active one."""
    with casetrack.begin_immediate(conn):
        eid = insert_event(
            conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
            kind="qc_fail", reason="r", source="manual",
            created_by="me", transaction_id="txn_1",
        )
    with casetrack.begin_immediate(conn):
        resolve_event(conn, eid, resolved_by="me", resolved_reason="re-seq ok")
    # Now a new active event on same entity+kind is allowed.
    with casetrack.begin_immediate(conn):
        eid2 = insert_event(
            conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
            kind="qc_fail", reason="different failure", source="manual",
            created_by="me", transaction_id="txn_2",
        )
    assert eid2 != eid
    assert len(list_events_for_entity(conn, "assay", "HGSOC002-normal-ONT-RNA")) == 2


def test_resolve_event_sets_resolved_fields(conn):
    with casetrack.begin_immediate(conn):
        eid = insert_event(
            conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
            kind="qc_fail", reason="r", source="manual",
            created_by="me", transaction_id="txn_1",
        )
    with casetrack.begin_immediate(conn):
        resolve_event(conn, eid, resolved_by="me", resolved_reason="fixed")
    ev = get_event_by_id(conn, eid)
    assert ev.resolved_at is not None
    assert ev.resolved_by == "me"
    assert ev.resolved_reason == "fixed"


def test_resolve_event_rejects_double_resolve(conn):
    with casetrack.begin_immediate(conn):
        eid = insert_event(
            conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
            kind="qc_fail", reason="r", source="manual",
            created_by="me", transaction_id="txn_1",
        )
    with casetrack.begin_immediate(conn):
        resolve_event(conn, eid, resolved_by="me", resolved_reason="a")
    with pytest.raises(QcEventError, match="already resolved"):
        with casetrack.begin_immediate(conn):
            resolve_event(conn, eid, resolved_by="me", resolved_reason="b")


def test_list_active_events_scopes_to_entity(conn):
    with casetrack.begin_immediate(conn):
        insert_event(
            conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
            kind="qc_fail", reason="r1", source="manual",
            created_by="me", transaction_id="txn_1",
        )
        insert_event(
            conn, level="assay", entity_id="HGSOC002-tumor-ONT-RNA",
            kind="qc_warn", reason="r2", source="manual",
            created_by="me", transaction_id="txn_1",
        )
    xs = list_active_events_for_entity(conn, "assay", "HGSOC002-normal-ONT-RNA")
    assert len(xs) == 1
    assert xs[0].kind == "qc_fail"


def test_transaction_rollback_on_error(conn):
    """Exception inside begin_immediate must roll back all QC inserts."""
    with pytest.raises(RuntimeError):
        with casetrack.begin_immediate(conn):
            insert_event(
                conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
                kind="qc_fail", reason="r", source="manual",
                created_by="me", transaction_id="txn_1",
            )
            raise RuntimeError("boom")
    (count,) = conn.execute("SELECT COUNT(*) FROM qc_events").fetchone()
    assert count == 0


def test_insert_event_rejects_bad_source(conn):
    with pytest.raises(QcEventError, match="invalid source"):
        with casetrack.begin_immediate(conn):
            insert_event(
                conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
                kind="qc_fail", reason="r", source="badsrc",
                created_by="me", transaction_id="txn_1",
            )


# ── derive_status (the materialization rule) ──────────────────────────────────


def test_derive_status_empty_is_pass():
    assert derive_status([], "assay") == "pass"


def test_derive_status_fail_beats_warn():
    assert derive_status(["qc_warn", "qc_fail"], "assay") == "fail"


def test_derive_status_consent_revoked_only_on_patient():
    assert derive_status(["consent_revoked"], "patient") == "consent_revoked"
    assert derive_status(["consent_revoked"], "specimen") == "censored"


def test_recompute_entity_status_updates_column(conn):
    with casetrack.begin_immediate(conn):
        insert_event(
            conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
            kind="qc_fail", reason="r", source="manual",
            created_by="me", transaction_id="txn_1",
        )
        recompute_entity_status(conn, "assay", "HGSOC002-normal-ONT-RNA")
    (status,) = conn.execute(
        "SELECT qc_status FROM assays WHERE assay_id='HGSOC002-normal-ONT-RNA'"
    ).fetchone()
    assert status == "fail"


def test_recompute_after_resolution_returns_to_pass(conn):
    with casetrack.begin_immediate(conn):
        eid = insert_event(
            conn, level="assay", entity_id="HGSOC002-normal-ONT-RNA",
            kind="qc_fail", reason="r", source="manual",
            created_by="me", transaction_id="txn_1",
        )
        recompute_entity_status(conn, "assay", "HGSOC002-normal-ONT-RNA")
    with casetrack.begin_immediate(conn):
        resolve_event(conn, eid, resolved_by="me", resolved_reason="fixed")
        recompute_entity_status(conn, "assay", "HGSOC002-normal-ONT-RNA")
    (status,) = conn.execute(
        "SELECT qc_status FROM assays WHERE assay_id='HGSOC002-normal-ONT-RNA'"
    ).fetchone()
    assert status == "pass"
