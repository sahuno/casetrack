"""Tests for proposal 0007 — project lifecycle status.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import casetrack
from casetrack_lifecycle.gate import assert_not_archived
from casetrack_lifecycle.schema import (
    VALID_STATUSES,
    auto_migrate_if_needed,
    get_status,
    migrate_status,
    set_status,
)


# ── fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def proj(tmp_path):
    """Initialised casetrack project with project_meta row."""
    result = subprocess.run(
        [
            sys.executable, "-m", "casetrack",
            "init", "--project-dir", str(tmp_path),
            "--from-template", "hgsoc", "--bare",
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return tmp_path


@pytest.fixture()
def conn(proj):
    db = sqlite3.connect(str(proj / "casetrack.db"))
    yield db
    db.close()


# ── schema / migration ─────────────────────────────────────────────────────────

def test_migrate_adds_column(conn):
    # Remove status column to simulate pre-0.7 DB.
    conn.execute("ALTER TABLE project_meta RENAME TO project_meta_bak")
    conn.execute(
        "CREATE TABLE project_meta AS SELECT project_id,name,schema_v,created_at,"
        "casetrack_version FROM project_meta_bak"
    )
    conn.commit()
    added = migrate_status(conn)
    assert added is True
    cols = {row[1] for row in conn.execute("PRAGMA table_info(project_meta)")}
    assert "status" in cols


def test_migrate_idempotent(conn):
    added_first = migrate_status(conn)
    added_second = migrate_status(conn)
    # second call must not raise and must report False
    assert added_second is False


def test_default_status_is_active(conn):
    assert get_status(conn) == "active"


def test_set_status_complete(proj, conn):
    old = set_status(conn, "complete")
    assert old == "active"
    assert get_status(conn) == "complete"


def test_set_status_archived(proj, conn):
    set_status(conn, "archived")
    assert get_status(conn) == "archived"


def test_reverse_archived(proj, conn):
    set_status(conn, "archived")
    set_status(conn, "active")
    assert get_status(conn) == "active"


def test_set_status_invalid(conn):
    with pytest.raises(ValueError, match="status must be one of"):
        set_status(conn, "pending")


# ── gate ───────────────────────────────────────────────────────────────────────

def test_gate_active_passes(proj):
    # No exception on active project.
    assert_not_archived(proj)


def test_gate_complete_passes(proj, conn):
    set_status(conn, "complete")
    conn.close()
    # complete is not gated — should pass
    assert_not_archived(proj)


def test_write_gate_archived_exits(proj, conn):
    set_status(conn, "archived")
    conn.close()
    with pytest.raises(SystemExit) as exc_info:
        assert_not_archived(proj)
    assert exc_info.value.code == 2


def test_force_archived_override(proj, conn):
    set_status(conn, "archived")
    conn.close()
    # Both flags set — should not raise.
    assert_not_archived(proj, force_archived=True, yes=True)


def test_force_archived_requires_yes(proj, conn):
    set_status(conn, "archived")
    conn.close()
    with pytest.raises(SystemExit) as exc_info:
        assert_not_archived(proj, force_archived=True, yes=False)
    assert exc_info.value.code == 2


def test_gate_no_db_passes(tmp_path):
    # Directory with no DB — gate must not crash.
    assert_not_archived(tmp_path)


# ── CLI — project set-status / status ─────────────────────────────────────────

def _run(args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "casetrack"] + args,
        capture_output=True, text=True, env=env,
    )


def test_cli_set_status_complete(proj):
    r = _run(["project", "set-status", "--project-dir", str(proj), "--status", "complete"])
    assert r.returncode == 0
    assert "complete" in r.stdout


def test_cli_set_status_archived(proj):
    r = _run(["project", "set-status", "--project-dir", str(proj), "--status", "archived"])
    assert r.returncode == 0
    assert "archived" in r.stdout


def test_cli_project_status(proj):
    r = _run(["project", "status", "--project-dir", str(proj)])
    assert r.returncode == 0
    assert "active" in r.stdout


def test_provenance_logged(proj):
    _run(["project", "set-status", "--project-dir", str(proj),
          "--status", "complete", "--reason", "manuscript submitted"])
    prov_path = proj / "provenance.jsonl"
    entries = [json.loads(l) for l in prov_path.read_text().splitlines() if l.strip()]
    status_entries = [e for e in entries if e.get("action") == "project_status_change"]
    assert len(status_entries) == 1
    e = status_entries[0]
    assert e["to_status"] == "complete"
    assert e["from_status"] == "active"
    assert "manuscript submitted" in e["reason"]


# ── CLI — append blocked on archived ──────────────────────────────────────────

def test_append_blocked_on_archived(proj, tmp_path):
    # Archive the project first.
    _run(["project", "set-status", "--project-dir", str(proj), "--status", "archived"])

    # Register a patient+specimen+assay so append has rows to target.
    subprocess.run([sys.executable, "-m", "casetrack", "register",
                    "--project-dir", str(proj), "--level", "patient",
                    "--id", "P01"], capture_output=True)

    # Create a dummy results TSV.
    results = tmp_path / "results.tsv"
    results.write_text("assay_id\tsome_metric\n")

    r = _run([
        "append", "--project-dir", str(proj),
        "--level", "assay", "--analysis", "test",
        "--results", str(results),
    ])
    assert r.returncode == 2
    assert "archived" in r.stderr


# ── migrate-status command ─────────────────────────────────────────────────────

def test_migrate_status_command(proj):
    r = _run(["migrate-status", "--project-dir", str(proj)])
    assert r.returncode == 0
    # Idempotent second run.
    r2 = _run(["migrate-status", "--project-dir", str(proj)])
    assert r2.returncode == 0
    assert "up-to-date" in r2.stdout


# ── MCP list_projects_tool ─────────────────────────────────────────────────────

def test_mcp_list_default_active(proj, tmp_path, monkeypatch):
    """list_projects_tool() with default status='active' excludes archived."""
    import os
    reg_path = tmp_path / "registry.json"
    monkeypatch.setenv("CASETRACK_REGISTRY", str(reg_path))

    # Register the project.
    _run(["projects", "register", "--project-dir", str(proj)])

    from casetrack_mcp.tools import list_projects_tool
    result = list_projects_tool(status="active")
    ids = [p["project_id"] for p in result["projects"]]
    assert len(ids) >= 1

    # Archive it then re-check.
    _run(["project", "set-status", "--project-dir", str(proj), "--status", "archived"])
    result_after = list_projects_tool(status="active")
    ids_after = [p["project_id"] for p in result_after["projects"]]
    assert all(p.get("status") != "archived" for p in result_after["projects"])
    # All IDs from archived filter should come back with status=archived.
    result_arch = list_projects_tool(status="archived")
    assert all(p.get("status") == "archived" for p in result_arch["projects"])
