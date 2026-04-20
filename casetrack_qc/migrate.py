"""`casetrack migrate-qc` — one-shot v0.3 → v0.4 upgrade.

Proposal 0002 §10. Adds the QC schema in-place on an existing v0.3 project:

1. ``qc_events`` table.
2. ``qc_status`` fast-filter columns on patients/specimens/assays.
3. Consent columns on patients (default ``consented``).
4. Migrates any legacy ``qc_pass`` boolean on assays into ``qc_status``
   + ``qc_events`` rows.
5. Drops the legacy column (SQLite ≥ 3.35 supports ``DROP COLUMN`` natively).
6. Appends ``[qc]``/``[qc.kind_scopes]`` to ``casetrack.toml`` if absent.
7. Writes a single ``action='migrate_qc'`` provenance entry.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import casetrack
from casetrack_qc import events as events_mod
from casetrack_qc.schema import (
    ensure_qc_schema,
    parse_qc_config,
    qc_schema_exists,
    write_qc_toml_block,
)


def _has_column(conn, table: str, column: str) -> bool:
    for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
        if row[1] == column:
            return True
    return False


def cmd_migrate_qc(args) -> None:
    # migrate-qc is an upgrade path — by definition it operates on legacy
    # projects, so it bypasses the v0.6 hard-error gate. End users still
    # see the gate when running normal read/write commands.
    project_dir, schema = casetrack._resolve_project(
        args.project_dir, bypass_legacy_gate=True
    )
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    toml_path = project_dir / casetrack.PROJECT_TOML_NAME

    dry_run = bool(getattr(args, "dry_run", False))
    legacy_col = getattr(args, "qc_pass_column", None) or "qc_pass"

    conn = casetrack.open_project_db(db_path)
    executed_sql: list[str] = []
    migrated_rows: list[dict] = []
    toml_updated = False

    try:
        already = qc_schema_exists(conn)
        legacy_present = _has_column(conn, "assays", legacy_col)
        qc_cfg = parse_qc_config(schema)

        if already and not legacy_present:
            print(
                "No migration needed — QC schema already in place and no legacy "
                f"{legacy_col!r} column on assays."
            )
            return

        # Inspect legacy values BEFORE we ALTER anything so the scan uses the
        # current table shape.
        legacy_rows: list[tuple[str, int | None]] = []
        if legacy_present:
            legacy_rows = conn.execute(
                f'SELECT assay_id, "{legacy_col}" FROM "assays"'
            ).fetchall()

        if dry_run:
            print("[dry-run] Would execute:")
            if not already:
                print("  - ensure_qc_schema (CREATE TABLE qc_events, ADD COLUMNS)")
            migrate_count = sum(1 for _, v in legacy_rows if v == 0)
            print(
                f"  - migrate {migrate_count} assay row(s) where "
                f"{legacy_col}=FALSE → qc_status='fail' + qc_events entry"
            )
            if legacy_present:
                print(f"  - DROP COLUMN {legacy_col!r} from assays")
            return

        txn_id = casetrack._new_transaction_id()
        created_at = datetime.datetime.now().strftime(
            events_mod.TIMESTAMP_FMT
        )

        with casetrack.begin_immediate(conn):
            executed_sql.extend(ensure_qc_schema(conn, kinds=qc_cfg["kinds"]))

            if legacy_rows:
                for assay_id, val in legacy_rows:
                    if val == 1:
                        # True → already default 'pass', nothing to do.
                        continue
                    if val is None:
                        # NULL → treat as unknown; leave default 'pass' per §10.
                        continue
                    # FALSE path → insert resolved-less qc_events + update status.
                    event_id = events_mod.insert_event(
                        conn,
                        level="assay",
                        entity_id=assay_id,
                        kind="qc_fail",
                        reason="migrated from legacy qc_pass",
                        source="import",
                        created_by=f"import:migrate-qc",
                        transaction_id=txn_id,
                        created_at=created_at,
                    )
                    status = events_mod.recompute_entity_status(
                        conn, "assay", assay_id
                    )
                    migrated_rows.append(
                        {"assay_id": assay_id, "qc_event_id": event_id,
                         "new_qc_status": status}
                    )

            if legacy_present:
                # SQLite ≥ 3.35 supports DROP COLUMN natively. We tested the
                # runtime's version via `python3 -c ... sqlite_version` = 3.51,
                # so this path is always available on supported installs.
                try:
                    conn.execute(f'ALTER TABLE "assays" DROP COLUMN "{legacy_col}"')
                    executed_sql.append(
                        f'ALTER TABLE "assays" DROP COLUMN "{legacy_col}"'
                    )
                except Exception as e:
                    # Fall back to warning — the rest of migration still works.
                    print(
                        f"Warning: couldn't drop legacy column "
                        f"{legacy_col!r}: {e}. Migration continues.",
                        file=sys.stderr,
                    )

        toml_updated = write_qc_toml_block(toml_path)

        entry = {
            "action": "migrate_qc",
            "transaction_id": txn_id,
            "executed_sql": executed_sql,
            "migrated_rows": migrated_rows,
            "legacy_column": legacy_col if legacy_present else None,
            "toml_updated": toml_updated,
        }
        casetrack.log_project_provenance(project_dir, entry)

        print(
            "Migrated project to v0.4 QC schema:\n"
            f"  - DDL statements:     {len(executed_sql)}\n"
            f"  - Legacy rows ported: {len(migrated_rows)}\n"
            f"  - [qc] block added:   {toml_updated}"
        )
    finally:
        conn.close()


__all__ = ["cmd_migrate_qc"]
