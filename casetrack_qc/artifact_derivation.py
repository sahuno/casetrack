"""Artifact-to-artifact lineage: schema, node addressing, edges, and the
read-time transitive staleness walk.

Proposal 0011. A `derived-from` edge links any two lineage nodes (cohort
artifact / reference / sample-level output). Staleness is derived live: a node
is ``derived_stale`` when any node it derives from is itself stale by any cause
(0009 input-stale, 0010 ref-stale, or 0011 derived-stale — recursively).

Mirrors casetrack_qc/reference_artifacts.py: module functions take an open
conn, the command layer owns the transaction + provenance, all DDL idempotent.
Lives in casetrack_qc/ (NOT a new top-level package) because the name
`casetrack_lineage` is already proposal 0006's assay-merge subsystem.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass

TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S"
NODE_SCOPES = ("cohort", "reference", "analysis")


class DerivationError(Exception):
    """Raised when a derivation operation violates an enforced invariant."""


# ── schema ──────────────────────────────────────────────────────────────────

def artifact_derivation_ddl() -> str:
    return (
        "CREATE TABLE artifact_derivation (\n"
        "    derivation_id  INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    down_node      TEXT NOT NULL,\n"
        "    up_node        TEXT NOT NULL,\n"
        "    recorded_at    TEXT NOT NULL,\n"
        "    transaction_id TEXT\n"
        ")"
    )


def artifact_derivation_indexes() -> list[str]:
    return [
        "CREATE UNIQUE INDEX idx_deriv_edge ON artifact_derivation(down_node, up_node)",
        "CREATE INDEX idx_deriv_up ON artifact_derivation(up_node)",
        "CREATE INDEX idx_deriv_down ON artifact_derivation(down_node)",
    ]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def derivation_schema_exists(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "artifact_derivation")


def ensure_derivation_schema(conn: sqlite3.Connection) -> list[str]:
    """Create the artifact_derivation table + indexes. Idempotent."""
    if _table_exists(conn, "artifact_derivation"):
        return []
    executed = [artifact_derivation_ddl()]
    conn.execute(executed[0])
    for idx in artifact_derivation_indexes():
        conn.execute(idx)
        executed.append(idx)
    return executed


# ── node addressing ───────────────────────────────────────────────────────────

@dataclass
class LineageNode:
    scope: str
    analysis: str | None = None      # cohort + analysis scopes
    run_tag: str | None = None       # cohort
    ref_key: str | None = None       # reference
    entity_level: str | None = None  # analysis
    entity_id: str | None = None     # analysis

    @classmethod
    def parse(cls, s: str) -> "LineageNode":
        if not s or ":" not in s:
            raise DerivationError(f"malformed node-ref {s!r}")
        scope, _, payload = s.partition(":")
        if scope == "cohort":
            if "@" not in payload:
                raise DerivationError(f"cohort node-ref needs <analysis>@<run_tag>: {s!r}")
            analysis, _, run_tag = payload.partition("@")
            if not analysis or not run_tag:
                raise DerivationError(f"cohort node-ref needs <analysis>@<run_tag>: {s!r}")
            return cls(scope="cohort", analysis=analysis, run_tag=run_tag)
        if scope == "reference":
            if not payload:
                raise DerivationError(f"reference node-ref needs a ref_key: {s!r}")
            return cls(scope="reference", ref_key=payload)
        if scope == "analysis":
            parts = payload.split("/")
            if len(parts) != 3 or not all(parts):
                raise DerivationError(
                    f"analysis node-ref needs <level>/<entity_id>/<analysis>: {s!r}")
            return cls(scope="analysis", entity_level=parts[0],
                       entity_id=parts[1], analysis=parts[2])
        raise DerivationError(f"unknown node scope {scope!r} in {s!r}")

    def canonical(self) -> str:
        if self.scope == "cohort":
            return f"cohort:{self.analysis}@{self.run_tag}"
        if self.scope == "reference":
            return f"reference:{self.ref_key}"
        if self.scope == "analysis":
            return f"analysis:{self.entity_level}/{self.entity_id}/{self.analysis}"
        raise DerivationError(f"unknown node scope {self.scope!r}")


__all__ = [
    "TIMESTAMP_FMT", "NODE_SCOPES", "DerivationError",
    "artifact_derivation_ddl", "artifact_derivation_indexes",
    "derivation_schema_exists", "ensure_derivation_schema",
    "LineageNode",
]
