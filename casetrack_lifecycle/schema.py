"""DDL and migration helper for project lifecycle status (proposal 0007).

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import sqlite3
import sys

VALID_STATUSES = ("active", "complete", "archived")

# SQLite ALTER TABLE cannot add a column with a CHECK constraint directly,
# so we use a DEFAULT-only ADD COLUMN, then enforce the enum in Python.
_STATUS_ALTER = (
    "ALTER TABLE project_meta ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
)

# A real CHECK added to the column definition at table-creation time.
# This covers brand-new projects (init runs before migrate-status).
_STATUS_CHECK_UPDATE = (
    "UPDATE project_meta SET status = 'active' WHERE status IS NULL"
)


def migrate_status(conn: sqlite3.Connection) -> bool:
    """Add `status` column to project_meta if absent.  Idempotent.

    Returns True if the column was added, False if it already existed.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(project_meta)")}
    if "status" in cols:
        return False
    conn.execute(_STATUS_ALTER)
    conn.execute(_STATUS_CHECK_UPDATE)
    conn.commit()
    return True


def auto_migrate_if_needed(conn: sqlite3.Connection, project_dir=None) -> None:
    """Auto-migrate silently on open; warn to stderr if migration was needed."""
    added = migrate_status(conn)
    if added:
        loc = f" ({project_dir})" if project_dir else ""
        print(
            f"[casetrack] project_meta missing 'status' column — "
            f"auto-migrating to v0.7 schema{loc}",
            file=sys.stderr,
        )


def get_status(conn: sqlite3.Connection) -> str:
    """Return the project's current status string, defaulting to 'active'."""
    auto_migrate_if_needed(conn)
    row = conn.execute("SELECT status FROM project_meta LIMIT 1").fetchone()
    return row[0] if row else "active"


def set_status(conn: sqlite3.Connection, new_status: str) -> str:
    """Update project_meta.status; returns the previous status."""
    if new_status not in VALID_STATUSES:
        raise ValueError(
            f"status must be one of {VALID_STATUSES}, got {new_status!r}"
        )
    auto_migrate_if_needed(conn)
    old_row = conn.execute("SELECT status FROM project_meta LIMIT 1").fetchone()
    old_status = old_row[0] if old_row else "active"
    conn.execute("UPDATE project_meta SET status = ?", (new_status,))
    conn.commit()
    return old_status
