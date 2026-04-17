"""QC DDL, ``qc_status`` column migrations, and ``[qc]`` TOML parsing.

Proposal 0002 §4 and §5.3. All DDL is idempotent so ``ensure_qc_schema`` can
be called from both fresh ``casetrack init`` and from ``casetrack migrate-qc``
on an already-live v0.3 project.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

# ── Defaults ────────────────────────────────────────────────────────────────────

# Base + ONT-HGSOC additions (proposal §0 decision #12). Used as the DDL CHECK
# on qc_events.kind and as the fallback kind list when [qc] is absent from TOML.
DEFAULT_QC_KINDS: tuple[str, ...] = (
    "qc_fail",
    "qc_warn",
    "consent_revoked",
    "protocol_deviation",
    "superseded",
    "library_prep_failed",
    "basecall_accuracy_low",
    "contamination",
    "batch_effect_flagged",
    "sequencing_run_failed",
    "other",
)

DEFAULT_CONSENT_ENUM: tuple[str, ...] = (
    "consented",
    "consented_limited_use",
    "pending",
    "revoked",
    "withdrawn",
    "consent_expired",
    "deceased_pre_consent",
)

# Consent values that exclude a patient from default read paths.
# `consented_limited_use` is treated as usable by default (see §4.3 guidance).
CONSENT_EXCLUDED_DEFAULT: tuple[str, ...] = (
    "revoked",
    "withdrawn",
    "consent_expired",
    "deceased_pre_consent",
    "pending",
)

# Statuses that exclude an entity from default read paths.
DEFAULT_EXCLUDE_STATUSES: tuple[str, ...] = ("fail", "censored", "consent_revoked")

# Level restrictions per kind (CLI-enforced). Kinds not listed allow any level.
DEFAULT_QC_KIND_SCOPES: dict[str, tuple[str, ...]] = {
    "consent_revoked": ("patient",),
    "library_prep_failed": ("assay",),
    "basecall_accuracy_low": ("assay",),
    "sequencing_run_failed": ("assay",),
    "protocol_deviation": ("specimen", "assay"),
    "contamination": ("specimen", "assay"),
}

# qc_status per-level allowed values. patient tolerates consent_revoked; the
# other levels do not (consent cascades at read time, not by denormalization).
PATIENT_QC_STATUSES: tuple[str, ...] = ("pass", "warn", "fail", "censored", "consent_revoked")
CHILD_QC_STATUSES: tuple[str, ...] = ("pass", "warn", "fail", "censored")

# Valid `source` values for qc_events (proposal §4.1).
QC_EVENT_SOURCES: tuple[str, ...] = ("manual", "slurm", "import")


# ── DDL ─────────────────────────────────────────────────────────────────────────


def _quote_list(values: Iterable[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def qc_events_ddl(kinds: Iterable[str] = DEFAULT_QC_KINDS) -> str:
    """``CREATE TABLE qc_events`` DDL with a CHECK over ``kinds``.

    Teams extending the kind list via TOML would regenerate this via
    ``schema apply`` (deferred to a later phase); α writes the default
    superset.
    """
    return (
        "CREATE TABLE qc_events (\n"
        "    id              INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    level           TEXT NOT NULL CHECK(level IN ('patient','specimen','assay')),\n"
        "    entity_id       TEXT NOT NULL,\n"
        f"    kind            TEXT NOT NULL CHECK(kind IN ({_quote_list(kinds)})),\n"
        "    reason          TEXT NOT NULL,\n"
        f"    source          TEXT NOT NULL CHECK(source IN ({_quote_list(QC_EVENT_SOURCES)})),\n"
        "    created_at      TEXT NOT NULL,\n"
        "    created_by      TEXT NOT NULL,\n"
        "    resolved_at     TEXT,\n"
        "    resolved_by     TEXT,\n"
        "    resolved_reason TEXT,\n"
        "    transaction_id  TEXT NOT NULL\n"
        ")"
    )


def qc_events_indexes() -> list[str]:
    """Indexes required for fast lookup by entity and by active status."""
    return [
        "CREATE INDEX idx_qc_events_entity ON qc_events(level, entity_id)",
        "CREATE INDEX idx_qc_events_active ON qc_events(level, entity_id) "
        "WHERE resolved_at IS NULL",
        "CREATE INDEX idx_qc_events_kind ON qc_events(kind)",
    ]


def qc_status_alter(level: str) -> str:
    """``ALTER TABLE`` adding the ``qc_status`` fast-filter column for *level*."""
    table = f"{level}s"
    if level == "patient":
        statuses = PATIENT_QC_STATUSES
    else:
        statuses = CHILD_QC_STATUSES
    return (
        f'ALTER TABLE "{table}" ADD COLUMN qc_status TEXT '
        f"CHECK (qc_status IN ({_quote_list(statuses)})) "
        f"DEFAULT 'pass'"
    )


def consent_alters() -> list[str]:
    """``ALTER TABLE patients`` adding the three consent columns."""
    return [
        'ALTER TABLE "patients" ADD COLUMN consent_status TEXT '
        f"CHECK (consent_status IN ({_quote_list(DEFAULT_CONSENT_ENUM)})) "
        "DEFAULT 'consented'",
        'ALTER TABLE "patients" ADD COLUMN consent_date DATE',
        'ALTER TABLE "patients" ADD COLUMN withdrawal_date DATE',
    ]


# ── Table / column introspection ────────────────────────────────────────────────


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f'PRAGMA table_info("{table}")')
    return any(row[1] == column for row in cur.fetchall())


def qc_schema_exists(conn: sqlite3.Connection) -> bool:
    """True when the project already has the v0.4 QC schema in place."""
    if not _table_exists(conn, "qc_events"):
        return False
    for level in ("patient", "specimen", "assay"):
        if not _column_exists(conn, f"{level}s", "qc_status"):
            return False
    for col in ("consent_status", "consent_date", "withdrawal_date"):
        if not _column_exists(conn, "patients", col):
            return False
    return True


def ensure_qc_schema(
    conn: sqlite3.Connection,
    kinds: Iterable[str] = DEFAULT_QC_KINDS,
) -> list[str]:
    """Create any missing QC objects. Idempotent.

    Returns the list of SQL statements executed (empty if the schema was
    already fully in place). Caller is responsible for transaction management
    — typically wrapped by ``begin_immediate``.
    """
    executed: list[str] = []

    if not _table_exists(conn, "qc_events"):
        ddl = qc_events_ddl(kinds)
        conn.execute(ddl)
        executed.append(ddl)
        for idx_ddl in qc_events_indexes():
            conn.execute(idx_ddl)
            executed.append(idx_ddl)

    for level in ("patient", "specimen", "assay"):
        table = f"{level}s"
        if _table_exists(conn, table) and not _column_exists(conn, table, "qc_status"):
            stmt = qc_status_alter(level)
            conn.execute(stmt)
            executed.append(stmt)

    if _table_exists(conn, "patients"):
        if not _column_exists(conn, "patients", "consent_status"):
            stmt = consent_alters()[0]
            conn.execute(stmt)
            executed.append(stmt)
        if not _column_exists(conn, "patients", "consent_date"):
            stmt = consent_alters()[1]
            conn.execute(stmt)
            executed.append(stmt)
        if not _column_exists(conn, "patients", "withdrawal_date"):
            stmt = consent_alters()[2]
            conn.execute(stmt)
            executed.append(stmt)

    return executed


# ── TOML parsing ────────────────────────────────────────────────────────────────


def parse_qc_config(schema: dict | None) -> dict:
    """Extract [qc] / [qc.kind_scopes] from a parsed schema dict, with defaults.

    Accepts ``None`` (or a schema without a ``[qc]`` block) and returns the
    default configuration. Always returns a dict with the same shape so
    callers don't branch on presence.
    """
    base = {
        "kinds": list(DEFAULT_QC_KINDS),
        "default_source": "manual",
        "default_exclude": list(DEFAULT_EXCLUDE_STATUSES),
        "kind_scopes": {k: list(v) for k, v in DEFAULT_QC_KIND_SCOPES.items()},
    }
    if not schema or "qc" not in schema:
        return base
    qc = schema["qc"] or {}
    if "kinds" in qc and isinstance(qc["kinds"], list):
        base["kinds"] = [str(k) for k in qc["kinds"]]
    if "default_source" in qc and isinstance(qc["default_source"], str):
        base["default_source"] = qc["default_source"]
    if "default_exclude" in qc and isinstance(qc["default_exclude"], list):
        base["default_exclude"] = [str(s) for s in qc["default_exclude"]]
    scopes = qc.get("kind_scopes")
    if isinstance(scopes, dict):
        base["kind_scopes"] = {
            str(k): [str(x) for x in v] for k, v in scopes.items() if isinstance(v, list)
        }
    return base


DEFAULT_QC_TOML_BLOCK = """
[qc]
kinds = [
  "qc_fail",
  "qc_warn",
  "consent_revoked",
  "protocol_deviation",
  "superseded",
  "library_prep_failed",
  "basecall_accuracy_low",
  "contamination",
  "batch_effect_flagged",
  "sequencing_run_failed",
  "other",
]
default_source   = "manual"
default_exclude  = ["fail", "censored", "consent_revoked"]

[qc.kind_scopes]
consent_revoked        = ["patient"]
library_prep_failed    = ["assay"]
basecall_accuracy_low  = ["assay"]
sequencing_run_failed  = ["assay"]
protocol_deviation     = ["specimen", "assay"]
contamination          = ["specimen", "assay"]
"""


def write_qc_toml_block(toml_path: str | Path) -> bool:
    """Append the default ``[qc]`` block to *toml_path* if absent.

    Returns True when the block was added, False when the file already had one
    (lookup is textual — a ``[qc]`` or ``[qc.`` header anywhere marks it as
    present).
    """
    p = Path(toml_path)
    text = p.read_text()
    # Quick textual check — if any [qc] or [qc.*] header is present, skip.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[qc]") or stripped.startswith("[qc."):
            return False
    if not text.endswith("\n"):
        text += "\n"
    text += DEFAULT_QC_TOML_BLOCK
    p.write_text(text)
    return True
