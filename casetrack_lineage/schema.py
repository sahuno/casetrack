"""DDL for the assay-lineage + batch tables (proposal 0006 §1).

Two new tables:
- ``batches``       — library-prep / sequencing batch metadata.
- ``assay_sources`` — directed edge: source_assay → (merged_assay | specimen).

One new column:
- ``assays.batch_id TEXT REFERENCES batches(batch_id)``

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

# ── DDL statements ─────────────────────────────────────────────────────────────

BATCHES_DDL = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT PRIMARY KEY,
    prep_date   TEXT,
    reagent_lot TEXT,
    operator    TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
"""

# SQLite does not support expression PKs (COALESCE in PK), so we use a
# plain nullable PK and enforce de-duplication via a UNIQUE index that
# substitutes NULLs with a sentinel string.
ASSAY_SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS assay_sources (
    source_assay_id      TEXT NOT NULL REFERENCES assays(assay_id),
    merged_assay_id      TEXT REFERENCES assays(assay_id),
    consumer_specimen_id TEXT REFERENCES specimens(specimen_id),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    PRIMARY KEY (source_assay_id, merged_assay_id, consumer_specimen_id),
    CHECK (
        (merged_assay_id IS NOT NULL) + (consumer_specimen_id IS NOT NULL) = 1
    ),
    CHECK (merged_assay_id IS NULL OR merged_assay_id != source_assay_id)
);
"""

# Unique index using COALESCE sentinels so NULL columns participate in
# de-duplication (SQLite NULLs are distinct from each other in PK/UNIQUE).
CREATE_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_assay_sources
ON assay_sources(
    source_assay_id,
    COALESCE(merged_assay_id,      '__NULL__'),
    COALESCE(consumer_specimen_id, '__NULL__')
);
"""

BATCH_ID_COLUMN = (
    "ALTER TABLE assays ADD COLUMN batch_id TEXT REFERENCES batches(batch_id);"
)


# ── Introspection helpers ──────────────────────────────────────────────────────

def lineage_schema_exists(conn) -> bool:
    """True when both ``batches`` and ``assay_sources`` tables are present."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return "batches" in tables and "assay_sources" in tables


def has_batch_id_column(conn) -> bool:
    """True when ``assays.batch_id`` column exists."""
    for row in conn.execute('PRAGMA table_info("assays")').fetchall():
        if row[1] == "batch_id":
            return True
    return False


__all__ = [
    "BATCHES_DDL",
    "ASSAY_SOURCES_DDL",
    "CREATE_UNIQUE_INDEX",
    "BATCH_ID_COLUMN",
    "lineage_schema_exists",
    "has_batch_id_column",
]
