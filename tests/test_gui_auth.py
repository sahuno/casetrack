"""Tests for HTTP Basic auth on the v0.8 operator GUI.

Strategy: build the same hgsoc test project as test_gui_app, but spin up
the FastAPI app via ``create_app(auth_config=AuthConfig(...))`` so the
auth gate is wired in. Then assert:

  • 401 without credentials, with WWW-Authenticate header
  • 401 on wrong username / wrong password
  • 200 on correct credentials
  • /healthz remains open (liveness probe must work without auth)
  • _resolve_auth honors env/file, refuses to start without a password,
    and bypasses cleanly under --no-auth.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import casetrack  # noqa: E402

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient


def _init_project(project_dir: Path, project_id: str = "auth-cohort") -> Path:
    casetrack.cmd_init(argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        samples=None,
        key="sample_id",
        metadata=None,
        cols=None,
        from_template="hgsoc",
        project_name="auth_cohort",
        project_id=project_id,
        force=False,
        bare=True,
    ))
    return project_dir


def _registry(tmp_path: Path, project_id: str, project_dir: Path) -> Path:
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps({
        "schema_v": 1,
        "projects": {
            project_id: {
                "path": str(project_dir.resolve()),
                "name": "auth_cohort",
                "created": "2026-04-30T00:00:00",
                "last_seen": "2026-04-30T00:00:00",
            }
        },
    }))
    return reg_path


@pytest.fixture
def authed_client(tmp_path: Path):
    project_dir = _init_project(tmp_path / "proj", project_id="auth-cohort")
    reg = _registry(tmp_path, "auth-cohort", project_dir)
    from casetrack_gui.app import create_app
    from casetrack_gui.auth import AuthConfig
    app = create_app(
        registry_path_override=reg,
        auth_config=AuthConfig(username="alice", password="hunter2"),
    )
    return TestClient(app)


# ── auth gate on protected routes ───────────────────────────────────────────


def test_root_unauthenticated_returns_401_with_basic_challenge(authed_client):
    r = authed_client.get("/")
    assert r.status_code == 401
    # Browsers need this header to pop up the credential dialog.
    assert "Basic" in r.headers.get("www-authenticate", "")
    assert "casetrack" in r.headers["www-authenticate"]


def test_root_wrong_username_returns_401(authed_client):
    r = authed_client.get("/", auth=("bob", "hunter2"))
    assert r.status_code == 401


def test_root_wrong_password_returns_401(authed_client):
    r = authed_client.get("/", auth=("alice", "wrong"))
    assert r.status_code == 401


def test_root_correct_credentials_returns_200(authed_client):
    r = authed_client.get("/", auth=("alice", "hunter2"))
    assert r.status_code == 200
    assert "auth-cohort" in r.text


def test_project_home_requires_auth(authed_client):
    assert authed_client.get("/p/auth-cohort").status_code == 401
    assert authed_client.get("/p/auth-cohort", auth=("alice", "hunter2")).status_code == 200


def test_qc_log_requires_auth(authed_client):
    assert authed_client.get("/p/auth-cohort/qc").status_code == 401
    assert authed_client.get("/p/auth-cohort/qc", auth=("alice", "hunter2")).status_code == 200


def test_censor_post_requires_auth(authed_client):
    r = authed_client.post(
        "/p/auth-cohort/censor",
        data={"level": "assay", "entity_id": "X", "kind": "qc_fail", "reason": "x"},
    )
    assert r.status_code == 401


# ── /healthz must stay open for liveness probes ─────────────────────────────


def test_healthz_does_not_require_auth(authed_client):
    r = authed_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ── _resolve_auth: CLI/env credential resolution ────────────────────────────


def _ns(**kw):
    """Build a minimal argparse.Namespace with sensible defaults."""
    defaults = dict(
        host="127.0.0.1", port=8765, registry=None,
        username=None, password_file=None, no_auth=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_resolve_auth_no_password_refuses_to_start(monkeypatch):
    monkeypatch.delenv("CASETRACK_GUI_PASSWORD", raising=False)
    monkeypatch.delenv("CASETRACK_GUI_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("CASETRACK_GUI_USER", raising=False)
    from casetrack_gui.cli import _resolve_auth
    with pytest.raises(SystemExit) as excinfo:
        _resolve_auth(_ns())
    assert "No password configured" in str(excinfo.value)


def test_resolve_auth_no_auth_flag_bypasses(monkeypatch):
    monkeypatch.delenv("CASETRACK_GUI_PASSWORD", raising=False)
    from casetrack_gui.cli import _resolve_auth
    assert _resolve_auth(_ns(no_auth=True)) is None


def test_resolve_auth_reads_env_password(monkeypatch):
    monkeypatch.setenv("CASETRACK_GUI_PASSWORD", "from-env")
    monkeypatch.delenv("CASETRACK_GUI_USER", raising=False)
    from casetrack_gui.cli import _resolve_auth
    cfg = _resolve_auth(_ns())
    assert cfg is not None
    assert cfg.username == "operator"  # default
    assert cfg.password == "from-env"


def test_resolve_auth_username_override(monkeypatch):
    monkeypatch.setenv("CASETRACK_GUI_PASSWORD", "x")
    from casetrack_gui.cli import _resolve_auth
    cfg = _resolve_auth(_ns(username="alice"))
    assert cfg.username == "alice"


def test_resolve_auth_password_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CASETRACK_GUI_PASSWORD", raising=False)
    pw = tmp_path / "gui_pw"
    pw.write_text("from-file\n")
    from casetrack_gui.cli import _resolve_auth
    cfg = _resolve_auth(_ns(password_file=str(pw)))
    assert cfg.password == "from-file"


def test_resolve_auth_password_file_overrides_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CASETRACK_GUI_PASSWORD", "ignored")
    pw = tmp_path / "gui_pw"
    pw.write_text("preferred")
    from casetrack_gui.cli import _resolve_auth
    cfg = _resolve_auth(_ns(password_file=str(pw)))
    assert cfg.password == "preferred"


def test_resolve_auth_password_file_missing_errors(tmp_path: Path):
    from casetrack_gui.cli import _resolve_auth
    with pytest.raises(SystemExit) as excinfo:
        _resolve_auth(_ns(password_file=str(tmp_path / "nope")))
    assert "not found" in str(excinfo.value)


def test_resolve_auth_password_file_empty_errors(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CASETRACK_GUI_PASSWORD", raising=False)
    pw = tmp_path / "empty"
    pw.write_text("")
    from casetrack_gui.cli import _resolve_auth
    with pytest.raises(SystemExit) as excinfo:
        _resolve_auth(_ns(password_file=str(pw)))
    assert "empty" in str(excinfo.value).lower() or "No password" in str(excinfo.value)
