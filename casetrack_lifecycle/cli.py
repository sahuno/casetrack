"""Argparse wiring for the lifecycle subsystem — called by ``casetrack.main()``.

Mirrors the pattern in ``casetrack_lineage.cli``.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

from casetrack_lifecycle.lifecycle import cmd_project_set_status, cmd_project_status
from casetrack_lifecycle.schema import VALID_STATUSES


def build_lifecycle_subparsers(subparsers) -> None:
    """Add ``project`` sub-dispatcher and ``migrate-status`` to *subparsers*."""

    # ── project <action> ──
    p_proj = subparsers.add_parser(
        "project",
        help="[v0.7] Project lifecycle commands: set-status, status",
    )
    proj_sub = p_proj.add_subparsers(dest="project_action")

    # project set-status
    p_ss = proj_sub.add_parser(
        "set-status",
        help="Change a project's lifecycle status (active | complete | archived)",
    )
    p_ss.add_argument("--project-dir", required=True,
                      help="Casetrack project directory")
    p_ss.add_argument(
        "--status", required=True,
        choices=list(VALID_STATUSES),
        help="New status value",
    )
    p_ss.add_argument(
        "--reason",
        help="Optional free-text note logged to provenance.jsonl",
    )

    # project status
    p_st = proj_sub.add_parser(
        "status",
        help="Display a project's lifecycle status",
    )
    p_st.add_argument("--project-dir", required=True,
                      help="Casetrack project directory")

    # ── migrate-status ──
    p_ms = subparsers.add_parser(
        "migrate-status",
        help="[v0.7] Add status column to project_meta (idempotent)",
    )
    p_ms.add_argument("--project-dir", required=True,
                      help="Casetrack project directory")


def lifecycle_command_dispatch() -> dict:
    """Command-name → function map that ``casetrack.main()`` merges into its own."""
    return {
        "project": cmd_project_dispatch,
        "migrate-status": cmd_migrate_status,
    }


def cmd_project_dispatch(args) -> None:
    """Dispatch ``casetrack project <action>``."""
    action = getattr(args, "project_action", None)
    if action == "set-status":
        cmd_project_set_status(args)
    elif action == "status":
        cmd_project_status(args)
    else:
        import sys
        print(
            "Error: `casetrack project` requires a subaction.\n"
            "  casetrack project set-status --project-dir ... --status <s>\n"
            "  casetrack project status     --project-dir ...",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_migrate_status(args) -> None:
    """`casetrack migrate-status` — idempotent DDL migration."""
    import sqlite3
    import sys
    from pathlib import Path
    from casetrack_lifecycle.schema import migrate_status

    project_dir = Path(args.project_dir).resolve()
    db_path = project_dir / "casetrack.db"
    if not db_path.exists():
        print(f"Error: casetrack.db not found in {project_dir}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    try:
        added = migrate_status(conn)
    finally:
        conn.close()
    if added:
        print(f"Migration complete: added 'status' column to project_meta in {db_path}")
    else:
        print(f"Already up-to-date: 'status' column present in {db_path}")


__all__ = [
    "build_lifecycle_subparsers",
    "lifecycle_command_dispatch",
]
