"""Cohort-level artifacts: schema, CRUD, and read-time staleness.

Proposal 0009. A cohort artifact is a single analysis output derived from many
assays — a joint-genotyped VCF, a panel-of-normals, a cohort PCA. It does not
fit the three-level biological hierarchy (derived, many-to-many, dynamically
membered — see proposal 0009 §7), so it lives in additive sibling tables that
mirror the ``qc_events`` pattern:

- ``cohort_artifacts``        — one row per cohort-level output, keyed by
  ``(analysis, run_tag)``.
- ``cohort_artifact_inputs``  — many-to-many lineage to contributing assays.

All DDL is idempotent so ``ensure_cohort_artifacts_schema`` can be called from a
fresh ``casetrack init`` and from ``casetrack migrate-cohort`` on a live project.
Nothing here writes provenance — the command layer does that so both rows land
in the same transaction (same contract as ``casetrack_qc.events``).

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import datetime
import sqlite3
from dataclasses import asdict, dataclass
from typing import Iterable

from casetrack_qc import reader

TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S"


# ── DDL ───────────────────────────────────────────────────────────────────────


def cohort_artifacts_ddl() -> str:
    return (
        "CREATE TABLE cohort_artifacts (\n"
        "    artifact_id    INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    analysis       TEXT NOT NULL,\n"
        "    run_tag        TEXT NOT NULL,\n"
        "    path           TEXT NOT NULL,\n"
        "    checksum       TEXT,\n"
        "    n_inputs       INTEGER NOT NULL,\n"
        "    stats_json     TEXT,\n"
        "    region_scope   TEXT,\n"
        "    created_at     TEXT NOT NULL,\n"
        "    created_by     TEXT,\n"
        "    transaction_id TEXT NOT NULL,\n"
        "    UNIQUE (analysis, run_tag)\n"
        ")"
    )


def cohort_artifact_inputs_ddl() -> str:
    return (
        "CREATE TABLE cohort_artifact_inputs (\n"
        "    artifact_id INTEGER NOT NULL "
        "REFERENCES cohort_artifacts(artifact_id) ON DELETE CASCADE,\n"
        "    assay_id    TEXT NOT NULL REFERENCES assays(assay_id) ON DELETE RESTRICT,\n"
        "    role        TEXT,\n"
        "    PRIMARY KEY (artifact_id, assay_id)\n"
        ")"
    )


def cohort_artifacts_indexes() -> list[str]:
    return [
        "CREATE INDEX idx_cohort_artifacts_key "
        "ON cohort_artifacts(analysis, run_tag)",
        "CREATE INDEX idx_cohort_artifact_inputs_assay "
        "ON cohort_artifact_inputs(assay_id)",
    ]


# ── Introspection ─────────────────────────────────────────────────────────────


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f'PRAGMA table_info("{table}")')
    return any(row[1] == column for row in cur.fetchall())


def cohort_artifacts_schema_exists(conn: sqlite3.Connection) -> bool:
    """True when both cohort-artifact tables are present."""
    return _table_exists(conn, "cohort_artifacts") and _table_exists(
        conn, "cohort_artifact_inputs"
    )


def ensure_cohort_artifacts_schema(conn: sqlite3.Connection) -> list[str]:
    """Create any missing cohort-artifact objects. Idempotent.

    Returns the list of SQL statements executed (empty when already present).
    Caller manages the transaction (typically via ``begin_immediate``).
    """
    executed: list[str] = []
    if not _table_exists(conn, "cohort_artifacts"):
        ddl = cohort_artifacts_ddl()
        conn.execute(ddl)
        executed.append(ddl)
    if not _table_exists(conn, "cohort_artifact_inputs"):
        ddl = cohort_artifact_inputs_ddl()
        conn.execute(ddl)
        executed.append(ddl)
    if executed:
        for idx_ddl in cohort_artifacts_indexes():
            conn.execute(idx_ddl)
            executed.append(idx_ddl)
    executed.extend(ensure_region_scope_columns(conn))
    return executed


def ensure_region_scope_columns(conn: sqlite3.Connection) -> list[str]:
    """Add the proposal-0013 columns to existing 0009 tables. Idempotent.

    ``region_scope`` on ``cohort_artifacts`` and ``role`` on
    ``cohort_artifact_inputs``. No-op (returns ``[]``) when the tables are
    absent (pre-0009) or the columns already exist (fresh DDL or already
    migrated). Caller owns the transaction.
    """
    executed: list[str] = []
    if _table_exists(conn, "cohort_artifacts") and not _column_exists(
        conn, "cohort_artifacts", "region_scope"
    ):
        sql = "ALTER TABLE cohort_artifacts ADD COLUMN region_scope TEXT"
        conn.execute(sql)
        executed.append(sql)
    if _table_exists(conn, "cohort_artifact_inputs") and not _column_exists(
        conn, "cohort_artifact_inputs", "role"
    ):
        sql = "ALTER TABLE cohort_artifact_inputs ADD COLUMN role TEXT"
        conn.execute(sql)
        executed.append(sql)
    # after adding the region_scope column, create the grouping index:
    if executed and _table_exists(conn, "cohort_artifacts"):
        idx = (
            "CREATE INDEX IF NOT EXISTS idx_cohort_artifacts_scope "
            "ON cohort_artifacts(region_scope)"
        )
        conn.execute(idx)
        executed.append(idx)
    return executed


# ── Dataclass ─────────────────────────────────────────────────────────────────


class CohortArtifactError(Exception):
    """Raised when a cohort-artifact operation violates an enforced invariant."""


@dataclass
class CohortArtifact:
    artifact_id: int
    analysis: str
    run_tag: str
    path: str
    checksum: str | None
    n_inputs: int
    stats_json: str | None
    created_at: str
    created_by: str | None
    transaction_id: str

    def to_dict(self) -> dict:
        return asdict(self)


_ARTIFACT_COLS = (
    "artifact_id, analysis, run_tag, path, checksum, n_inputs, "
    "stats_json, created_at, created_by, transaction_id"
)


def _row_to_artifact(row: tuple) -> CohortArtifact:
    return CohortArtifact(*row)


# ── CRUD ──────────────────────────────────────────────────────────────────────


def insert_artifact(
    conn: sqlite3.Connection,
    *,
    analysis: str,
    run_tag: str,
    path: str,
    n_inputs: int,
    transaction_id: str,
    checksum: str | None = None,
    stats_json: str | None = None,
    created_by: str | None = None,
    created_at: str | None = None,
) -> int:
    """Insert a cohort artifact. Returns the new ``artifact_id``.

    Refuses a duplicate ``(analysis, run_tag)`` with a friendly error rather
    than letting the UNIQUE constraint surface as a raw ``IntegrityError`` —
    a re-genotyping run must use a new ``run_tag`` (proposal 0009 §8.2).
    """
    if get_artifact_by_key(conn, analysis, run_tag) is not None:
        raise CohortArtifactError(
            f"cohort artifact already exists for analysis={analysis!r} "
            f"run_tag={run_tag!r}; use a distinct run_tag for a new run"
        )
    if created_at is None:
        created_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    cur = conn.execute(
        """
        INSERT INTO cohort_artifacts
            (analysis, run_tag, path, checksum, n_inputs, stats_json,
             created_at, created_by, transaction_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (analysis, run_tag, path, checksum, n_inputs, stats_json,
         created_at, created_by, transaction_id),
    )
    return cur.lastrowid


def add_artifact_inputs(
    conn: sqlite3.Connection, artifact_id: int, assay_ids: Iterable[str]
) -> int:
    """Link contributing assays to an artifact. Returns the number inserted.

    Validates every assay exists first so a typo surfaces as a clear error
    instead of a raw FK ``IntegrityError`` mid-loop.
    """
    ids = list(assay_ids)
    for assay_id in ids:
        row = conn.execute(
            "SELECT 1 FROM assays WHERE assay_id = ?", (assay_id,)
        ).fetchone()
        if row is None:
            raise CohortArtifactError(f"unknown assay {assay_id!r}")
    conn.executemany(
        "INSERT OR IGNORE INTO cohort_artifact_inputs (artifact_id, assay_id) "
        "VALUES (?, ?)",
        [(artifact_id, a) for a in ids],
    )
    return len(ids)


def get_artifact(conn: sqlite3.Connection, artifact_id: int) -> CohortArtifact | None:
    row = conn.execute(
        f"SELECT {_ARTIFACT_COLS} FROM cohort_artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    return _row_to_artifact(row) if row else None


def get_artifact_by_key(
    conn: sqlite3.Connection, analysis: str, run_tag: str
) -> CohortArtifact | None:
    row = conn.execute(
        f"SELECT {_ARTIFACT_COLS} FROM cohort_artifacts "
        "WHERE analysis = ? AND run_tag = ?",
        (analysis, run_tag),
    ).fetchone()
    return _row_to_artifact(row) if row else None


def list_artifacts(conn: sqlite3.Connection) -> list[CohortArtifact]:
    rows = conn.execute(
        f"SELECT {_ARTIFACT_COLS} FROM cohort_artifacts ORDER BY artifact_id"
    ).fetchall()
    return [_row_to_artifact(r) for r in rows]


def artifact_inputs(conn: sqlite3.Connection, artifact_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT assay_id FROM cohort_artifact_inputs "
        "WHERE artifact_id = ? ORDER BY assay_id",
        (artifact_id,),
    ).fetchall()
    return [r[0] for r in rows]


# ── Read-time staleness (proposal 0009 §6.2) ────────────────────────────────


def artifact_staleness(
    conn: sqlite3.Connection,
    *,
    include_censored: bool = False,
    include_consent_revoked: bool = False,
) -> dict[int, list[str]]:
    """Map every artifact_id → sorted list of its non-active input assays.

    An empty list means the artifact is fresh. A non-empty list means one or
    more contributing assays are currently excluded by the QC / consent cascade
    (proposal 0002 §4.4), so the artifact is **stale** — its inputs no longer
    all pass. Derived purely at read time from the existing cascade, so it
    tracks censor / uncensor automatically with no stored flag.

    ``include_censored`` / ``include_consent_revoked`` mirror the read-path
    flags: when set, those exclusions don't count toward staleness.
    """
    active = reader.active_assay_ids(
        conn,
        include_censored=include_censored,
        include_consent_revoked=include_consent_revoked,
    )
    out: dict[int, list[str]] = {}
    for (artifact_id,) in conn.execute(
        "SELECT artifact_id FROM cohort_artifacts"
    ).fetchall():
        inputs = artifact_inputs(conn, artifact_id)
        out[artifact_id] = sorted(a for a in inputs if a not in active)
    return out


__all__ = [
    "TIMESTAMP_FMT",
    "CohortArtifact",
    "CohortArtifactError",
    "cohort_artifacts_ddl",
    "cohort_artifact_inputs_ddl",
    "cohort_artifacts_indexes",
    "cohort_artifacts_schema_exists",
    "ensure_cohort_artifacts_schema",
    "ensure_region_scope_columns",
    "insert_artifact",
    "add_artifact_inputs",
    "get_artifact",
    "get_artifact_by_key",
    "list_artifacts",
    "artifact_inputs",
    "artifact_staleness",
]
