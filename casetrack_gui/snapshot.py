"""Static snapshot exporter — renders the GUI page set to self-contained HTML.

Output directory layout::

    index.html              project home (heatmap + queue + active events)
    qc.html                 QC event log (read-only, no forms)
    patient_{pid}.html      one per patient
    static/casetrack.css    copy of the live stylesheet

Internal links are rewritten to relative references so the output directory
opens offline in a browser.  POST forms (censor / uncensor) are suppressed
via the ``snapshot=True`` template context variable.

CLI entry-point: ``casetrack snapshot --project-id <id> --output <dir>``

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _jinja_env():
    from jinja2 import Environment, FileSystemLoader
    return Environment(
        loader=FileSystemLoader(str(_HERE / "templates")),
        autoescape=True,
    )


def _registry_path() -> Path:
    override = os.environ.get("CASETRACK_REGISTRY")
    if override:
        return Path(override)
    return Path.home() / ".casetrack" / "registry.json"


def resolve_project_dir(project_id: str) -> Path:
    """Look up *project_id* in the registry and return its project directory."""
    p = _registry_path()
    if not p.exists():
        raise SystemExit(f"[casetrack snapshot] Registry not found: {p}")
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[casetrack snapshot] Registry parse error: {exc}") from exc
    projects = data.get("projects", {}) if isinstance(data, dict) else {}
    entry = projects.get(project_id)
    if not entry:
        raise SystemExit(f"[casetrack snapshot] project_id {project_id!r} not in registry {p}")
    project_dir = Path(entry["path"])
    if not (project_dir / "casetrack.db").exists():
        raise SystemExit(f"[casetrack snapshot] casetrack.db not found under {project_dir}")
    return project_dir


def _connect_ro(project_dir: Path) -> sqlite3.Connection:
    db = project_dir / "casetrack.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rewrite_urls(html: str, project_id: str) -> str:
    """Rewrite absolute server URLs to relative paths for offline use."""
    # Static assets — must come first so the path prefix isn't re-matched below.
    html = html.replace('href="/static/', 'href="static/')
    html = html.replace('src="/static/', 'src="static/')
    # Per-patient pages (pattern before project-home rewrite to avoid clobbering).
    html = re.sub(
        r'href="/p/' + re.escape(project_id) + r'/patient/([^"]+)"',
        r'href="patient_\1.html"',
        html,
    )
    # QC log page.
    html = html.replace(f'href="/p/{project_id}/qc"', 'href="qc.html"')
    # Project home (used in breadcrumbs on patient / qc pages).
    html = html.replace(f'href="/p/{project_id}"', 'href="index.html"')
    # Project picker root — not included in snapshot scope.
    html = html.replace('href="/"', 'href="#"')
    return html


def render_snapshot(project_id: str, project_dir: Path, output_dir: Path) -> list[Path]:
    """Render the full snapshot page set into *output_dir*.

    Returns the list of written file paths.  Calls the same data helpers and
    Jinja2 templates as the live GUI — no logic is duplicated.
    """
    from casetrack_gui import heatmap as heatmap_mod
    from casetrack_gui.app import _patient_drill, _project_summary, _qc_events
    from casetrack_gui.introspect import introspect

    output_dir.mkdir(parents=True, exist_ok=True)
    static_out = output_dir / "static"
    static_out.mkdir(exist_ok=True)

    env = _jinja_env()
    written: list[Path] = []

    def _render(template_name: str, **ctx) -> str:
        ctx["snapshot"] = True
        ctx["request"] = None
        ctx["project_id"] = project_id
        return _rewrite_urls(env.get_template(template_name).render(**ctx), project_id)

    conn = _connect_ro(project_dir)
    try:
        shape = introspect(conn)
        summary = _project_summary(conn)
        heatmap = heatmap_mod.build(conn, shape)
        queue = heatmap_mod.next_up(conn, shape)
        events = _qc_events(conn)

        # index.html — project home
        p = output_dir / "index.html"
        p.write_text(
            _render(
                "project_home.html",
                project_dir=str(project_dir),
                shape=shape,
                summary=summary,
                heatmap=heatmap,
                queue=queue,
                events=events[:20],
            ),
            encoding="utf-8",
        )
        written.append(p)

        # qc.html — QC event log (forms suppressed via snapshot=True)
        p = output_dir / "qc.html"
        p.write_text(
            _render(
                "qc_log.html",
                project_dir=str(project_dir),
                events=events,
                summary=summary,
            ),
            encoding="utf-8",
        )
        written.append(p)

        # patient_{pid}.html — per-patient drill-down (mutation buttons suppressed)
        try:
            patient_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT patient_id FROM patients ORDER BY patient_id"
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            patient_ids = []

        for pid in patient_ids:
            data = _patient_drill(conn, pid)
            p = output_dir / f"patient_{pid}.html"
            p.write_text(_render("patient.html", shape=shape, **data), encoding="utf-8")
            written.append(p)

    finally:
        conn.close()

    # Static assets
    css_src = _HERE / "static" / "casetrack.css"
    if css_src.exists():
        css_dst = static_out / "casetrack.css"
        shutil.copy2(css_src, css_dst)
        written.append(css_dst)

    return written


def cmd_snapshot(args) -> None:
    """CLI entry-point for ``casetrack snapshot``."""
    try:
        import jinja2  # noqa: F401
        from casetrack_gui import heatmap as _  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            f"casetrack snapshot requires the optional 'gui' extras:\n"
            f"  pip install -e '.[gui]'\n"
            f"(missing: {getattr(exc, 'name', exc)})"
        ) from exc

    project_id: str = args.project_id
    output_dir = Path(args.output).expanduser().resolve()

    if getattr(args, "project_dir", None):
        project_dir = Path(args.project_dir).expanduser().resolve()
        if not (project_dir / "casetrack.db").exists():
            raise SystemExit(
                f"[casetrack snapshot] casetrack.db not found under {project_dir}"
            )
    else:
        project_dir = resolve_project_dir(project_id)

    written = render_snapshot(project_id, project_dir, output_dir)
    n_html = sum(1 for p in written if p.suffix == ".html")
    n_other = len(written) - n_html
    print(
        f"[casetrack snapshot] wrote {n_html} HTML page(s) + {n_other} asset(s) "
        f"→ {output_dir}"
    )
    print(f"  open {output_dir / 'index.html'}")
