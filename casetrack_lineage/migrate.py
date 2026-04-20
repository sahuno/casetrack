"""`casetrack migrate-lineage` — one-shot schema upgrade for proposal 0006.

Creates ``batches`` and ``assay_sources`` tables and adds ``batch_id`` to
``assays``.  Idempotent: safe to run more than once.

Optionally copies ``flowcell_id`` → ``batch_id`` for assays that already
have a flowcell_id set (--map-flowcell-to-batch).

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import sys

import casetrack
from casetrack_lineage.schema import (
    ASSAY_SOURCES_DDL,
    BATCH_ID_COLUMN,
    BATCHES_DDL,
    CREATE_UNIQUE_INDEX,
    has_batch_id_column,
    lineage_schema_exists,
)


def cmd_migrate_lineage(args) -> None:
    """Create batches + assay_sources tables; add batch_id column to assays.

    Parameters
    ----------
    args:
        Parsed :class:`argparse.Namespace` with at minimum:
        - ``project_dir`` (str | Path)
        - ``map_flowcell_to_batch`` (bool, optional)
    """
    project_dir, _schema = casetrack._resolve_project(
        args.project_dir, bypass_legacy_gate=True
    )
    db_path = project_dir / casetrack.PROJECT_DB_NAME

    conn = casetrack.open_project_db(db_path)
    mapped_n = 0
    try:
        with casetrack.begin_immediate(conn):
            # Create tables (idempotent — IF NOT EXISTS).
            conn.execute(BATCHES_DDL)
            conn.execute(ASSAY_SOURCES_DDL)
            conn.execute(CREATE_UNIQUE_INDEX)

            # Add batch_id column (ignore "duplicate column" if already exists).
            if not has_batch_id_column(conn):
                try:
                    conn.execute(BATCH_ID_COLUMN)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise

            # Optionally seed batch_id from flowcell_id.
            if getattr(args, "map_flowcell_to_batch", False):
                # Only copy if flowcell_id column exists on assays.
                cols = {
                    row[1]
                    for row in conn.execute(
                        'PRAGMA table_info("assays")'
                    ).fetchall()
                }
                if "flowcell_id" in cols:
                    # Ensure every distinct flowcell_id that is about to become
                    # a batch_id already has a row in batches (FK constraint).
                    flowcell_ids = [
                        r[0]
                        for r in conn.execute(
                            "SELECT DISTINCT flowcell_id FROM assays "
                            "WHERE flowcell_id IS NOT NULL AND batch_id IS NULL"
                        ).fetchall()
                    ]
                    for fid in flowcell_ids:
                        conn.execute(
                            "INSERT OR IGNORE INTO batches (batch_id) VALUES (?)",
                            (fid,),
                        )
                    conn.execute(
                        """
                        UPDATE assays
                        SET    batch_id = flowcell_id
                        WHERE  flowcell_id IS NOT NULL
                        AND    batch_id   IS NULL
                        """
                    )
                    mapped_n = conn.execute("SELECT changes()").fetchone()[0]
                    print(
                        f"Mapped flowcell_id → batch_id for {mapped_n} assay(s)."
                    )
                else:
                    print(
                        "Warning: --map-flowcell-to-batch requested but assays "
                        "table has no flowcell_id column; skipped.",
                        file=sys.stderr,
                    )

        entry = {
            "action": "migrate_lineage",
            "project_dir": str(project_dir),
            "map_flowcell_to_batch": getattr(args, "map_flowcell_to_batch", False),
            "flowcell_mapped_n": mapped_n,
        }
        casetrack.log_project_provenance(project_dir, entry)

        print(
            "Migration complete — batches + assay_sources tables created; "
            "batch_id column added to assays."
        )
    finally:
        conn.close()


__all__ = ["cmd_migrate_lineage"]
