"""Tests for `casetrack init --project-dir` (v0.3 / proposal 0001 §7).

Verifies directory layout, DB schema, provenance entry, and .gitignore after
a fresh project init. Also covers the --force / collision behavior.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import casetrack


def _init_ns(project_dir: Path, *, template: str = "blank", force: bool = False,
             project_name: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        samples=None,
        key="sample_id",
        metadata=None,
        cols=None,
        from_template=template,
        project_name=project_name,
        force=force,
    )


# ── File layout ───────────────────────────────────────────────────────────────


def test_init_creates_expected_files(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    captured = capsys.readouterr()
    assert "Initialized casetrack project" in captured.out

    assert (proj / "casetrack.toml").exists()
    assert (proj / "casetrack.db").exists()
    assert (proj / "provenance.jsonl").exists()
    assert (proj / ".gitignore").exists()


def test_init_creates_missing_directory(tmp_path: Path):
    """init should `mkdir -p` the target directory."""
    proj = tmp_path / "nested" / "dir" / "proj"
    casetrack.cmd_init(_init_ns(proj))
    assert proj.is_dir()
    assert (proj / "casetrack.db").exists()


def test_gitignore_excludes_db_and_wal(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    contents = (proj / ".gitignore").read_text()
    assert "casetrack.db" in contents
    assert "casetrack.db-wal" in contents
    assert "casetrack.db-shm" in contents


# ── DB schema ─────────────────────────────────────────────────────────────────


def test_init_creates_three_tables(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert names == {"patients", "specimens", "assays"}
    finally:
        conn.close()


def test_hgsoc_template_creates_enum_check(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        # sqlite_master.sql contains the CHECK clause verbatim.
        (sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='assays'"
        ).fetchone()
        assert "CHECK" in sql
        assert "'scRNA'" in sql and "'WGS'" in sql
    finally:
        conn.close()


def test_foreign_key_enforcement_after_init(tmp_path: Path):
    """New DB should reject child rows whose parent doesn't exist."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))

    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "VALUES ('spec1', 'ghost_patient', 'tumor')"
            )
    finally:
        conn.close()


# ── Provenance ────────────────────────────────────────────────────────────────


def test_init_logs_provenance_entry(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))

    lines = (proj / "provenance.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "init_project"
    assert entry["template"] == "hgsoc"
    assert entry["schema_v_before"] == 0
    assert entry["schema_v_after"] == 1
    assert entry["transaction_id"].startswith("txn_")
    assert len(entry["sql"]) == 3  # one CREATE TABLE per level
    assert any("CREATE TABLE" in s and "patients" in s for s in entry["sql"])


# ── --force / collision ───────────────────────────────────────────────────────


def test_init_refuses_overwrite_without_force(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_init(_init_ns(proj))
    assert excinfo.value.code == 1
    assert "already exists" in capsys.readouterr().err


def test_init_force_rewrites_db(tmp_path: Path):
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj))

    # Tamper: add a stray table.
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    conn.execute("CREATE TABLE stray (x INTEGER)")
    conn.commit()
    conn.close()

    casetrack.cmd_init(_init_ns(proj, force=True))

    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "stray" not in names
        assert names == {"patients", "specimens", "assays"}
    finally:
        conn.close()


def test_invalid_template_name_exits(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_init(_init_ns(proj, template="does_not_exist"))
    assert excinfo.value.code == 1
    assert "unknown template" in capsys.readouterr().err


# ── CLI dispatch ──────────────────────────────────────────────────────────────


def test_cli_init_project_dir_smoke(tmp_path: Path):
    """Actually exec the CLI entrypoint so argparse wiring is exercised."""
    proj = tmp_path / "proj"
    res = subprocess.run(
        [
            sys.executable,
            str(Path(casetrack.__file__)),
            "init",
            "--project-dir", str(proj),
            "--from-template", "hgsoc",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"stderr: {res.stderr}"
    assert (proj / "casetrack.db").exists()


def test_cli_init_manifest_still_works(tmp_path: Path, samples_file: Path):
    """Flat-mode init path must still work through the CLI after dispatch refactor."""
    manifest = tmp_path / "manifest.tsv"
    res = subprocess.run(
        [
            sys.executable,
            str(Path(casetrack.__file__)),
            "init",
            "--manifest", str(manifest),
            "--samples", str(samples_file),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"stderr: {res.stderr}"
    assert manifest.exists()


def test_cli_init_requires_manifest_or_project_dir(tmp_path: Path):
    res = subprocess.run(
        [sys.executable, str(Path(casetrack.__file__)), "init"],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "--manifest" in res.stderr or "--project-dir" in res.stderr


def test_flat_init_without_samples_errors(tmp_path: Path):
    """Flat mode needs --samples; without it we expect a clean, typed error."""
    manifest = tmp_path / "manifest.tsv"
    res = subprocess.run(
        [
            sys.executable,
            str(Path(casetrack.__file__)),
            "init",
            "--manifest", str(manifest),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "--samples" in res.stderr
