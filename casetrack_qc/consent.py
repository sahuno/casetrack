"""Consent-specific rules: column updates, ethics-override regex, invariant check.

Proposal 0002 §4.3, §7. Consent is a distinct concept from QC: patient-level
only, cascades at read, requires ``--ethics-override --yes`` to reverse, and
lives in its own dashboard/export flag.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import re
import sqlite3
from typing import Iterable

from casetrack_qc.schema import DEFAULT_CONSENT_ENUM

# ── Ethics override check ──────────────────────────────────────────────────────

# Per §7.2 #4: uncensor of a consent_revoked event requires --ethics-override
# --yes AND a reason mentioning an IRB reference OR a re-consent date.
# Case-insensitive match on any of the following signals:
#  - the literal "IRB" (plus common variants)
#  - an explicit "re-consent" or "reconsent"
#  - an ISO date (YYYY-MM-DD)
_IRB_RE = re.compile(r"\bIRB\b|\bERB\b|\bethics\s*committee\b", re.IGNORECASE)
_RECONSENT_RE = re.compile(r"\bre[-\s]?consent", re.IGNORECASE)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def ethics_override_reason_ok(reason: str) -> bool:
    """Heuristic gate on ``--reason`` for consent reversal.

    Matches §7.2 #4: the reason must mention an IRB reference OR a re-consent
    phrasing OR contain an explicit ISO date so a later auditor can grep for
    it. Deliberately permissive — stricter review belongs in human sign-off,
    not the CLI.
    """
    if not reason or not reason.strip():
        return False
    return bool(
        _IRB_RE.search(reason)
        or _RECONSENT_RE.search(reason)
        or _DATE_RE.search(reason)
    )


# ── Patient consent column updates ─────────────────────────────────────────────


REVOKED_STATUSES: tuple[str, ...] = ("revoked", "withdrawn", "consent_expired")


def set_patient_consent(
    conn: sqlite3.Connection,
    patient_id: str,
    *,
    consent_status: str,
    withdrawal_date: str | None = None,
    consent_date: str | None = None,
) -> None:
    """Update consent columns on a single ``patients`` row inside the caller's
    transaction. Enforces the §4.3 invariant at the column level — the
    event-side invariant is the caller's problem (they need to write the event
    in the same transaction)."""
    if consent_status not in DEFAULT_CONSENT_ENUM:
        raise ValueError(
            f"invalid consent_status {consent_status!r}; "
            f"must be one of {list(DEFAULT_CONSENT_ENUM)}"
        )
    if consent_status in REVOKED_STATUSES and withdrawal_date is None:
        raise ValueError(
            f"consent_status={consent_status!r} requires withdrawal_date "
            f"(§4.3 invariant)"
        )
    if consent_status not in REVOKED_STATUSES and withdrawal_date is not None:
        raise ValueError(
            f"consent_status={consent_status!r} must not carry "
            f"withdrawal_date (§4.3 invariant)"
        )
    sets = ["consent_status=?"]
    params: list = [consent_status]
    # Always allow explicit consent_date update.
    if consent_date is not None:
        sets.append("consent_date=?")
        params.append(consent_date)
    sets.append("withdrawal_date=?")
    params.append(withdrawal_date)
    params.append(patient_id)
    conn.execute(
        f'UPDATE "patients" SET {", ".join(sets)} WHERE patient_id=?',
        params,
    )


def get_patient_consent(
    conn: sqlite3.Connection, patient_id: str
) -> dict | None:
    row = conn.execute(
        'SELECT consent_status, consent_date, withdrawal_date '
        'FROM "patients" WHERE patient_id=?',
        (patient_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "consent_status": row[0],
        "consent_date": row[1],
        "withdrawal_date": row[2],
    }


# ── Invariant checks (used by `casetrack validate`) ────────────────────────────


def consent_event_invariant_violations(conn: sqlite3.Connection) -> list[dict]:
    """Return rows where consent column state contradicts qc_events state.

    Two checks (§4.3):
    1. ``consent_status='revoked'`` requires an active ``consent_revoked`` event.
    2. ``withdrawal_date`` non-NULL iff ``consent_status`` in revoke-family.
    """
    violations: list[dict] = []
    # Check 1.
    rows = conn.execute(
        'SELECT patient_id FROM "patients" WHERE consent_status=\'revoked\''
    ).fetchall()
    for (pid,) in rows:
        (cnt,) = conn.execute(
            "SELECT COUNT(*) FROM qc_events "
            "WHERE level='patient' AND entity_id=? AND kind='consent_revoked' "
            "AND resolved_at IS NULL",
            (pid,),
        ).fetchone()
        if cnt == 0:
            violations.append({
                "kind": "missing_consent_revoked_event",
                "patient_id": pid,
                "message": (
                    f"patient {pid!r} has consent_status='revoked' but no "
                    "active qc_events row with kind='consent_revoked'"
                ),
            })
    # Check 2.
    rows = conn.execute(
        'SELECT patient_id, consent_status, withdrawal_date FROM "patients"'
    ).fetchall()
    for pid, status, wd in rows:
        must_have_wd = status in REVOKED_STATUSES
        has_wd = wd is not None
        if must_have_wd and not has_wd:
            violations.append({
                "kind": "missing_withdrawal_date",
                "patient_id": pid,
                "message": (
                    f"patient {pid!r} has consent_status={status!r} but no "
                    "withdrawal_date"
                ),
            })
        elif not must_have_wd and has_wd:
            violations.append({
                "kind": "spurious_withdrawal_date",
                "patient_id": pid,
                "message": (
                    f"patient {pid!r} has withdrawal_date={wd!r} but "
                    f"consent_status={status!r} (should be NULL)"
                ),
            })
    return violations


__all__ = [
    "REVOKED_STATUSES",
    "consent_event_invariant_violations",
    "ethics_override_reason_ok",
    "get_patient_consent",
    "set_patient_consent",
]
