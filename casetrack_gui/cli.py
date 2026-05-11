"""Argparse wiring for ``casetrack gui`` — called by ``casetrack.main()``.

Mirrors ``casetrack_qc.cli`` so integration into ``casetrack.py`` is a
two-line change.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse
import os
import sys
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
    # ── auth ────────────────────────────────────────────────────────────────
    p.add_argument(
        "--username",
        help="Browser login username. Default: $CASETRACK_GUI_USER or 'operator'.",
    )
    p.add_argument(
        "--password-file",
        help="Path to a file whose first line is the password (preferred over "
             "$CASETRACK_GUI_PASSWORD — keeps the password out of `ps`).",
    )
    p.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable HTTP Basic auth. Only safe on a trusted host with no other "
             "users. By default the GUI refuses to start without credentials.",
    )


def _resolve_auth(args: argparse.Namespace):
    """Return (AuthConfig | None). None means the operator opted out."""
    from casetrack_gui.auth import AuthConfig

    if getattr(args, "no_auth", False):
        if args.host != "127.0.0.1":
            print(
                f"[casetrack gui] WARNING: --no-auth with --host {args.host} exposes the "
                f"GUI without authentication. Reachable from anyone who can connect to "
                f"port {args.port} on this host.",
                file=sys.stderr,
            )
        return None

    username = (
        getattr(args, "username", None)
        or os.environ.get("CASETRACK_GUI_USER")
        or "operator"
    )

    password: str | None = None
    pw_file = (
        getattr(args, "password_file", None)
        or os.environ.get("CASETRACK_GUI_PASSWORD_FILE")
    )
    if pw_file:
        path = Path(pw_file).expanduser()
        if not path.exists():
            raise SystemExit(f"[casetrack gui] --password-file not found: {path}")
        password = path.read_text().splitlines()[0].strip() if path.read_text().strip() else ""
        if not password:
            raise SystemExit(f"[casetrack gui] --password-file is empty: {path}")
    else:
        password = os.environ.get("CASETRACK_GUI_PASSWORD")

    if not password:
        raise SystemExit(
            "[casetrack gui] No password configured. Set one of:\n"
            "  • CASETRACK_GUI_PASSWORD=<your_pw> casetrack gui ...\n"
            "  • casetrack gui --password-file ~/.casetrack/gui_password\n"
            "  • casetrack gui --no-auth   (only on a trusted single-user host)\n"
            f"Username defaults to {username!r} (override with --username or $CASETRACK_GUI_USER)."
        )

    return AuthConfig(username=username, password=password)


def cmd_gui(args: argparse.Namespace) -> None:
    try:
        from casetrack_gui.app import serve
    except ImportError as e:
        raise SystemExit(
            f"casetrack gui requires the optional 'gui' extras: pip install -e '.[gui]'\n"
            f"(missing: {e.name})"
        ) from e

    auth_config = _resolve_auth(args)

    registry = Path(args.registry) if getattr(args, "registry", None) else None
    if registry:
        os.environ["CASETRACK_REGISTRY"] = str(registry)

    if auth_config is not None:
        print(
            f"[casetrack gui] HTTP Basic auth enabled — username={auth_config.username!r}. "
            f"Open http://{args.host}:{args.port}/ in your browser.",
            file=sys.stderr,
        )
    serve(host=args.host, port=args.port, registry_path=registry, auth_config=auth_config)


def build_snapshot_subparsers(subparsers) -> None:
    p = subparsers.add_parser(
        "snapshot",
        help="[v0.8] Export a self-contained HTML snapshot of a project for PI consumption.",
    )
    p.add_argument(
        "--project-id",
        required=True,
        help="Project ID (must be registered in ~/.casetrack/registry.json).",
    )
    p.add_argument(
        "--output",
        required=True,
        help="Directory to write snapshot files into (created if absent).",
    )
    p.add_argument(
        "--project-dir",
        help="Override registry lookup — use this project directory directly.",
    )


def cmd_snapshot(args: argparse.Namespace) -> None:
    try:
        from casetrack_gui.snapshot import cmd_snapshot as _cmd_snapshot
    except ImportError as e:
        raise SystemExit(
            f"casetrack snapshot requires the optional 'gui' extras: pip install -e '.[gui]'\n"
            f"(missing: {getattr(e, 'name', e)})"
        ) from e
    _cmd_snapshot(args)


def gui_command_dispatch() -> dict:
    return {"gui": cmd_gui, "snapshot": cmd_snapshot}
