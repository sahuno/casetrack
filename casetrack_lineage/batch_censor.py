"""`casetrack censor --batch` cascade (proposal 0006 §3).

When a whole sequencing/library-prep batch is found to be problematic,
this module censors all assays in the batch and propagates the effect
downstream:

- **Mode A** (merged assay): any ``assay_sources`` row where a source assay
  is in the batch and the edge points to a ``merged_assay_id`` → censor the
  merged assay (kind=``qc_fail``).
- **Mode B** (consumer specimen): same scenario but the edge points to a
  ``consumer_specimen_id`` → warn the specimen (kind=``qc_warn``).

``cmd_uncensor_batch`` reverses via the append-only event pattern (writes
``resolved_at`` on existing events, resets qc_status).

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import datetime
import os
import sys

import casetrack
from casetrack_lineage.schema import lineage_schema_exists
from casetrack_qc import events as events_mod
from casetrack_qc.events import (
    TIMESTAMP_FMT,
    QcEventError,
    get_active_event,
    insert_event,
    list_active_events_for_entity,
    recompute_entity_status,
    resolve_event,
)
from casetrack_qc.schema import qc_schema_exists


# ── helpers ───────────────────────────────────────────────────────────────────


def _error(msg: str, exit_code: int = 1) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(exit_code)


def _require_schemas(conn) -> None:
    if not qc_schema_exists(conn):
        _error(
            "project has no QC schema. "
            "Run `casetrack migrate-qc --project-dir <DIR>`.",
            exit_code=1,
        )
    if not lineage_schema_exists(conn):
        _error(
            "project has no lineage schema. "
            "Run `casetrack migrate-lineage --project-dir <DIR>`.",
            exit_code=1,
        )


def _safe_insert_event(conn, *, level, entity_id, kind, reason, source,
                       created_by, txn_id, created_at) -> int | None:
    """Insert a qc_event, skipping if one with the same (level, entity_id, kind)
    is already active.  Returns the new event_id or None if skipped."""
    active = get_active_event(conn, level=level, entity_id=entity_id, kind=kind)
    if active is not None:
        return None
    return insert_event(
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


# ── censor-batch ──────────────────────────────────────────────────────────────


def cmd_censor_batch(args) -> None:
    """Censor all assays in ``args.batch`` and cascade to downstream entities.

    Summary line printed on completion:
    ``Censored N assays in batch B. Warned M specimens. Censored P derived assays.``
    """
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        _require_schemas(conn)

        batch_id = args.batch
        reason = getattr(args, "reason", None) or f"batch {batch_id!r} censored"
        source = getattr(args, "source", None) or "manual"
        created_by = os.environ.get("USER", "unknown")
        txn_id = casetrack._new_transaction_id()
        created_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)

        # 1. Fetch all assays in this batch.
        assay_rows = conn.execute(
            "SELECT assay_id FROM assays WHERE batch_id=?", (batch_id,)
        ).fetchall()
        if not assay_rows:
            print(
                f"Warning: no assays found with batch_id={batch_id!r}.",
                file=sys.stderr,
            )
            return

        assay_ids = [r[0] for r in assay_rows]
        censored_assays: list[str] = []
        warned_specimens: list[str] = []
        censored_derived: list[str] = []

        try:
            with casetrack.begin_immediate(conn):
                # 2. Censor each batch assay.
                for assay_id in assay_ids:
                    ev_id = _safe_insert_event(
                        conn,
                        level="assay",
                        entity_id=assay_id,
                        kind="qc_fail",
                        reason=reason,
                        source=source,
                        created_by=created_by,
                        txn_id=txn_id,
                        created_at=created_at,
                    )
                    recompute_entity_status(conn, "assay", assay_id)
                    if ev_id is not None:
                        censored_assays.append(assay_id)

                # 3. Find downstream edges from the censored set.
                placeholders = ", ".join("?" * len(assay_ids))
                edges = conn.execute(
                    f"""
                    SELECT source_assay_id, merged_assay_id, consumer_specimen_id
                    FROM   assay_sources
                    WHERE  source_assay_id IN ({placeholders})
                    """,
                    assay_ids,
                ).fetchall()

                seen_specimens: set[str] = set()
                seen_derived: set[str] = set()

                for _src, merged_id, specimen_id in edges:
                    # Mode A: censor merged assay.
                    if merged_id is not None and merged_id not in seen_derived:
                        seen_derived.add(merged_id)
                        ev_id = _safe_insert_event(
                            conn,
                            level="assay",
                            entity_id=merged_id,
                            kind="qc_fail",
                            reason=f"source assay in batch {batch_id!r} censored",
                            source=source,
                            created_by=created_by,
                            txn_id=txn_id,
                            created_at=created_at,
                        )
                        recompute_entity_status(conn, "assay", merged_id)
                        if ev_id is not None:
                            censored_derived.append(merged_id)

                    # Mode B: warn consumer specimen.
                    if specimen_id is not None and specimen_id not in seen_specimens:
                        seen_specimens.add(specimen_id)
                        ev_id = _safe_insert_event(
                            conn,
                            level="specimen",
                            entity_id=specimen_id,
                            kind="qc_warn",
                            reason=f"source assay in batch {batch_id!r} censored",
                            source=source,
                            created_by=created_by,
                            txn_id=txn_id,
                            created_at=created_at,
                        )
                        recompute_entity_status(conn, "specimen", specimen_id)
                        if ev_id is not None:
                            warned_specimens.append(specimen_id)

        except QcEventError as e:
            _error(str(e), exit_code=2)

        entry = {
            "action": "censor_batch",
            "batch_id": batch_id,
            "reason": reason,
            "source": source,
            "created_by": created_by,
            "created_at": created_at,
            "transaction_id": txn_id,
            "censored_assays": censored_assays,
            "warned_specimens": warned_specimens,
            "censored_derived_assays": censored_derived,
        }
        casetrack.log_project_provenance(project_dir, entry)

        print(
            f"Censored {len(censored_assays)} assay(s) in batch {batch_id!r}. "
            f"Warned {len(warned_specimens)} specimen(s). "
            f"Censored {len(censored_derived)} derived assay(s)."
        )
    finally:
        conn.close()


# ── uncensor-batch ────────────────────────────────────────────────────────────


def cmd_uncensor_batch(args) -> None:
    """Reverse a batch censor via append-only resolved_at writes.

    Resolves all active qc_events on assays with ``batch_id = args.batch``
    plus any downstream warn/censor events that were emitted by
    ``cmd_censor_batch``.
    """
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        _require_schemas(conn)

        batch_id = args.batch
        reason = getattr(args, "reason", None) or f"batch {batch_id!r} uncensored"
        resolved_by = os.environ.get("USER", "unknown")
        resolved_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
        txn_id = casetrack._new_transaction_id()

        assay_rows = conn.execute(
            "SELECT assay_id FROM assays WHERE batch_id=?", (batch_id,)
        ).fetchall()
        if not assay_rows:
            print(
                f"Warning: no assays found with batch_id={batch_id!r}.",
                file=sys.stderr,
            )
            return

        assay_ids = [r[0] for r in assay_rows]
        resolved_assays: list[str] = []
        resolved_specimens: list[str] = []
        resolved_derived: list[str] = []

        try:
            with casetrack.begin_immediate(conn):
                # Resolve active events on the batch assays.
                for assay_id in assay_ids:
                    active_events = list_active_events_for_entity(
                        conn, "assay", assay_id
                    )
                    for ev in active_events:
                        resolve_event(
                            conn,
                            ev.id,
                            resolved_by=resolved_by,
                            resolved_reason=reason,
                            resolved_at=resolved_at,
                        )
                    recompute_entity_status(conn, "assay", assay_id)
                    if active_events:
                        resolved_assays.append(assay_id)

                # Find downstream edges and resolve their events.
                placeholders = ", ".join("?" * len(assay_ids))
                edges = conn.execute(
                    f"""
                    SELECT source_assay_id, merged_assay_id, consumer_specimen_id
                    FROM   assay_sources
                    WHERE  source_assay_id IN ({placeholders})
                    """,
                    assay_ids,
                ).fetchall()

                seen_specimens: set[str] = set()
                seen_derived: set[str] = set()

                for _src, merged_id, specimen_id in edges:
                    if merged_id is not None and merged_id not in seen_derived:
                        seen_derived.add(merged_id)
                        active_events = list_active_events_for_entity(
                            conn, "assay", merged_id
                        )
                        for ev in active_events:
                            resolve_event(
                                conn, ev.id,
                                resolved_by=resolved_by,
                                resolved_reason=reason,
                                resolved_at=resolved_at,
                            )
                        recompute_entity_status(conn, "assay", merged_id)
                        if active_events:
                            resolved_derived.append(merged_id)

                    if specimen_id is not None and specimen_id not in seen_specimens:
                        seen_specimens.add(specimen_id)
                        active_events = list_active_events_for_entity(
                            conn, "specimen", specimen_id
                        )
                        for ev in active_events:
                            resolve_event(
                                conn, ev.id,
                                resolved_by=resolved_by,
                                resolved_reason=reason,
                                resolved_at=resolved_at,
                            )
                        recompute_entity_status(conn, "specimen", specimen_id)
                        if active_events:
                            resolved_specimens.append(specimen_id)

        except QcEventError as e:
            _error(str(e), exit_code=2)

        entry = {
            "action": "uncensor_batch",
            "batch_id": batch_id,
            "reason": reason,
            "resolved_by": resolved_by,
            "resolved_at": resolved_at,
            "transaction_id": txn_id,
            "resolved_assays": resolved_assays,
            "resolved_specimens": resolved_specimens,
            "resolved_derived_assays": resolved_derived,
        }
        casetrack.log_project_provenance(project_dir, entry)

        print(
            f"Uncensored batch {batch_id!r}: "
            f"{len(resolved_assays)} assay(s), "
            f"{len(resolved_specimens)} specimen(s), "
            f"{len(resolved_derived)} derived assay(s) resolved."
        )
    finally:
        conn.close()


__all__ = ["cmd_censor_batch", "cmd_uncensor_batch"]
