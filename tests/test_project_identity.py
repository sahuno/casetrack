"""Tests for v0.6 Part B: project_id + project_meta + registry
(proposal 0005 §5/§6).

Covers:
- validate_project_id / suggest_project_id pure-function behavior
- casetrack init writes project_meta row + TOML project_id + registry entry
- --project-id flag honored explicitly; auto-derive from --project-name
  or directory; rejects malformed slugs
- check_project_identity_consistency raises on TOML↔DB mismatch; skipped
  for legacy projects with no project_id in TOML or no project_meta row
- Registry helpers round-trip: register → resolve → list → deregister
- Registry collision: re-registering the same project_id at a different
  path is rejected
- `casetrack projects list / register / deregister / scan` subcommands
- --project <id> on _resolve_project resolves via registry
- Legacy projects (no project_id) continue to work — read paths skip
  the consistency check silently

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-19
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

import pytest

import casetrack


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path, monkeypatch):
    """Route the registry to a per-test JSON so tests never touch ~/.casetrack/."""
    reg_path = tmp_path / "registry.json"
    monkeypatch.setenv("CASETRACK_REGISTRY", str(reg_path))
    yield reg_path


def _init_ns(project_dir: Path, *, template: str = "blank",
             project_name: str | None = None,
             project_id: str | None = None,
             force: bool = False, bare: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        samples=None,
        key="sample_id",
        metadata=None,
        cols=None,
        from_template=template,
        project_name=project_name,
        project_id=project_id,
        force=force,
        bare=bare,
    )


# ── validate_project_id (pure) ────────────────────────────────────────────────


@pytest.mark.parametrize("good", [
    "abc",                       # min length 3
    "hgsoc-2026",
    "my-very-long-project-id-that-is-still-under-64-chars-12345",
    "a01",
    "0a-b",
    "a" * 64,
])
def test_validate_project_id_accepts(good: str):
    casetrack.validate_project_id(good)


@pytest.mark.parametrize("bad", [
    "Abc",                  # uppercase
    "hgsoc_2026",           # underscore not allowed
    "hgsoc.2026",           # dot not allowed
    "ab",                   # too short
    "a" * 65,               # too long
    "-abc",                 # leading hyphen
    "ab cd",                # whitespace
    "abc:def",              # colon
    "",                     # empty
    None,                   # null
    123,                    # not string
])
def test_validate_project_id_rejects(bad):
    with pytest.raises(ValueError):
        casetrack.validate_project_id(bad)


# ── suggest_project_id ────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,expected", [
    ("HGSOC methylation cohort", "hgsoc-methylation-cohort"),
    ("hgsoc_2026", "hgsoc-2026"),
    ("HGSOC.v2", "hgsoc-v2"),
    ("My Project (2026)", "my-project-2026"),
    ("--leading--", "leading"),
])
def test_suggest_project_id_recovers(name: str, expected: str):
    assert casetrack.suggest_project_id(name) == expected


@pytest.mark.parametrize("name", ["", "   ", "α β γ", "!"])
def test_suggest_project_id_returns_none_when_unsafe(name: str):
    assert casetrack.suggest_project_id(name) is None


def test_suggest_project_id_truncates_to_64():
    name = "a" * 80
    cleaned = casetrack.suggest_project_id(name)
    assert cleaned is not None and len(cleaned) <= 64


# ── init writes project_meta + TOML + registry ────────────────────────────────


def test_init_writes_project_id_to_toml(tmp_path: Path):
    proj = tmp_path / "hgsoc-2026"
    casetrack.cmd_init(_init_ns(proj))
    text = (proj / "casetrack.toml").read_text()
    assert 'project_id = "hgsoc-2026"' in text


def test_init_writes_project_meta_row(tmp_path: Path):
    proj = tmp_path / "hgsoc-2026"
    casetrack.cmd_init(_init_ns(proj))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        meta = casetrack.read_project_meta(conn)
    finally:
        conn.close()
    assert meta is not None
    assert meta["project_id"] == "hgsoc-2026"
    assert meta["schema_v"] == 1
    assert meta["casetrack_version"] == casetrack._CASETRACK_VERSION


def test_init_registers_project(tmp_path: Path, isolated_registry: Path):
    proj = tmp_path / "hgsoc-2026"
    casetrack.cmd_init(_init_ns(proj))
    reg = casetrack._registry_load()
    assert "hgsoc-2026" in reg["projects"]
    entry = reg["projects"]["hgsoc-2026"]
    assert Path(entry["path"]) == proj.resolve()


def test_init_explicit_project_id_overrides_dir_name(tmp_path: Path):
    proj = tmp_path / "weird-dir-name"
    casetrack.cmd_init(_init_ns(proj, project_id="my-custom-id"))
    text = (proj / "casetrack.toml").read_text()
    assert 'project_id = "my-custom-id"' in text


def test_init_derives_from_project_name(tmp_path: Path):
    proj = tmp_path / "anything"
    casetrack.cmd_init(_init_ns(proj, project_name="HGSOC methylation cohort"))
    text = (proj / "casetrack.toml").read_text()
    assert 'project_id = "hgsoc-methylation-cohort"' in text


def test_init_falls_back_to_dir_basename(tmp_path: Path):
    # project_name has chars that don't slug; falls back to dir basename.
    proj = tmp_path / "good-slug-name"
    casetrack.cmd_init(_init_ns(proj, project_name="!@#$%"))
    text = (proj / "casetrack.toml").read_text()
    assert 'project_id = "good-slug-name"' in text


def test_init_rejects_malformed_explicit_id(tmp_path: Path, capsys):
    proj = tmp_path / "anything"
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_init(_init_ns(proj, project_id="HGSOC_2026"))
    assert exc.value.code == 1
    assert "valid identifier" in capsys.readouterr().err


def test_init_fails_when_no_slug_derivable(tmp_path: Path, capsys):
    # Both --project-name and dir basename produce nothing valid.
    proj = tmp_path / "!@#$"
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_init(_init_ns(proj, project_name="!@#"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "could not derive" in err
    assert "--project-id" in err


# ── consistency check ─────────────────────────────────────────────────────────


def test_consistency_check_raises_on_toml_db_mismatch(tmp_path: Path):
    proj = tmp_path / "p"
    casetrack.cmd_init(_init_ns(proj, project_id="real-id"))
    # Hand-edit TOML to a DIFFERENT project_id.
    toml = proj / "casetrack.toml"
    toml.write_text(toml.read_text().replace('"real-id"', '"fake-id"'))
    schema = casetrack.load_schema(toml)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with pytest.raises(ValueError, match="project_id mismatch"):
            casetrack.check_project_identity_consistency(conn, schema, proj)
    finally:
        conn.close()


def test_consistency_check_skips_for_legacy_db(tmp_path: Path):
    """Legacy DB: project_meta table doesn't exist. Check is a silent no-op."""
    proj = tmp_path / "p"
    casetrack.cmd_init(_init_ns(proj, project_id="abc-123"))
    # Drop the project_meta table to simulate legacy v0.5 state.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        conn.execute("DROP TABLE project_meta")
        conn.commit()
    finally:
        conn.close()
    schema = casetrack.load_schema(proj / "casetrack.toml")
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        casetrack.check_project_identity_consistency(conn, schema, proj)  # no raise
    finally:
        conn.close()


def test_consistency_check_skips_for_legacy_toml(tmp_path: Path):
    """Legacy TOML: no project_id key. Check is a silent no-op."""
    proj = tmp_path / "p"
    casetrack.cmd_init(_init_ns(proj, project_id="abc-123"))
    # Strip the project_id line from TOML.
    toml = proj / "casetrack.toml"
    toml.write_text(
        "\n".join(
            line for line in toml.read_text().splitlines()
            if not line.startswith("project_id")
        )
    )
    schema = casetrack.load_schema(toml)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        casetrack.check_project_identity_consistency(conn, schema, proj)  # no raise
    finally:
        conn.close()


# ── registry round-trip ──────────────────────────────────────────────────────


def test_registry_round_trip(tmp_path: Path, isolated_registry: Path):
    p1 = tmp_path / "proj-a"
    p2 = tmp_path / "proj-b"
    p1.mkdir()
    p2.mkdir()
    casetrack.registry_register("proj-a", p1, "Project A")
    casetrack.registry_register("proj-b", p2, "Project B")

    assert casetrack.registry_resolve("proj-a") == p1
    assert casetrack.registry_resolve("proj-b") == p2
    assert casetrack.registry_resolve("nope") is None

    reg = casetrack._registry_load()
    assert set(reg["projects"]) == {"proj-a", "proj-b"}

    # Touch updates last_seen.
    before = reg["projects"]["proj-a"]["last_seen"]
    casetrack.registry_touch("proj-a")
    after = casetrack._registry_load()["projects"]["proj-a"]["last_seen"]
    assert after >= before  # may be equal at sub-second resolution

    assert casetrack.registry_deregister("proj-a") is True
    assert casetrack.registry_resolve("proj-a") is None
    assert casetrack.registry_deregister("proj-a") is False  # second time = noop


def test_registry_register_rejects_collision(tmp_path: Path, isolated_registry: Path):
    p1 = tmp_path / "proj-a"
    p2 = tmp_path / "proj-b"
    p1.mkdir()
    p2.mkdir()
    casetrack.registry_register("collision", p1, "Project A")
    with pytest.raises(ValueError, match="registry conflict"):
        casetrack.registry_register("collision", p2, "Project B")


def test_registry_register_idempotent_same_path(tmp_path: Path, isolated_registry: Path):
    p1 = tmp_path / "proj-a"
    p1.mkdir()
    casetrack.registry_register("idem", p1, "Project A")
    casetrack.registry_register("idem", p1, "Project A renamed")  # no raise
    reg = casetrack._registry_load()
    assert reg["projects"]["idem"]["name"] == "Project A renamed"


# ── --project flag resolves via registry ─────────────────────────────────────


def test_resolve_project_by_id(tmp_path: Path, isolated_registry: Path):
    proj = tmp_path / "looks-up-by-id"
    casetrack.cmd_init(_init_ns(proj))
    project_dir, schema = casetrack._resolve_project(
        None, project_id="looks-up-by-id"
    )
    assert project_dir == proj.resolve() or project_dir == proj


def test_resolve_project_unknown_id_exits(tmp_path: Path, isolated_registry: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        casetrack._resolve_project(None, project_id="never-registered")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not in the registry" in err


def test_resolve_project_no_input_exits(capsys, isolated_registry: Path):
    with pytest.raises(SystemExit) as exc:
        casetrack._resolve_project(None, project_id=None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--project-dir" in err and "--project" in err


# ── projects list / register / deregister CLI ────────────────────────────────


def _projects_ns(action: str, **kwargs) -> argparse.Namespace:
    return argparse.Namespace(projects_action=action, **kwargs)


def test_projects_list_empty(isolated_registry: Path, capsys):
    casetrack.cmd_projects(_projects_ns("list", fmt="table"))
    out = capsys.readouterr().out
    assert "No projects registered" in out


def test_projects_list_table(tmp_path: Path, isolated_registry: Path, capsys):
    proj = tmp_path / "alpha"
    casetrack.cmd_init(_init_ns(proj))
    capsys.readouterr()
    casetrack.cmd_projects(_projects_ns("list", fmt="table"))
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "1 project(s)" in out


def test_projects_list_json(tmp_path: Path, isolated_registry: Path, capsys):
    proj = tmp_path / "alpha"
    casetrack.cmd_init(_init_ns(proj))
    capsys.readouterr()
    casetrack.cmd_projects(_projects_ns("list", fmt="json"))
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["registry"] == str(isolated_registry)
    pids = {p["project_id"] for p in payload["projects"]}
    assert pids == {"alpha"}


def test_projects_register_from_existing_dir(tmp_path: Path, isolated_registry: Path, capsys):
    proj = tmp_path / "to-register"
    casetrack.cmd_init(_init_ns(proj))
    # Wipe registry and re-register from project dir.
    isolated_registry.write_text(
        json.dumps({"schema_v": 1, "projects": {}})
    )
    capsys.readouterr()
    casetrack.cmd_projects(_projects_ns("register", project_dir=str(proj)))
    out = capsys.readouterr().out
    assert "Registered" in out
    reg = casetrack._registry_load()
    assert "to-register" in reg["projects"]


def test_projects_deregister(tmp_path: Path, isolated_registry: Path, capsys):
    proj = tmp_path / "ephemeral"
    casetrack.cmd_init(_init_ns(proj))
    capsys.readouterr()
    casetrack.cmd_projects(_projects_ns("deregister", project_id="ephemeral"))
    out = capsys.readouterr().out
    assert "Deregistered" in out
    assert "ephemeral" not in casetrack._registry_load()["projects"]


def test_projects_deregister_unknown_exits(isolated_registry: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_projects(_projects_ns("deregister", project_id="never-existed"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "nothing to do" in err


def test_projects_no_action_prints_help(isolated_registry: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_projects(_projects_ns(None))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "subaction" in err
