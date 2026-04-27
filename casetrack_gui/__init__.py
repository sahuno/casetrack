"""casetrack_gui — operator GUI for casetrack (committed framing 2026-04-27).

Live FastAPI + HTMX + Jinja2 over `casetrack.db`, with a "publish snapshot"
button that emits self-contained static HTML for PI consumption. Mutations
(censor, uncensor, append, add-metadata) shell out to the `casetrack` CLI
so the SQLite WAL writer is never raced and provenance.jsonl + qc_events
invariants stay single-headed.

Public API:
- :func:`create_app` — FastAPI factory used by the `casetrack gui` CLI and tests.
- :func:`build_gui_subparsers` / :func:`gui_command_dispatch` — argparse wiring
  helpers consumed by `casetrack.main()`.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

from casetrack_gui.app import create_app
from casetrack_gui.cli import build_gui_subparsers, gui_command_dispatch

__all__ = [
    "build_gui_subparsers",
    "create_app",
    "gui_command_dispatch",
]
