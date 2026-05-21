"""CLI commands for reference artifacts (proposal 0010 §6.3).

- ``migrate-references`` — additive: create the two tables on a pre-0010 project.

Mirrors ``casetrack_qc.cohort_artifacts_cli.cmd_migrate_cohort`` exactly:
resolve project → open db → check schema → one ``begin_immediate`` write →
provenance entry.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import sys

import casetrack
from casetrack_qc import reference_artifacts as ra


def capture_reference_usage(conn, *, schema: dict, analysis: str, level: str,
                            entity_ids: list[str], transaction_id: str,
                            override_refs: list[str] | None = None) -> int:
    """Record reference_usage for every (entity, ref) this analysis consumed.

    ref keys come from override_refs if given, else [analyses.<analysis>].uses.
    The version recorded is the current canonical version from reference_artifacts.
    No-op (returns 0) when there are no ref keys. Caller owns the transaction.
    """
    if override_refs is not None:
        ref_keys = override_refs
    else:
        ref_keys = (schema.get("analyses", {}).get(analysis, {}) or {}).get("uses", [])
    if not ref_keys:
        return 0
    ra.ensure_reference_schema(conn)
    current = {r.ref_key: r.version for r in ra.list_references(conn)}
    n = 0
    for ref_key in ref_keys:
        version = current.get(ref_key)
        if version is None:
            # declared-but-unsynced ref: skip silently; validate/doctor flags it
            continue
        for eid in entity_ids:
            ra.record_usage(conn, scope="analysis", entity_level=level,
                            entity_id=eid, analysis=analysis, ref_key=ref_key,
                            version_used=version, transaction_id=transaction_id)
            n += 1
    return n


def cmd_migrate_references(args) -> None:
    # Upgrade path — operates on pre-0010 projects, so bypass the legacy gate.
    project_dir, _ = casetrack._resolve_project(
        args.project_dir, bypass_legacy_gate=True
    )
    db_path = project_dir / casetrack.PROJECT_DB_NAME

    conn = casetrack.open_project_db(db_path)
    try:
        if ra.reference_schema_exists(conn):
            print("No migration needed — reference schema already in place.")
            return
        if getattr(args, "dry_run", False):
            print(
                "[dry-run] Would create reference_artifacts + "
                "reference_usage tables (+ indexes)."
            )
            return
        txn_id = casetrack._new_transaction_id()
        with casetrack.begin_immediate(conn):
            executed = ra.ensure_reference_schema(conn)
        casetrack.log_project_provenance(
            project_dir,
            {
                "action": "migrate_references",
                "executed_sql": executed,
                "transaction_id": txn_id,
            },
        )
        print(f"Created reference schema ({len(executed)} statements).")
    finally:
        conn.close()


__all__ = ["cmd_migrate_references", "capture_reference_usage"]
