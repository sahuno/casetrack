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


class ReferenceError(Exception):
    """Raised when a reference operation violates an enforced invariant."""


@dataclass
class ReferenceArtifact:
    ref_key: str
    path: str
    version: str
    kind: str | None
    checksum: str | None
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def get_reference(conn: sqlite3.Connection, ref_key: str) -> "ReferenceArtifact | None":
    row = conn.execute(
        "SELECT ref_key, path, version, kind, checksum, updated_at "
        "FROM reference_artifacts WHERE ref_key = ?", (ref_key,)
    ).fetchone()
    return ReferenceArtifact(*row) if row else None


def list_references(conn: sqlite3.Connection) -> list["ReferenceArtifact"]:
    rows = conn.execute(
        "SELECT ref_key, path, version, kind, checksum, updated_at "
        "FROM reference_artifacts ORDER BY ref_key"
    ).fetchall()
    return [ReferenceArtifact(*r) for r in rows]


def sync_references_from_toml(conn: sqlite3.Connection, toml_refs: dict) -> list[dict]:
    """Make reference_artifacts match the TOML [references] block.

    Inserts new refs, updates changed path/version/kind/checksum. Returns the
    list of version changes ({ref_key, old_version, new_version}) so the caller
    can write reference_version_change provenance. Removal from TOML does NOT
    delete the row here (a usage row pointing at a removed ref must still read
    STALE — 0010 §6.1); removal handling is left to a future doctor check.
    """
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    changes: list[dict] = []
    for ref_key, spec in toml_refs.items():
        existing = get_reference(conn, ref_key)
        new_version = spec["version"]
        if existing is None:
            conn.execute(
                "INSERT INTO reference_artifacts "
                "(ref_key, path, version, kind, checksum, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ref_key, spec["path"], new_version, spec.get("kind"),
                 spec.get("checksum"), now),
            )
            changes.append({"ref_key": ref_key, "old_version": None,
                            "new_version": new_version})
        elif (existing.version != new_version or existing.path != spec["path"]
              or existing.kind != spec.get("kind")
              or existing.checksum != spec.get("checksum")):
            conn.execute(
                "UPDATE reference_artifacts SET path=?, version=?, kind=?, "
                "checksum=?, updated_at=? WHERE ref_key=?",
                (spec["path"], new_version, spec.get("kind"),
                 spec.get("checksum"), now, ref_key),
            )
            if existing.version != new_version:
                changes.append({"ref_key": ref_key,
                                "old_version": existing.version,
                                "new_version": new_version})
    return changes


def record_usage(conn: sqlite3.Connection, *, scope: str, ref_key: str,
                 version_used: str, transaction_id: str,
                 entity_level: str | None = None,
                 entity_id: str | None = None,
                 analysis: str | None = None,
                 artifact_id: int | None = None,
                 recorded_at: str | None = None) -> None:
    """Upsert one (output × ref) usage edge. Re-append overwrites version_used."""
    if recorded_at is None:
        recorded_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    if scope == "analysis":
        conn.execute(
            "DELETE FROM reference_usage WHERE scope='analysis' AND "
            "entity_level=? AND entity_id=? AND analysis=? AND ref_key=?",
            (entity_level, entity_id, analysis, ref_key),
        )
    elif scope == "cohort":
        conn.execute(
            "DELETE FROM reference_usage WHERE scope='cohort' AND "
            "artifact_id=? AND ref_key=?", (artifact_id, ref_key),
        )
    else:
        raise ReferenceError(f"unknown usage scope {scope!r}")
    conn.execute(
        "INSERT INTO reference_usage (scope, entity_level, entity_id, analysis, "
        "artifact_id, ref_key, version_used, recorded_at, transaction_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scope, entity_level, entity_id, analysis, artifact_id, ref_key,
         version_used, recorded_at, transaction_id),
    )


def _current_versions(conn) -> dict[str, str]:
    return {r.ref_key: r.version for r in list_references(conn)}


def output_staleness(conn, *, scope: str, entity_level: str | None = None,
                     entity_id: str | None = None, analysis: str | None = None,
                     artifact_id: int | None = None) -> dict:
    """Return {'state': fresh|STALE|untracked, 'reasons': [str]} for one output.

    STALE when any usage row's version_used != current canonical version, or the
    ref_key was removed from the canonical set. untracked when no usage rows.
    Derived purely at read time (0010 §6.2).
    """
    if scope == "analysis":
        rows = conn.execute(
            "SELECT ref_key, version_used FROM reference_usage WHERE "
            "scope='analysis' AND entity_level=? AND entity_id=? AND analysis=?",
            (entity_level, entity_id, analysis),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ref_key, version_used FROM reference_usage WHERE "
            "scope='cohort' AND artifact_id=?", (artifact_id,),
        ).fetchall()
    if not rows:
        return {"state": "untracked", "reasons": []}
    current = _current_versions(conn)
    reasons: list[str] = []
    for ref_key, version_used in rows:
        if ref_key not in current:
            reasons.append(f"reference removed: {ref_key}")
        elif current[ref_key] != version_used:
            reasons.append(f"{ref_key}: {version_used} -> {current[ref_key]}")
    return {"state": "STALE" if reasons else "fresh", "reasons": sorted(reasons)}


def all_stale_outputs(conn) -> list[dict]:
    """Every output with >=1 usage row, annotated with its staleness state.

    Used by the `references --stale-only` CLI and read paths.
    """
    out: list[dict] = []
    # analysis-scope outputs (distinct entity+analysis)
    for level, eid, analysis in conn.execute(
        "SELECT DISTINCT entity_level, entity_id, analysis FROM reference_usage "
        "WHERE scope='analysis'"
    ).fetchall():
        s = output_staleness(conn, scope="analysis", entity_level=level,
                             entity_id=eid, analysis=analysis)
        out.append({"scope": "analysis", "entity_level": level, "entity_id": eid,
                    "analysis": analysis, "artifact_id": None, **s})
    for (aid,) in conn.execute(
        "SELECT DISTINCT artifact_id FROM reference_usage WHERE scope='cohort'"
    ).fetchall():
        s = output_staleness(conn, scope="cohort", artifact_id=aid)
        out.append({"scope": "cohort", "entity_level": None, "entity_id": None,
                    "analysis": None, "artifact_id": aid, **s})
    return out


__all__ = [
    "TIMESTAMP_FMT", "REFERENCE_KINDS",
    "reference_artifacts_ddl", "reference_usage_ddl", "reference_usage_indexes",
    "reference_schema_exists", "ensure_reference_schema",
    "ReferenceError", "ReferenceArtifact",
    "get_reference", "list_references",
    "sync_references_from_toml", "record_usage",
    "output_staleness", "all_stale_outputs",
]
