"""`casetrack censor` / `uncensor` / `qc-history` CLI commands.

Proposal 0002 §5.1. These sit in the public API of ``casetrack_qc`` and are
dispatched from ``casetrack.main()`` via :func:`casetrack_qc.cli.qc_command_dispatch`.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import sys
from pathlib import Path

import casetrack
from casetrack_qc import consent as consent_mod
from casetrack_qc import events as events_mod
from casetrack_qc.events import (
    LEVEL_KEYS,
    LEVEL_TABLES,
    TIMESTAMP_FMT,
    QcEventError,
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
    qc_schema_exists,
    parse_qc_config,
)


# ── small helpers ──────────────────────────────────────────────────────────────


def _created_by(source: str) -> str:
    """Render `created_by` per §12 Q8."""
    if source == "slurm":
        job = os.environ.get("SLURM_JOB_ID", "unknown")
        return f"slurm:{job}"
    if source == "import":
        # Caller should override with the basename of the --from file.
        return "import:unknown"
    return os.environ.get("USER", "unknown")


def _error(msg: str, exit_code: int = 1) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(exit_code)


def _require_qc_schema(conn) -> None:
    if not qc_schema_exists(conn):
        _error(
            "project has no QC schema. Run `casetrack migrate-qc --project-dir <DIR>`.",
            exit_code=1,
        )


def _load_qc_config(project_dir: Path) -> dict:
    toml_path = project_dir / casetrack.PROJECT_TOML_NAME
    try:
        schema = casetrack.load_schema(toml_path)
    except casetrack.SchemaError:
        return parse_qc_config(None)
    return parse_qc_config(schema)


# ── censor ────────────────────────────────────────────────────────────────────


def cmd_censor(args) -> None:
    """Manual censoring entry point.

    Modes:
    - Single event:  ``--level --id --kind --reason [--withdrawal-date]``
    - Bulk import:   ``--from FILE`` (TSV with columns
                     ``level, entity_id, kind, reason``).
    """
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        _require_qc_schema(conn)
        qc_cfg = _load_qc_config(project_dir)

        if args.from_file:
            _cmd_censor_bulk(conn, project_dir, qc_cfg, args)
        else:
            _cmd_censor_single(conn, project_dir, qc_cfg, args)
    finally:
        conn.close()


def _cmd_censor_single(conn, project_dir, qc_cfg, args) -> None:
    if not all((args.level, args.id, args.kind, args.reason)):
        _error(
            "single-event censor requires --level, --id, --kind, --reason "
            "(use --from FILE for bulk).",
            exit_code=1,
        )

    level = args.level
    entity_id = args.id
    kind = args.kind
    reason = args.reason
    source = args.source or qc_cfg.get("default_source", "manual")

    try:
        validate_kind_for_level(
            kind, level,
            kinds=qc_cfg["kinds"],
            kind_scopes=qc_cfg["kind_scopes"],
        )
    except QcEventError as e:
        _error(str(e), exit_code=2)

    created_by = _created_by(source)
    txn_id = casetrack._new_transaction_id()
    created_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)

    # Consent revocation requires withdrawal-date handling (§7).
    consent_revoked = (kind == "consent_revoked")
    if consent_revoked and level == "patient" and not args.withdrawal_date:
        # Default to today's date — users expected to pass it, but don't fail.
        args.withdrawal_date = datetime.date.today().isoformat()

    try:
        with casetrack.begin_immediate(conn):
            if not entity_exists(conn, level, entity_id):
                raise QcEventError(
                    f"{level} {entity_id!r} not found — register it first "
                    f"(or use `casetrack register --level {level} --id {entity_id}`)."
                )
            event_id = insert_event(
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
            new_status = recompute_entity_status(conn, level, entity_id)

            if consent_revoked and level == "patient":
                consent_mod.set_patient_consent(
                    conn, entity_id,
                    consent_status="revoked",
                    withdrawal_date=args.withdrawal_date,
                )
    except QcEventError as e:
        _error(str(e), exit_code=2)
    except Exception as e:
        _error(f"censor failed: {e}", exit_code=1)

    entry = {
        "action": "censor",
        "level": level,
        "entity_id": entity_id,
        "kind": kind,
        "reason": reason,
        "source": source,
        "created_by": created_by,
        "created_at": created_at,
        "transaction_id": txn_id,
        "qc_event_id": event_id,
        "new_qc_status": new_status,
    }
    if consent_revoked:
        entry["ethics"] = True
        entry["withdrawal_date"] = args.withdrawal_date
    casetrack.log_project_provenance(project_dir, entry)

    print(
        f"Censored {level} {entity_id!r}: kind={kind}, "
        f"event_id={event_id}, qc_status={new_status}"
    )


def _cmd_censor_bulk(conn, project_dir, qc_cfg, args) -> None:
    src = Path(args.from_file)
    if not src.exists():
        _error(f"--from file not found: {src}", exit_code=1)
    with open(src, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        rows = list(reader)
    required = {"level", "entity_id", "kind", "reason"}
    if not rows:
        print(f"No rows in {src}", file=sys.stderr)
        return
    missing = required - set(rows[0].keys() or ())
    if missing:
        _error(
            f"--from file missing required columns: {sorted(missing)}; "
            f"expected {sorted(required)}",
            exit_code=1,
        )

    txn_id = casetrack._new_transaction_id()
    source = args.source or "import"
    created_by = f"import:{src.name}"
    created_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    emitted: list[dict] = []

    try:
        with casetrack.begin_immediate(conn):
            for idx, row in enumerate(rows, start=1):
                level = (row.get("level") or "").strip()
                entity_id = (row.get("entity_id") or "").strip()
                kind = (row.get("kind") or "").strip()
                reason = (row.get("reason") or "").strip()
                if not all((level, entity_id, kind, reason)):
                    raise QcEventError(
                        f"row {idx}: missing required field "
                        f"(level={level!r}, entity_id={entity_id!r}, "
                        f"kind={kind!r}, reason={reason!r})"
                    )
                validate_kind_for_level(
                    kind, level,
                    kinds=qc_cfg["kinds"],
                    kind_scopes=qc_cfg["kind_scopes"],
                )
                if not entity_exists(conn, level, entity_id):
                    raise QcEventError(
                        f"row {idx}: {level} {entity_id!r} not found"
                    )
                event_id = insert_event(
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
                new_status = recompute_entity_status(conn, level, entity_id)
                emitted.append({
                    "level": level,
                    "entity_id": entity_id,
                    "kind": kind,
                    "qc_event_id": event_id,
                    "new_qc_status": new_status,
                })
                if kind == "consent_revoked" and level == "patient":
                    # Bulk imports: default withdrawal_date to today.
                    consent_mod.set_patient_consent(
                        conn, entity_id,
                        consent_status="revoked",
                        withdrawal_date=datetime.date.today().isoformat(),
                    )
    except QcEventError as e:
        _error(str(e), exit_code=2)

    # One provenance entry per event, all sharing the same transaction_id
    # (§12 Q7 lean).
    for em in emitted:
        entry = {
            "action": "censor",
            "level": em["level"],
            "entity_id": em["entity_id"],
            "kind": em["kind"],
            "reason": next(
                r["reason"] for r in rows
                if (r.get("level") or "").strip() == em["level"]
                and (r.get("entity_id") or "").strip() == em["entity_id"]
                and (r.get("kind") or "").strip() == em["kind"]
            ),
            "source": source,
            "created_by": created_by,
            "created_at": created_at,
            "transaction_id": txn_id,
            "qc_event_id": em["qc_event_id"],
            "new_qc_status": em["new_qc_status"],
            "from_file": src.name,
        }
        if em["kind"] == "consent_revoked":
            entry["ethics"] = True
        casetrack.log_project_provenance(project_dir, entry)

    print(f"Bulk-censored {len(emitted)} events from {src}")


# ── uncensor ───────────────────────────────────────────────────────────────────


def cmd_uncensor(args) -> None:
    """Resolve an active qc_events row. Consent reversal gated by ``--ethics-override --yes``."""
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        _require_qc_schema(conn)

        reason = args.reason or ""
        if not reason.strip():
            _error("--reason is required", exit_code=1)

        event_id: int | None = None
        if args.event_id is not None:
            event_id = args.event_id
        else:
            # Sugar path: (--level, --id) with exactly one active event.
            if not (args.level and args.id):
                _error(
                    "either --event-id OR both --level and --id are required",
                    exit_code=1,
                )
            active = list_active_events_for_entity(conn, args.level, args.id)
            if not active:
                _error(
                    f"no active qc_events for {args.level} {args.id!r}",
                    exit_code=2,
                )
            if len(active) > 1:
                ids = ", ".join(str(e.id) for e in active)
                _error(
                    f"ambiguous: {args.level} {args.id!r} has {len(active)} "
                    f"active events ({ids}); pass --event-id explicitly",
                    exit_code=2,
                )
            event_id = active[0].id

        event = get_event_by_id(conn, event_id)
        if event is None:
            _error(f"qc_events id={event_id} not found", exit_code=2)
        if event.resolved_at is not None:
            _error(
                f"qc_events id={event_id} already resolved at {event.resolved_at}",
                exit_code=2,
            )

        # Consent reversal gate (§7.2 #4).
        ethics = False
        if event.kind == "consent_revoked":
            if not (args.ethics_override and args.yes):
                _error(
                    "uncensor of a consent_revoked event requires "
                    "--ethics-override --yes",
                    exit_code=2,
                )
            if not consent_mod.ethics_override_reason_ok(reason):
                _error(
                    "--reason must reference an IRB ref OR mention "
                    "re-consent OR contain an ISO date (YYYY-MM-DD)",
                    exit_code=2,
                )
            ethics = True

        resolved_by = os.environ.get("USER", "unknown")
        resolved_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
        txn_id = casetrack._new_transaction_id()

        try:
            with casetrack.begin_immediate(conn):
                resolve_event(
                    conn,
                    event_id,
                    resolved_by=resolved_by,
                    resolved_reason=reason,
                    resolved_at=resolved_at,
                )
                new_status = recompute_entity_status(
                    conn, event.level, event.entity_id
                )
                # If this was a consent revocation, flip the column back.
                if event.kind == "consent_revoked" and event.level == "patient":
                    consent_mod.set_patient_consent(
                        conn, event.entity_id,
                        consent_status="consented",
                        withdrawal_date=None,
                    )
        except Exception as e:
            _error(f"uncensor failed: {e}", exit_code=1)

        action = "ethics_override" if ethics else "uncensor"
        entry = {
            "action": action,
            "level": event.level,
            "entity_id": event.entity_id,
            "kind": event.kind,
            "reason": reason,
            "resolved_by": resolved_by,
            "resolved_at": resolved_at,
            "transaction_id": txn_id,
            "qc_event_id": event_id,
            "new_qc_status": new_status,
        }
        if ethics:
            entry["ethics"] = True
        casetrack.log_project_provenance(project_dir, entry)

        print(
            f"Uncensored qc_events id={event_id} "
            f"({event.level} {event.entity_id!r}, kind={event.kind}) "
            f"→ qc_status={new_status}"
        )
    finally:
        conn.close()


# ── qc-history ─────────────────────────────────────────────────────────────────


def cmd_qc_history(args) -> None:
    """Print the full history of events for one entity, or all active events."""
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        _require_qc_schema(conn)

        if args.level and args.id:
            events = list_events_for_entity(conn, args.level, args.id)
            header = f"qc-history for {args.level} {args.id!r}"
        elif args.level or args.id:
            _error("both --level and --id are required together", exit_code=1)
        else:
            events = events_mod.list_all_active(conn)
            header = "qc-history (all active events)"

        fmt = args.fmt or "table"
        if fmt == "json":
            print(json.dumps([e.to_dict() for e in events], indent=2))
            return
        if fmt == "tsv":
            cols = [
                "id", "level", "entity_id", "kind", "reason", "source",
                "created_at", "created_by", "resolved_at", "resolved_by",
                "resolved_reason", "transaction_id",
            ]
            print("\t".join(cols))
            for e in events:
                d = e.to_dict()
                print("\t".join("" if d[c] is None else str(d[c]) for c in cols))
            return

        # Table.
        print(header)
        if not events:
            print("  (none)")
            return
        for e in events:
            status = "ACTIVE" if e.resolved_at is None else "resolved"
            print(
                f"  id={e.id:<4} {status:<8} {e.level:<8} {e.entity_id:<32} "
                f"kind={e.kind:<22} src={e.source:<6} "
                f"created={e.created_at}"
            )
            print(f"         reason: {e.reason}")
            if e.resolved_at:
                print(
                    f"         resolved {e.resolved_at} by {e.resolved_by}: "
                    f"{e.resolved_reason}"
                )
    finally:
        conn.close()


__all__ = ["cmd_censor", "cmd_qc_history", "cmd_uncensor"]
