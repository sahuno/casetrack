"""SLURM summary-TSV auto-flag convention.

Proposal 0002 §6. When a summarize script emits any of the reserved columns
``qc_pass`` (BOOLEAN), ``qc_fail_reason`` (TEXT), ``qc_warn`` (BOOLEAN),
``casetrack append`` reads them, writes ``qc_events`` rows + bumps
``assays.qc_status``, all inside the same transaction as the analysis
columns. The three columns are consumed — they never become analysis columns
themselves.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import datetime
import os
import sqlite3
from typing import Iterable

import pandas as pd

from casetrack_qc import consent as consent_mod
from casetrack_qc import events as events_mod
from casetrack_qc.events import TIMESTAMP_FMT


# Reserved column names carved out of the summary TSV. `casetrack append` drops
# them before inferring analysis-column types.
AUTOFLAG_COLUMNS: tuple[str, ...] = ("qc_pass", "qc_fail_reason", "qc_warn")


def _is_truthy(val) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        # NaN is falsy.
        if val != val:  # NaN
            return False
        return bool(val)
    s = str(val).strip().lower()
    if s in ("", "nan", "none", "null"):
        return False
    return s in ("true", "t", "1", "yes", "y")


def _is_false(val) -> bool:
    """True iff the value is an explicit False (not NaN / missing)."""
    if val is None:
        return False
    if isinstance(val, bool):
        return not val
    if isinstance(val, (int, float)):
        if val != val:
            return False
        return val == 0
    s = str(val).strip().lower()
    if s in ("", "nan", "none", "null"):
        return False
    return s in ("false", "f", "0", "no", "n")


def detect_autoflag_columns(columns: Iterable[str]) -> list[str]:
    """Return the subset of ``columns`` that match the autoflag convention."""
    cols = set(columns)
    return [c for c in AUTOFLAG_COLUMNS if c in cols]


def extract_flag_actions(
    df: pd.DataFrame, key_col: str
) -> list[dict]:
    """Return per-row autoflag actions: [{entity_id, action, reason}, ...]

    ``action`` is one of:
    - ``"fail"`` — ``qc_pass`` is explicitly False.
    - ``"warn"`` — ``qc_warn`` is True (and ``qc_pass`` is not False).
    - ``None`` — no flag to apply.

    Rows without any autoflag signal are omitted from the output.
    """
    actions: list[dict] = []
    has_pass = "qc_pass" in df.columns
    has_reason = "qc_fail_reason" in df.columns
    has_warn = "qc_warn" in df.columns
    if not (has_pass or has_warn):
        return actions
    for _, row in df.iterrows():
        entity_id = str(row[key_col])
        reason_val = row["qc_fail_reason"] if has_reason else None
        reason = (
            None
            if (reason_val is None or (isinstance(reason_val, float) and reason_val != reason_val))
            else str(reason_val).strip() or None
        )
        if has_pass and _is_false(row["qc_pass"]):
            actions.append({
                "entity_id": entity_id,
                "action": "fail",
                "reason": reason or "slurm: qc_pass=False",
            })
            continue
        if has_warn and _is_truthy(row["qc_warn"]):
            actions.append({
                "entity_id": entity_id,
                "action": "warn",
                "reason": reason or "slurm: qc_warn=True",
            })
    return actions


def apply_autoflag(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    key_col: str,
    *,
    level: str,
    transaction_id: str,
    source: str = "slurm",
    created_by: str | None = None,
) -> list[dict]:
    """Read autoflag columns from ``df`` and write events + update qc_status.

    Called from inside ``cmd_append``'s ``begin_immediate`` envelope so both
    data and QC land atomically. Skips entities that already have an active
    event of the matching kind (caller is responsible for the strict-refuse
    check for pre-existing fail/censored status).
    """
    actions = extract_flag_actions(df, key_col)
    if not actions:
        return []
    if created_by is None:
        if source == "slurm":
            created_by = f"slurm:{os.environ.get('SLURM_JOB_ID', 'unknown')}"
        else:
            created_by = os.environ.get("USER", "unknown")

    created_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    emitted: list[dict] = []
    for action in actions:
        entity_id = action["entity_id"]
        kind = "qc_fail" if action["action"] == "fail" else "qc_warn"
        # Skip if an active event of the same kind already exists.
        already = events_mod.get_active_event(
            conn, level=level, entity_id=entity_id, kind=kind
        )
        if already is not None:
            continue
        if not events_mod.entity_exists(conn, level, entity_id):
            # Shouldn't happen — append gates on existing keys first — but be
            # defensive.
            continue
        event_id = events_mod.insert_event(
            conn,
            level=level,
            entity_id=entity_id,
            kind=kind,
            reason=action["reason"],
            source=source,
            created_by=created_by,
            transaction_id=transaction_id,
            created_at=created_at,
        )
        new_status = events_mod.recompute_entity_status(
            conn, level, entity_id
        )
        emitted.append({
            "entity_id": entity_id,
            "kind": kind,
            "reason": action["reason"],
            "qc_event_id": event_id,
            "new_qc_status": new_status,
        })
    return emitted


__all__ = [
    "AUTOFLAG_COLUMNS",
    "apply_autoflag",
    "detect_autoflag_columns",
    "extract_flag_actions",
]
