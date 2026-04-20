"""Write-gate for archived projects (proposal 0007 §7).

Import `assert_not_archived` at the top of any mutation command.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from casetrack_lifecycle.schema import auto_migrate_if_needed


def assert_not_archived(
    project_dir: str | Path,
    *,
    force_archived: bool = False,
    yes: bool = False,
) -> None:
    """Exit 2 if the project is archived, unless --force-archived --yes is set.

    Opens a short-lived read connection — safe to call before the main
    command opens its own connection.
    """
    project_dir = Path(project_dir)
    db_path = project_dir / "casetrack.db"
    if not db_path.exists():
        return  # no DB yet → can't be archived

    conn = sqlite3.connect(str(db_path))
    try:
        auto_migrate_if_needed(conn, project_dir)
        row = conn.execute("SELECT status FROM project_meta LIMIT 1").fetchone()
    finally:
        conn.close()

    if row is None or row[0] != "archived":
        return  # active or complete — no gate

    if force_archived and yes:
        return  # explicit override — allow

    project_id = _read_project_id(project_dir)
    label = f"'{project_id}'" if project_id else str(project_dir)
    print(
        f"Error: project {label} is archived (status=archived).\n"
        f"       Mutation commands are refused on archived projects.\n"
        f"       To override:  add --force-archived --yes\n"
        f"       To unarchive: casetrack project set-status "
        f"--project-dir {project_dir} --status active",
        file=sys.stderr,
    )
    sys.exit(2)


def _read_project_id(project_dir: Path) -> str | None:
    db_path = project_dir / "casetrack.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT project_id FROM project_meta LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None
