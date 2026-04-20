"""Tests for `casetrack migrate-project-id` (proposal 0005 §7, Part B beta).

Single-project + batch (--scan) modes. Idempotent. Refuses slug conflicts
in the registry. Provenance entry per migration. Drift between TOML and
DB project_id is surfaced (not silently overwritten).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-19
"""
from __future__ import annotations

import argparse
import json
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


def _mpid_ns(*, project_dir: Path | None = None,
             scan: Path | None = None,
             project_id: str | None = None,
             yes: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir) if project_dir else None,
        scan=str(scan) if scan else None,
        project_id=project_id,
        yes=yes,
    )


def _make_legacy_project(tmp_path: Path, name: str = "legacy-cohort") -> Path:
    """Create a project, then strip out the v0.6 identity bits to simulate
    a v0.5 / pre-Part-B state: no project_id in TOML, no project_meta row,
    no registry entry.
    """
    proj = tmp_path / name
    casetrack.cmd_init(_init_ns(proj, project_name=name))
    # Remove project_id from TOML.
    toml = proj / "casetrack.toml"
    toml.write_text(
        "\n".join(
            line for line in toml.read_text().splitlines()
            if not line.startswith("project_id")
        ) + "\n"
    )
    # Drop project_meta table.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        conn.execute("DROP TABLE project_meta")
        conn.commit()
    finally:
        conn.close()
    # Wipe registry entry — registry path is per-test (via conftest fixture).
    casetrack.registry_deregister(name)
    return proj


# ── single-project: legacy → migrated ─────────────────────────────────────────


def test_migrate_legacy_project(tmp_path: Path, capsys):
    proj = _make_legacy_project(tmp_path, name="legacy-a")
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(project_dir=proj, yes=True))
    assert exc.value.code == 0  # nothing skipped
    out = capsys.readouterr().out

    assert "Migrated" in out
    assert "legacy-a" in out

    # TOML now has project_id.
    text = (proj / "casetrack.toml").read_text()
    assert 'project_id = "legacy-a"' in text

    # project_meta row exists.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        meta = casetrack.read_project_meta(conn)
    finally:
        conn.close()
    assert meta is not None
    assert meta["project_id"] == "legacy-a"

    # Registry entry exists.
    assert casetrack.registry_resolve("legacy-a") == proj.resolve()


def test_migrate_idempotent(tmp_path: Path, capsys):
    """Re-running migrate on an already-fully-migrated project is a no-op."""
    proj = tmp_path / "idem"
    casetrack.cmd_init(_init_ns(proj))  # already fully migrated
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(project_dir=proj, yes=True))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "No-op" in out
    assert "already migrated" in out


def test_migrate_with_explicit_project_id(tmp_path: Path, capsys):
    proj = _make_legacy_project(tmp_path, name="will-be-renamed")
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(
            project_dir=proj, project_id="custom-slug", yes=True,
        ))
    assert exc.value.code == 0

    text = (proj / "casetrack.toml").read_text()
    assert 'project_id = "custom-slug"' in text
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert casetrack.read_project_meta(conn)["project_id"] == "custom-slug"
    finally:
        conn.close()
    assert casetrack.registry_resolve("custom-slug") == proj.resolve()


def test_migrate_partial_state_toml_only(tmp_path: Path, capsys):
    """TOML has project_id but project_meta row + registry entry are missing
    (e.g. someone hand-added the field). Migrate fills the gaps without
    touching TOML."""
    proj = tmp_path / "partial"
    casetrack.cmd_init(_init_ns(proj, project_id="partial"))
    # Strip project_meta + registry, keep TOML.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        conn.execute("DROP TABLE project_meta")
        conn.commit()
    finally:
        conn.close()
    casetrack.registry_deregister("partial")
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(project_dir=proj, yes=True))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Migrated" in out
    # TOML wasn't rewritten (project_id was already there, matched target).
    assert "project_meta" in out
    assert "registry" in out

    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert casetrack.read_project_meta(conn)["project_id"] == "partial"
    finally:
        conn.close()
    assert casetrack.registry_resolve("partial") == proj.resolve()


def test_migrate_refuses_drift(tmp_path: Path, capsys):
    """TOML and DB disagree on project_id → migrate refuses, asks user to
    resolve manually."""
    proj = tmp_path / "drifted"
    casetrack.cmd_init(_init_ns(proj, project_id="real-id"))
    # Hand-edit TOML to a different id without updating the DB.
    toml = proj / "casetrack.toml"
    toml.write_text(toml.read_text().replace('"real-id"', '"forged-id"'))
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(project_dir=proj, yes=True))
    assert exc.value.code == 1  # something skipped
    err = capsys.readouterr().err
    assert "disagrees" in err
    assert "real-id" in err and "forged-id" in err


def test_migrate_refuses_slug_conflict(tmp_path: Path, capsys):
    """Suggested slug is already in the registry pointing at a different
    directory → refuse and ask for an explicit --project-id."""
    # First project takes the slug.
    p1 = tmp_path / "shared-slug"
    casetrack.cmd_init(_init_ns(p1))  # registers project_id 'shared-slug'
    # Second project (different dir) wants the same slug after migration.
    p2 = _make_legacy_project(tmp_path, name="shared-slug-2")
    # Force the slug to collide by renaming the project_name to match.
    toml2 = p2 / "casetrack.toml"
    toml2.write_text(
        toml2.read_text().replace(
            'name     = "shared-slug-2"', 'name     = "shared-slug"'
        )
    )
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(project_dir=p2, yes=True))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "already registered" in err
    assert "deregister" in err

    # Pass an explicit, unique id and confirm it succeeds.
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(
            project_dir=p2, project_id="actually-unique", yes=True,
        ))
    assert exc.value.code == 0
    assert casetrack.registry_resolve("actually-unique") == p2.resolve()


# ── batch --scan mode ────────────────────────────────────────────────────────


def test_scan_batch_migrates_each(tmp_path: Path, capsys):
    root = tmp_path / "cohorts"
    root.mkdir()
    p1 = _make_legacy_project(root, name="legacy-one")
    p2 = _make_legacy_project(root, name="legacy-two")
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(scan=root, yes=True))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Migrated" in out
    assert "legacy-one" in out
    assert "legacy-two" in out

    assert casetrack.registry_resolve("legacy-one") == p1.resolve()
    assert casetrack.registry_resolve("legacy-two") == p2.resolve()


def test_scan_skips_nothing_to_migrate(tmp_path: Path, capsys):
    """Mix of fully-migrated + truly-legacy projects; scan migrates the
    legacy ones and reports the others as no-op."""
    root = tmp_path / "mixed"
    root.mkdir()
    fresh = root / "fresh"
    casetrack.cmd_init(_init_ns(fresh))  # already migrated
    legacy = _make_legacy_project(root, name="needs-migration")
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(scan=root, yes=True))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "needs-migration" in out and "Migrated" in out
    assert "fresh" in out and "No-op" in out


def test_scan_rejects_explicit_project_id(tmp_path: Path, capsys):
    root = tmp_path / "any"
    root.mkdir()
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(
            scan=root, project_id="cannot-set-globally", yes=True,
        ))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "incompatible" in err


def test_scan_empty_root(tmp_path: Path, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    # No SystemExit because the function returns cleanly when no projects
    # are found (avoids exit 1 spam in CI for empty trees).
    casetrack.cmd_migrate_project_id(_mpid_ns(scan=root, yes=True))
    out = capsys.readouterr().out
    assert "No casetrack projects found" in out


# ── arg validation ──────────────────────────────────────────────────────────


def test_requires_project_dir_or_scan(capsys):
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns())
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--project-dir" in err and "--scan" in err


def test_project_dir_and_scan_mutually_exclusive(tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_migrate_project_id(_mpid_ns(
            project_dir=tmp_path / "a", scan=tmp_path / "b", yes=True,
        ))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


# ── provenance audit trail ───────────────────────────────────────────────────


def test_migration_logs_provenance(tmp_path: Path):
    proj = _make_legacy_project(tmp_path, name="audited")
    with pytest.raises(SystemExit):
        casetrack.cmd_migrate_project_id(_mpid_ns(project_dir=proj, yes=True))
    prov = (proj / "provenance.jsonl").read_text().strip().splitlines()
    actions = [json.loads(line)["action"] for line in prov]
    assert "migrate_project_id" in actions
    entry = next(json.loads(line) for line in prov
                 if json.loads(line)["action"] == "migrate_project_id")
    assert entry["project_id"] == "audited"
    assert "applied_to" in entry
    assert set(entry["applied_to"]) >= {"toml", "project_meta", "registry"}


# ── _insert_project_id_into_toml unit ────────────────────────────────────────


def test_insert_project_id_idempotent(tmp_path: Path):
    """Re-inserting overwrites the existing project_id line, doesn't duplicate."""
    proj = tmp_path / "idem-toml"
    casetrack.cmd_init(_init_ns(proj, project_id="first-id"))
    casetrack._insert_project_id_into_toml(proj / "casetrack.toml", "second-id")
    text = (proj / "casetrack.toml").read_text()
    assert text.count("project_id = ") == 1
    assert 'project_id = "second-id"' in text


def test_insert_project_id_into_legacy_toml(tmp_path: Path):
    """A TOML missing project_id gets the line inserted under [project]."""
    proj = _make_legacy_project(tmp_path, name="bare")
    toml = proj / "casetrack.toml"
    casetrack._insert_project_id_into_toml(toml, "freshly-added")
    text = toml.read_text()
    assert 'project_id = "freshly-added"' in text
    # Position check: the line should appear on a line right after [project].
    lines = text.splitlines()
    project_idx = lines.index("[project]")
    assert lines[project_idx + 1].strip().startswith('project_id =')
