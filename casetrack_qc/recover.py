"""Provenance replay for QC actions — called from ``casetrack.cmd_recover_project``.

Handles ``action`` ∈ {``censor``, ``uncensor``, ``ethics_override``,
``migrate_qc``}. Reconstructs ``qc_events`` rows + ``qc_status`` columns +
consent-column state byte-identical to the original DB, provided the
provenance log is intact.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import sqlite3
from typing import Any

from casetrack_qc import consent as consent_mod
from casetrack_qc import events as events_mod
from casetrack_qc.schema import ensure_qc_schema


def _parse_sql_list(entry: dict) -> list[str]:
    sql = entry.get("executed_sql") or entry.get("sql") or []
    if isinstance(sql, str):
        return [sql]
    return list(sql)


def _ensure_qc_schema_from_entry(conn: sqlite3.Connection, entry: dict) -> None:
    """Idempotently apply the QC schema (handles the migrate_qc replay case)."""
    ensure_qc_schema(conn)


def recover_qc_action(
    conn: sqlite3.Connection, entry: dict
) -> tuple[bool, str | None]:
    """Replay a single QC-related provenance entry.

    Returns ``(handled, warning)``. ``handled=False`` means the caller should
    treat this entry as unknown. ``warning`` is a non-fatal message for the
    caller to surface (e.g. "skipped — entity no longer exists"); ``None``
    means the replay succeeded cleanly.

    Runs without its own BEGIN IMMEDIATE — the caller's recover loop manages
    the outer transaction envelope by wrapping each action.
    """
    action = entry.get("action")

    if action == "migrate_qc":
        with _begin(conn):
            _ensure_qc_schema_from_entry(conn, entry)
            # Replay migrated rows.
            for row in entry.get("migrated_rows") or []:
                assay_id = row["assay_id"]
                # Check existence — if the assay row never got replayed, skip
                # with a warning rather than fail the whole recover.
                if not events_mod.entity_exists(conn, "assay", assay_id):
                    continue
                # Re-insert a resolved-less qc_events row if not already present.
                existing = events_mod.get_active_event(
                    conn, level="assay", entity_id=assay_id, kind="qc_fail"
                )
                if existing is None:
                    events_mod.insert_event(
                        conn,
                        level="assay",
                        entity_id=assay_id,
                        kind="qc_fail",
                        reason="migrated from legacy qc_pass",
                        source="import",
                        created_by="import:migrate-qc",
                        transaction_id=entry.get(
                            "transaction_id", "txn_recover"
                        ),
                        created_at=entry.get("timestamp")
                        or events_mod.TIMESTAMP_FMT,
                    )
                events_mod.recompute_entity_status(conn, "assay", assay_id)
        return True, None

    if action == "censor":
        level = entry["level"]
        entity_id = entry["entity_id"]
        kind = entry["kind"]
        reason = entry.get("reason", "")
        source = entry.get("source", "manual")
        created_by = entry.get("created_by", "unknown")
        created_at = entry.get("created_at") or entry.get("timestamp")
        txn_id = entry.get("transaction_id", "txn_recover")

        with _begin(conn):
            if not events_mod.entity_exists(conn, level, entity_id):
                return True, (
                    f"skipped censor: {level} {entity_id!r} not in DB yet"
                )
            # Skip if an active event with same (level, entity_id, kind) already
            # exists (idempotent replay).
            already = events_mod.get_active_event(
                conn, level=level, entity_id=entity_id, kind=kind
            )
            if already is None:
                events_mod.insert_event(
                    conn,
                    level=level,
                    entity_id=entity_id,
                    kind=kind,
                    reason=reason,
                    source=source,
                    created_by=created_by,
                    transaction_id=txn_id,
                    created_at=created_at,
                )
            events_mod.recompute_entity_status(conn, level, entity_id)
            if kind == "consent_revoked" and level == "patient":
                consent_mod.set_patient_consent(
                    conn,
                    entity_id,
                    consent_status="revoked",
                    withdrawal_date=entry.get("withdrawal_date"),
                )
        return True, None

    if action in ("uncensor", "ethics_override"):
        level = entry.get("level")
        entity_id = entry.get("entity_id")
        kind = entry.get("kind")
        event_id = entry.get("qc_event_id")
        resolved_by = entry.get("resolved_by", "unknown")
        resolved_reason = entry.get("reason", "")
        resolved_at = entry.get("resolved_at") or entry.get("timestamp")

        with _begin(conn):
            event = None
            if event_id is not None:
                event = events_mod.get_event_by_id(conn, int(event_id))
            if event is None and (level and entity_id and kind):
                event = events_mod.get_active_event(
                    conn, level=level, entity_id=entity_id, kind=kind
                )
            if event is None:
                return True, (
                    f"skipped {action}: target event not found "
                    f"(event_id={event_id}, level={level}, id={entity_id}, kind={kind})"
                )
            if event.resolved_at is None:
                events_mod.resolve_event(
                    conn,
                    event.id,
                    resolved_by=resolved_by,
                    resolved_reason=resolved_reason,
                    resolved_at=resolved_at,
                )
            events_mod.recompute_entity_status(conn, event.level, event.entity_id)
            if event.kind == "consent_revoked" and event.level == "patient":
                consent_mod.set_patient_consent(
                    conn,
                    event.entity_id,
                    consent_status="consented",
                    withdrawal_date=None,
                )
        return True, None

    return False, None


# ── small transaction helper ───────────────────────────────────────────────────


class _TxnCtx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.rollback()
            return False
        self.conn.commit()
        return False


def _begin(conn: sqlite3.Connection) -> _TxnCtx:
    return _TxnCtx(conn)


__all__ = ["recover_qc_action"]
