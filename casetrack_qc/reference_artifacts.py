"""Reference artifacts: schema, CRUD, and read-time staleness.

Proposal 0010. A reference artifact is a versioned, per-file external input
(genome, annotation, known-variant set, …) declared in the TOML [references]
block and materialized here. ``reference_usage`` records which output consumed
which reference at which version; staleness is derived live at read time when
a recorded version no longer matches the current canonical version.

Mirrors casetrack_qc/cohort_artifacts.py: module functions take an open conn,
the command layer owns the transaction + provenance, all DDL is idempotent.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import datetime
import sqlite3
from dataclasses import asdict, dataclass

TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S"

REFERENCE_KINDS = (
    "genome", "annotation", "known_variants", "repeats", "intervals", "other",
)


def reference_artifacts_ddl() -> str:
    return (
        "CREATE TABLE reference_artifacts (\n"
        "    ref_key    TEXT PRIMARY KEY,\n"
        "    path       TEXT NOT NULL,\n"
        "    version    TEXT NOT NULL,\n"
        "    kind       TEXT,\n"
        "    checksum   TEXT,\n"
        "    updated_at TEXT NOT NULL\n"
        ")"
    )


def reference_usage_ddl() -> str:
    return (
        "CREATE TABLE reference_usage (\n"
        "    usage_id       INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    scope          TEXT NOT NULL,\n"
        "    entity_level   TEXT,\n"
        "    entity_id      TEXT,\n"
        "    analysis       TEXT,\n"
        "    artifact_id    INTEGER REFERENCES cohort_artifacts(artifact_id) "
        "ON DELETE CASCADE,\n"
        "    ref_key        TEXT NOT NULL,\n"
        "    version_used   TEXT NOT NULL,\n"
        "    recorded_at    TEXT NOT NULL,\n"
        "    transaction_id TEXT\n"
        ")"
    )


def reference_usage_indexes() -> list[str]:
    return [
        "CREATE UNIQUE INDEX idx_refusage_analysis ON reference_usage("
        "entity_level, entity_id, analysis, ref_key) WHERE scope = 'analysis'",
        "CREATE UNIQUE INDEX idx_refusage_cohort ON reference_usage("
        "artifact_id, ref_key) WHERE scope = 'cohort'",
        "CREATE INDEX idx_refusage_refkey ON reference_usage(ref_key)",
    ]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def reference_schema_exists(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "reference_artifacts") and _table_exists(
        conn, "reference_usage"
    )


def ensure_reference_schema(conn: sqlite3.Connection) -> list[str]:
    """Create any missing reference objects. Idempotent. Returns executed SQL."""
    executed: list[str] = []
    if not _table_exists(conn, "reference_artifacts"):
        ddl = reference_artifacts_ddl()
        conn.execute(ddl)
        executed.append(ddl)
    if not _table_exists(conn, "reference_usage"):
        ddl = reference_usage_ddl()
        conn.execute(ddl)
        executed.append(ddl)
    if executed:
        for idx in reference_usage_indexes():
            conn.execute(idx)
            executed.append(idx)
    return executed


__all__ = [
    "TIMESTAMP_FMT", "REFERENCE_KINDS",
    "reference_artifacts_ddl", "reference_usage_ddl", "reference_usage_indexes",
    "reference_schema_exists", "ensure_reference_schema",
]
