"""Core insert/query/resolve operations on the ``qc_events`` table.

Proposal 0002 §4.1, §4.4. These functions take a live ``sqlite3.Connection``
and assume the caller manages transactions (typically via
``casetrack.begin_immediate``). Nothing here writes to the provenance log —
provenance is written by the command-layer callers so both rows land inside
the same transaction.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import datetime
import sqlite3
from dataclasses import asdict, dataclass
from typing import Iterable

from casetrack_qc.schema import (
    DEFAULT_QC_KINDS,
    PATIENT_QC_STATUSES,
    CHILD_QC_STATUSES,
    QC_EVENT_SOURCES,
)


TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S"

LEVEL_TABLES = {"patient": "patients", "specimen": "specimens", "assay": "assays"}
LEVEL_KEYS = {"patient": "patient_id", "specimen": "specimen_id", "assay": "assay_id"}


# ── Dataclass ───────────────────────────────────────────────────────────────────


@dataclass
class QcEvent:
    id: int
    level: str
    entity_id: str
    kind: str
    reason: str
    source: str
    created_at: str
    created_by: str
    resolved_at: str | None
    resolved_by: str | None
    resolved_reason: str | None
    transaction_id: str

    def to_dict(self) -> dict:
        return asdict(self)


_EVENT_COLS = (
    "id, level, entity_id, kind, reason, source, created_at, created_by, "
    "resolved_at, resolved_by, resolved_reason, transaction_id"
)


def _row_to_event(row: tuple) -> QcEvent:
    return QcEvent(*row)


# ── Validation helpers ─────────────────────────────────────────────────────────


class QcEventError(Exception):
    """Raised when a qc_events operation violates a CLI-enforced invariant."""


def validate_kind_for_level(
    kind: str,
    level: str,
    kinds: Iterable[str],
    kind_scopes: dict[str, Iterable[str]],
) -> None:
    """Raise :class:`QcEventError` unless ``kind`` is allowed at ``level``.

    Enforces §5.3 ``[qc.kind_scopes]`` — kinds listed with a scope must land at
    that level; kinds absent from the map are allowed anywhere. Also checks
    that *kind* is one of the declared kinds.
    """
    kinds_tuple = tuple(kinds)
    if kind not in kinds_tuple:
        raise QcEventError(
            f"unknown qc kind {kind!r}; known: {sorted(kinds_tuple)}"
        )
    scope = kind_scopes.get(kind)
    if scope is not None and level not in tuple(scope):
        raise QcEventError(
            f"kind {kind!r} is not allowed at level {level!r} "
            f"(allowed: {list(scope)})"
        )
    if level not in LEVEL_TABLES:
        raise QcEventError(f"unknown level {level!r}")


def entity_exists(conn: sqlite3.Connection, level: str, entity_id: str) -> bool:
    """True when a row with primary key ``entity_id`` exists at ``level``."""
    if level not in LEVEL_TABLES:
        raise QcEventError(f"unknown level {level!r}")
    table = LEVEL_TABLES[level]
    key = LEVEL_KEYS[level]
    row = conn.execute(
        f'SELECT 1 FROM "{table}" WHERE "{key}" = ?', (entity_id,)
    ).fetchone()
    return row is not None


# ── CRUD ────────────────────────────────────────────────────────────────────────


def insert_event(
    conn: sqlite3.Connection,
    *,
    level: str,
    entity_id: str,
    kind: str,
    reason: str,
    source: str,
    created_by: str,
    transaction_id: str,
    created_at: str | None = None,
) -> int:
    """Insert a new active event row. Returns the new row's ``id``.

    Refuses to create a second active event with the same ``(level, entity_id,
    kind)`` triple — caller should either resolve the prior one first or skip.
    """
    if source not in QC_EVENT_SOURCES:
        raise QcEventError(
            f"invalid source {source!r}; must be one of {list(QC_EVENT_SOURCES)}"
        )
    active = get_active_event(conn, level=level, entity_id=entity_id, kind=kind)
    if active is not None:
        raise QcEventError(
            f"already active: {level} {entity_id!r} has kind={kind!r} event "
            f"id={active.id} (resolve it first with `casetrack uncensor`)"
        )
    if created_at is None:
        created_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    cur = conn.execute(
        """
        INSERT INTO qc_events
            (level, entity_id, kind, reason, source,
             created_at, created_by, transaction_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (level, entity_id, kind, reason, source,
         created_at, created_by, transaction_id),
    )
    return cur.lastrowid


def insert_event_resolved(
    conn: sqlite3.Connection,
    *,
    level: str,
    entity_id: str,
    kind: str,
    reason: str,
    source: str,
    created_by: str,
    transaction_id: str,
    created_at: str,
    resolved_at: str,
    resolved_by: str,
    resolved_reason: str,
    forced_id: int | None = None,
) -> int:
    """Insert an already-resolved event (used only by recover / migrate).

    Skips the active-uniqueness check because recover replays provenance
    verbatim, and a resolved event on the same (level, entity_id, kind) can
    legitimately predate a later active one.
    """
    cols = (
        "level, entity_id, kind, reason, source, "
        "created_at, created_by, resolved_at, resolved_by, resolved_reason, "
        "transaction_id"
    )
    params = (
        level, entity_id, kind, reason, source,
        created_at, created_by, resolved_at, resolved_by, resolved_reason,
        transaction_id,
    )
    if forced_id is not None:
        cols = "id, " + cols
        params = (forced_id,) + params
    placeholders = ", ".join(["?"] * len(params))
    cur = conn.execute(
        f"INSERT INTO qc_events ({cols}) VALUES ({placeholders})",
        params,
    )
    return cur.lastrowid


def get_event_by_id(conn: sqlite3.Connection, event_id: int) -> QcEvent | None:
    row = conn.execute(
        f"SELECT {_EVENT_COLS} FROM qc_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    return _row_to_event(row) if row else None


def get_active_event(
    conn: sqlite3.Connection,
    *,
    level: str,
    entity_id: str,
    kind: str,
) -> QcEvent | None:
    row = conn.execute(
        f"SELECT {_EVENT_COLS} FROM qc_events "
        "WHERE level=? AND entity_id=? AND kind=? AND resolved_at IS NULL",
        (level, entity_id, kind),
    ).fetchone()
    return _row_to_event(row) if row else None


def list_active_events_for_entity(
    conn: sqlite3.Connection, level: str, entity_id: str
) -> list[QcEvent]:
    rows = conn.execute(
        f"SELECT {_EVENT_COLS} FROM qc_events "
        "WHERE level=? AND entity_id=? AND resolved_at IS NULL "
        "ORDER BY id",
        (level, entity_id),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def list_events_for_entity(
    conn: sqlite3.Connection, level: str, entity_id: str
) -> list[QcEvent]:
    """All events (active or resolved) for ``(level, entity_id)``, oldest first."""
    rows = conn.execute(
        f"SELECT {_EVENT_COLS} FROM qc_events "
        "WHERE level=? AND entity_id=? ORDER BY id",
        (level, entity_id),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def list_all_active(conn: sqlite3.Connection) -> list[QcEvent]:
    rows = conn.execute(
        f"SELECT {_EVENT_COLS} FROM qc_events WHERE resolved_at IS NULL ORDER BY id"
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def resolve_event(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    resolved_by: str,
    resolved_reason: str,
    resolved_at: str | None = None,
) -> QcEvent:
    """Mark an active event as resolved. Raises if the event is unknown or
    already resolved. Returns the updated event."""
    event = get_event_by_id(conn, event_id)
    if event is None:
        raise QcEventError(f"qc_events id={event_id} not found")
    if event.resolved_at is not None:
        raise QcEventError(
            f"qc_events id={event_id} already resolved at {event.resolved_at}"
        )
    if resolved_at is None:
        resolved_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    conn.execute(
        "UPDATE qc_events SET resolved_at=?, resolved_by=?, resolved_reason=? "
        "WHERE id=?",
        (resolved_at, resolved_by, resolved_reason, event_id),
    )
    event.resolved_at = resolved_at
    event.resolved_by = resolved_by
    event.resolved_reason = resolved_reason
    return event


# ── Materialized qc_status updates ─────────────────────────────────────────────


# Ordering for "most severe active kind wins" when deriving qc_status.
_KIND_TO_STATUS = {
    "consent_revoked": "consent_revoked",
    "qc_fail": "fail",
    "qc_warn": "warn",
    "library_prep_failed": "fail",
    "basecall_accuracy_low": "fail",
    "sequencing_run_failed": "fail",
    "contamination": "fail",
    "protocol_deviation": "fail",
    "batch_effect_flagged": "warn",
    "superseded": "censored",
    "other": "censored",
}

_STATUS_SEVERITY = {
    "pass": 0,
    "warn": 1,
    "censored": 2,
    "fail": 3,
    "consent_revoked": 4,
}


def derive_status(active_kinds: Iterable[str], level: str) -> str:
    """Pick the most severe derived status from a set of active kinds."""
    best = "pass"
    for kind in active_kinds:
        cand = _KIND_TO_STATUS.get(kind, "censored")
        if level != "patient" and cand == "consent_revoked":
            cand = "censored"
        if _STATUS_SEVERITY[cand] > _STATUS_SEVERITY[best]:
            best = cand
    # Guard: specimen/assay can't carry consent_revoked per CHECK.
    if level != "patient" and best == "consent_revoked":
        best = "censored"
    return best


def recompute_entity_status(conn: sqlite3.Connection, level: str, entity_id: str) -> str:
    """Recompute + write ``qc_status`` for a single entity from its active events."""
    kinds = [
        row[0]
        for row in conn.execute(
            "SELECT kind FROM qc_events "
            "WHERE level=? AND entity_id=? AND resolved_at IS NULL",
            (level, entity_id),
        ).fetchall()
    ]
    status = derive_status(kinds, level)
    table = LEVEL_TABLES[level]
    key = LEVEL_KEYS[level]
    conn.execute(
        f'UPDATE "{table}" SET qc_status=? WHERE "{key}"=?',
        (status, entity_id),
    )
    return status


__all__ = [
    "QcEvent",
    "QcEventError",
    "derive_status",
    "entity_exists",
    "get_active_event",
    "get_event_by_id",
    "insert_event",
    "insert_event_resolved",
    "list_active_events_for_entity",
    "list_all_active",
    "list_events_for_entity",
    "recompute_entity_status",
    "resolve_event",
    "validate_kind_for_level",
    "LEVEL_TABLES",
    "LEVEL_KEYS",
    "TIMESTAMP_FMT",
]
