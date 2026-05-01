"""FastAPI app — operator GUI for casetrack.

Routes (committed framing 2026-04-27, see project_gui_design_starting_point.md):

    GET  /                          project picker (reads ~/.casetrack/registry.json)
    GET  /p/{project_id}            project home — heatmap + next-up queue
    GET  /p/{project_id}/patient/{pid}   drill-down: patient → specimens → assays
    GET  /p/{project_id}/qc         QC event log
    POST /p/{project_id}/censor     subprocess to `casetrack censor ...`
    POST /p/{project_id}/uncensor   subprocess to `casetrack uncensor ...`
    GET  /healthz                   liveness probe

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from casetrack_gui import heatmap as heatmap_mod
from casetrack_gui import mutations
from casetrack_gui.auth import AuthConfig
from casetrack_gui.introspect import introspect


_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_STATIC = _HERE / "static"


def _registry_path() -> Path:
    override = os.environ.get("CASETRACK_REGISTRY")
    if override:
        return Path(override)
    return Path.home() / ".casetrack" / "registry.json"


def _load_registry() -> dict[str, dict]:
    p = _registry_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}
    return data.get("projects", {}) if isinstance(data, dict) else {}


def _resolve_project(project_id: str) -> Path:
    projects = _load_registry()
    entry = projects.get(project_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"project_id {project_id!r} not in registry")
    project_dir = Path(entry["path"])
    db = project_dir / "casetrack.db"
    if not db.exists():
        raise HTTPException(status_code=404, detail=f"casetrack.db missing under {project_dir}")
    return project_dir


def _connect(project_dir: Path) -> sqlite3.Connection:
    db = project_dir / "casetrack.db"
    # read-only via URI to avoid any accidental writes from the web layer.
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _project_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        meta = conn.execute("SELECT * FROM project_meta LIMIT 1").fetchone()
        out["meta"] = dict(meta) if meta else {}
    except sqlite3.OperationalError:
        out["meta"] = {}
    counts: dict[str, int] = {}
    for tbl in ("patients", "specimens", "assays"):
        try:
            counts[tbl] = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[tbl] = 0
    out["counts"] = counts
    qc_summary = {"fail": 0, "warn": 0, "censored": 0, "total_events": 0}
    try:
        rows = conn.execute(
            "SELECT kind, count(*) FROM qc_events WHERE resolved_at IS NULL GROUP BY kind"
        ).fetchall()
        for kind, n in rows:
            qc_summary["total_events"] += n
            if kind in ("qc_fail",):
                qc_summary["fail"] += n
            elif kind in ("qc_warn",):
                qc_summary["warn"] += n
            else:
                qc_summary["censored"] += n
    except sqlite3.OperationalError:
        pass
    out["qc"] = qc_summary
    return out


def _qc_events(conn: sqlite3.Connection, *, entity_id: str | None = None) -> list[dict]:
    cols = "id, level, entity_id, kind, reason, source, created_at, created_by, resolved_at, resolved_by, resolved_reason"
    try:
        if entity_id:
            sql = f"SELECT {cols} FROM qc_events WHERE entity_id = ? ORDER BY id DESC"
            rows = conn.execute(sql, (entity_id,)).fetchall()
        else:
            sql = f"SELECT {cols} FROM qc_events ORDER BY id DESC LIMIT 200"
            rows = conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def _patient_drill(conn: sqlite3.Connection, patient_id: str) -> dict:
    """Build the patient drill-down: patient row + its specimens + their assays."""
    try:
        p = conn.execute("SELECT * FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
    except sqlite3.OperationalError:
        p = None
    if p is None:
        raise HTTPException(status_code=404, detail=f"patient {patient_id!r} not found")
    specimens = []
    try:
        spec_rows = conn.execute(
            "SELECT * FROM specimens WHERE patient_id = ? ORDER BY specimen_id", (patient_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        spec_rows = []
    for sp in spec_rows:
        sp_d = dict(sp)
        try:
            assay_rows = conn.execute(
                "SELECT * FROM assays WHERE specimen_id = ? ORDER BY assay_id", (sp_d["specimen_id"],)
            ).fetchall()
        except sqlite3.OperationalError:
            assay_rows = []
        sp_d["assays"] = [dict(a) for a in assay_rows]
        sp_d["qc_events"] = _qc_events(conn, entity_id=sp_d["specimen_id"])
        for a in sp_d["assays"]:
            a["qc_events"] = _qc_events(conn, entity_id=a["assay_id"])
        specimens.append(sp_d)
    return {
        "patient": dict(p),
        "specimens": specimens,
        "qc_events": _qc_events(conn, entity_id=patient_id),
    }


def create_app(
    *,
    registry_path_override: Path | None = None,
    auth_config: AuthConfig | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    ``registry_path_override`` and ``auth_config`` are both optional —
    tests pass ``auth_config=None`` to skip the basic-auth gate; the CLI
    refuses to start without credentials unless the operator passes
    ``--no-auth``.
    """
    app = FastAPI(title="casetrack GUI", docs_url=None, redoc_url=None)
    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    if registry_path_override is not None:
        # Override env var so _registry_path() picks it up. Survives the lifetime
        # of the process — tests use TestClient in-process which is fine.
        os.environ["CASETRACK_REGISTRY"] = str(registry_path_override)

    auth_deps: list = []
    if auth_config is not None:
        auth_deps = [Depends(auth_config.make_dependency())]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse, dependencies=auth_deps)
    def projects_index(request: Request) -> Any:
        projects = _load_registry()
        return _TEMPLATES.TemplateResponse(
            request, "projects.html",
            {"projects": projects},
        )

    @app.get("/p/{project_id}", response_class=HTMLResponse, dependencies=auth_deps)
    def project_home(request: Request, project_id: str) -> Any:
        project_dir = _resolve_project(project_id)
        conn = _connect(project_dir)
        try:
            shape = introspect(conn)
            summary = _project_summary(conn)
            heatmap = heatmap_mod.build(conn, shape)
            queue = heatmap_mod.next_up(conn, shape)
            unresolved_events = _qc_events(conn)[:20]
        finally:
            conn.close()
        return _TEMPLATES.TemplateResponse(
            request, "project_home.html",
            {
                "project_id": project_id,
                "project_dir": str(project_dir),
                "shape": shape,
                "summary": summary,
                "heatmap": heatmap,
                "queue": queue,
                "events": unresolved_events,
            },
        )

    @app.get("/p/{project_id}/patient/{pid}", response_class=HTMLResponse, dependencies=auth_deps)
    def patient_view(request: Request, project_id: str, pid: str) -> Any:
        project_dir = _resolve_project(project_id)
        conn = _connect(project_dir)
        try:
            data = _patient_drill(conn, pid)
            shape = introspect(conn)
        finally:
            conn.close()
        return _TEMPLATES.TemplateResponse(
            request, "patient.html",
            {"project_id": project_id, "shape": shape, **data},
        )

    @app.get("/p/{project_id}/qc", response_class=HTMLResponse, dependencies=auth_deps)
    def qc_log(request: Request, project_id: str) -> Any:
        project_dir = _resolve_project(project_id)
        conn = _connect(project_dir)
        try:
            events = _qc_events(conn)
            summary = _project_summary(conn)
        finally:
            conn.close()
        return _TEMPLATES.TemplateResponse(
            request, "qc_log.html",
            {
                "project_id": project_id,
                "project_dir": str(project_dir),
                "events": events,
                "summary": summary,
            },
        )

    @app.post("/p/{project_id}/censor", dependencies=auth_deps)
    def censor_action(
        project_id: str,
        level: str = Form(...),
        entity_id: str = Form(...),
        kind: str = Form("qc_fail"),
        reason: str = Form(...),
        return_to: str = Form(""),
    ) -> Any:
        project_dir = _resolve_project(project_id)
        result = mutations.censor(str(project_dir), level, entity_id, kind, reason)
        # Always redirect — the next page shows the new state. We surface failures
        # by funneling the operator to /qc where stderr from the CLI shows verbatim.
        target = return_to or f"/p/{project_id}/qc"
        return RedirectResponse(
            url=f"{target}?last_status={'ok' if result.ok else 'fail'}"
                f"&last_msg={result.stderr or result.stdout or 'censor done'}",
            status_code=303,
        )

    @app.post("/p/{project_id}/uncensor", dependencies=auth_deps)
    def uncensor_action(
        project_id: str,
        event_id: int = Form(...),
        reason: str = Form(...),
        ethics_override: bool = Form(False),
        return_to: str = Form(""),
    ) -> Any:
        project_dir = _resolve_project(project_id)
        result = mutations.uncensor(
            str(project_dir),
            event_id=event_id,
            reason=reason,
            ethics_override=bool(ethics_override),
        )
        target = return_to or f"/p/{project_id}/qc"
        return RedirectResponse(
            url=f"{target}?last_status={'ok' if result.ok else 'fail'}"
                f"&last_msg={result.stderr or result.stdout or 'uncensor done'}",
            status_code=303,
        )

    return app


def serve(
    host: str,
    port: int,
    registry_path: Path | None = None,
    auth_config: AuthConfig | None = None,
) -> None:
    """Run uvicorn in the foreground. Blocks."""
    import uvicorn
    app = create_app(registry_path_override=registry_path, auth_config=auth_config)
    uvicorn.run(app, host=host, port=port, log_level="info")
