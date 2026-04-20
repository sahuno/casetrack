"""CLI command implementations for project lifecycle status (proposal 0007).

Commands:
  casetrack project set-status --project-dir <path> --status <s> [--reason <r>]
  casetrack project status     --project-dir <path>

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from casetrack_lifecycle.schema import (
    VALID_STATUSES,
    auto_migrate_if_needed,
    get_status,
    set_status,
)


def cmd_project_set_status(args) -> None:
    """`casetrack project set-status` — change a project's lifecycle status."""
    project_dir = Path(args.project_dir).resolve()
    db_path = project_dir / "casetrack.db"

    if not project_dir.is_dir():
        print(f"Error: project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)
    if not db_path.exists():
        print(
            f"Error: casetrack.db not found in {project_dir}. "
            "Run `casetrack init` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    new_status = args.status
    if new_status not in VALID_STATUSES:
        print(
            f"Error: --status must be one of {list(VALID_STATUSES)}, "
            f"got {new_status!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    reason = getattr(args, "reason", None) or ""

    conn = sqlite3.connect(str(db_path))
    try:
        auto_migrate_if_needed(conn, project_dir)
        old_status = set_status(conn, new_status)
        project_id = _read_project_id(conn)
    finally:
        conn.close()

    # Provenance
    import casetrack as _ct
    _ct.log_project_provenance(project_dir, {
        "action": "project_status_change",
        "project_id": project_id,
        "from_status": old_status,
        "to_status": new_status,
        "reason": reason,
        "casetrack_version": _ct._CASETRACK_VERSION,
    })

    label = project_id or str(project_dir)
    print(
        f"project {label}: status {old_status!r} → {new_status!r}"
        + (f" ({reason})" if reason else "")
    )


def cmd_project_status(args) -> None:
    """`casetrack project status` — display a project's lifecycle status."""
    project_dir = Path(args.project_dir).resolve()
    db_path = project_dir / "casetrack.db"

    if not project_dir.is_dir():
        print(f"Error: project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)
    if not db_path.exists():
        print(
            f"Error: casetrack.db not found in {project_dir}.",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        auto_migrate_if_needed(conn, project_dir)
        status = get_status(conn)
        project_id = _read_project_id(conn)
        project_name = _read_project_name(conn)
    finally:
        conn.close()

    last_change = _last_status_change(project_dir)

    print(f"project:    {project_id or project_dir.name}")
    if project_name and project_name != project_id:
        print(f"name:       {project_name}")
    print(f"status:     {status}")
    if last_change:
        print(f"changed:    {last_change['timestamp']}  (by {last_change.get('user', '?')})")
        if last_change.get("reason"):
            print(f"reason:     {last_change['reason']}")


# ── helpers ────────────────────────────────────────────────────────────────────

def _read_project_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT project_id FROM project_meta LIMIT 1").fetchone()
    return row[0] if row else None


def _read_project_name(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT name FROM project_meta LIMIT 1").fetchone()
    return row[0] if row else None


def _last_status_change(project_dir: Path) -> dict | None:
    """Read the most recent project_status_change entry from provenance.jsonl."""
    import json
    prov_path = project_dir / "provenance.jsonl"
    if not prov_path.exists():
        return None
    last = None
    try:
        with open(prov_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("action") == "project_status_change":
                    last = entry
    except OSError:
        pass
    return last
