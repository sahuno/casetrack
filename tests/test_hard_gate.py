"""Tests for the v0.6.0 final hard-error gate that refuses commands on
projects without v0.6 identity wiring (proposal 0005 §9 step 4).

Three locked-in design calls being verified:
  1. Strict — applies to read paths (query, dashboard, export) too.
  2. Bypass via CASETRACK_ALLOW_LEGACY=1 env var (not a per-command flag).
  3. Suggestion-only — error tells the user to run migrate-project-id;
     never auto-runs it.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-19
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

import casetrack


# ── fixtures ──────────────────────────────────────────────────────────────────


def _init_ns(project_dir: Path, *, template: str = "blank",
             project_name: str | None = None,
             project_id: str | None = None,
             force: bool = False, bare: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None,
        key="sample_id", metadata=None, cols=None,
        from_template=template, project_name=project_name,
        project_id=project_id, force=force, bare=bare,
    )


def _make_legacy_project(tmp_path: Path, name: str = "legacy") -> Path:
    """Strip the v0.6 identity bits from a freshly-init'd project."""
    proj = tmp_path / name
    casetrack.cmd_init(_init_ns(proj, project_name=name))
    toml = proj / "casetrack.toml"
    toml.write_text(
        "\n".join(
            line for line in toml.read_text().splitlines()
            if not line.startswith("project_id")
        ) + "\n"
    )
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        conn.execute("DROP TABLE project_meta")
        conn.commit()
    finally:
        conn.close()
    casetrack.registry_deregister(name)
    return proj


# ── core gate behavior ───────────────────────────────────────────────────────


def test_legacy_project_blocked_by_default(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("CASETRACK_ALLOW_LEGACY", raising=False)
    proj = _make_legacy_project(tmp_path)
    with pytest.raises(SystemExit) as exc:
        casetrack._resolve_project(proj)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "missing v0.6 identity wiring" in err
    assert "casetrack migrate-project-id" in err
    assert str(proj) in err
    assert "CASETRACK_ALLOW_LEGACY" in err  # bypass mentioned


def test_env_var_bypasses_gate(tmp_path: Path, capsys, monkeypatch):
    proj = _make_legacy_project(tmp_path)
    monkeypatch.setenv("CASETRACK_ALLOW_LEGACY", "1")
    project_dir, schema = casetrack._resolve_project(proj)
    assert project_dir == proj


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "Yes", "on"])
def test_env_var_truthy_values(tmp_path: Path, monkeypatch, truthy: str):
    proj = _make_legacy_project(tmp_path)
    monkeypatch.setenv("CASETRACK_ALLOW_LEGACY", truthy)
    casetrack._resolve_project(proj)  # no raise


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off"])
def test_env_var_falsy_values_still_block(
    tmp_path: Path, monkeypatch, falsy: str, capsys,
):
    proj = _make_legacy_project(tmp_path)
    monkeypatch.setenv("CASETRACK_ALLOW_LEGACY", falsy)
    with pytest.raises(SystemExit):
        casetrack._resolve_project(proj)


def test_freshly_initialised_project_unaffected(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CASETRACK_ALLOW_LEGACY", raising=False)
    proj = tmp_path / "fresh"
    casetrack.cmd_init(_init_ns(proj))  # writes project_id automatically
    project_dir, _ = casetrack._resolve_project(proj)  # no raise
    assert project_dir == proj


def test_migrate_then_run_works_without_bypass(
    tmp_path: Path, capsys, monkeypatch,
):
    monkeypatch.delenv("CASETRACK_ALLOW_LEGACY", raising=False)
    proj = _make_legacy_project(tmp_path, name="needs-migration")
    capsys.readouterr()
    # Migrate first.
    with pytest.raises(SystemExit):
        casetrack.cmd_migrate_project_id(argparse.Namespace(
            project_dir=str(proj), scan=None, project_id=None, yes=True,
        ))
    # Now resolve_project should succeed without the env var.
    project_dir, _ = casetrack._resolve_project(proj)
    assert project_dir == proj


# ── strictness applies to read paths too ─────────────────────────────────────


def test_query_blocked_on_legacy(tmp_path: Path, capsys, monkeypatch):
    """Read-only command (query) must also fire the gate per design call #1."""
    monkeypatch.delenv("CASETRACK_ALLOW_LEGACY", raising=False)
    proj = _make_legacy_project(tmp_path, name="legacy-read")
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_query_project(argparse.Namespace(
            project_dir=str(proj), project=None,
            sql="SELECT 1", fmt="table", output=None,
            view=None,
        ))
    assert exc.value.code == 1
    assert "missing v0.6 identity wiring" in capsys.readouterr().err


def test_dashboard_blocked_on_legacy(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("CASETRACK_ALLOW_LEGACY", raising=False)
    proj = _make_legacy_project(tmp_path, name="legacy-dash")
    out = tmp_path / "dash.html"
    with pytest.raises(SystemExit):
        casetrack.cmd_dashboard_project(argparse.Namespace(
            project_dir=str(proj), project=None,
            output=str(out), key="sample_id", manifest=None,
        ))
    err = capsys.readouterr().err
    assert "missing v0.6 identity wiring" in err


# ── partial state handling ──────────────────────────────────────────────────


def test_toml_has_id_but_db_does_not(tmp_path: Path, capsys, monkeypatch):
    """TOML has project_id but project_meta row is missing (e.g. someone
    dropped the table). Gate fires — needs migrate to fill the gap."""
    monkeypatch.delenv("CASETRACK_ALLOW_LEGACY", raising=False)
    proj = tmp_path / "half"
    casetrack.cmd_init(_init_ns(proj, project_id="half-state"))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        conn.execute("DROP TABLE project_meta")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(SystemExit):
        casetrack._resolve_project(proj)
    err = capsys.readouterr().err
    assert "project_meta row" in err


def test_db_has_id_but_toml_does_not(tmp_path: Path, capsys, monkeypatch):
    """project_meta row exists but TOML lacks project_id (someone hand-edited
    the TOML)."""
    monkeypatch.delenv("CASETRACK_ALLOW_LEGACY", raising=False)
    proj = tmp_path / "half2"
    casetrack.cmd_init(_init_ns(proj, project_id="half-state-2"))
    toml = proj / "casetrack.toml"
    toml.write_text(
        "\n".join(
            line for line in toml.read_text().splitlines()
            if not line.startswith("project_id")
        ) + "\n"
    )
    with pytest.raises(SystemExit):
        casetrack._resolve_project(proj)
    err = capsys.readouterr().err
    assert "[project] project_id" in err
