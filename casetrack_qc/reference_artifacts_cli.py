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


__all__ = ["cmd_migrate_references"]
