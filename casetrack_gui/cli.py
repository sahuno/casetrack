"""Argparse wiring for ``casetrack gui`` — called by ``casetrack.main()``.

Mirrors ``casetrack_qc.cli`` so integration into ``casetrack.py`` is a
two-line change.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def build_gui_subparsers(subparsers) -> None:
    p = subparsers.add_parser(
        "gui",
        help="[v0.8] Start the operator web UI (FastAPI). Open at http://localhost:PORT/",
    )
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1 — laptop + ssh -L)")
    p.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    p.add_argument(
        "--registry",
        help="Override ~/.casetrack/registry.json (also honors $CASETRACK_REGISTRY)",
    )


def cmd_gui(args: argparse.Namespace) -> None:
    try:
        from casetrack_gui.app import serve
    except ImportError as e:
        raise SystemExit(
            f"casetrack gui requires the optional 'gui' extras: pip install -e '.[gui]'\n"
            f"(missing: {e.name})"
        ) from e

    registry = Path(args.registry) if getattr(args, "registry", None) else None
    if registry:
        os.environ["CASETRACK_REGISTRY"] = str(registry)
    serve(host=args.host, port=args.port, registry_path=registry)


def gui_command_dispatch() -> dict:
    return {"gui": cmd_gui}
