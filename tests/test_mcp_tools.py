"""Tests for casetrack_mcp.tools — pure-Python tool helpers.

Doesn't require the `mcp` SDK to be installed (the server adapter in
casetrack_mcp.server is the only module that imports mcp).

Covers the proposal 0005 §5.6 contract:
  - Closed-world lookup: unknown project_id → fail-fast with the valid set.
  - Read-only SQL: non-SELECT rejected.
  - Legacy projects blocked by the hard gate unless CASETRACK_ALLOW_LEGACY=1.
  - Row limit truncation flags truncated=True in the payload.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-19
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

import casetrack
from casetrack_mcp.tools import (
    MCPToolError,
    cohort_artifacts_tool,
    list_projects_tool,
    query_tool,
)


# ── fixtures ─────────────────────────────────────────────────────────────────


def _init_ns(project_dir: Path, *, project_name: str | None = None,
             project_id: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None,
        key="sample_id", metadata=None, cols=None,
        from_template="blank", project_name=project_name,
        project_id=project_id, force=False, bare=True,
    )


@pytest.fixture
def populated_project(tmp_path: Path) -> Path:
    """A freshly-init'd project with a patient row we can SELECT."""
    proj = tmp_path / "project-alpha"
    casetrack.cmd_init(_init_ns(proj))
    casetrack.cmd_register(argparse.Namespace(
        project_dir=str(proj), level="patient", id="HG006",
        parent=None, meta=None, allow_new_parent=False, yes=False,
    ))
    return proj


# ── list_projects_tool ────────────────────────────────────────────────────────


def test_list_projects_empty_registry():
    payload = list_projects_tool()
    assert payload["projects"] == []
    assert payload["schema_v"] == 1


def test_list_projects_returns_sorted_entries(tmp_path: Path):
    for name in ("charlie", "alpha", "bravo"):
        casetrack.cmd_init(_init_ns(tmp_path / name))
    payload = list_projects_tool()
    ids = [p["project_id"] for p in payload["projects"]]
    assert ids == ["alpha", "bravo", "charlie"]


def test_list_projects_payload_shape(populated_project: Path):
    payload = list_projects_tool()
    assert payload["projects"][0]["project_id"] == "project-alpha"
    assert "path" in payload["projects"][0]
    assert "last_seen" in payload["projects"][0]
    assert payload["registry"].endswith("registry.json")


# ── query_tool — unknown project_id ──────────────────────────────────────────


def test_query_unknown_project_id_fails(populated_project: Path):
    with pytest.raises(MCPToolError) as exc:
        query_tool("never-heard-of-it", "SELECT 1")
    msg = str(exc.value)
    assert "never-heard-of-it" in msg
    assert "not in the casetrack registry" in msg
    # Closed-world hint: the valid id is included.
    assert "project-alpha" in msg


def test_query_unknown_project_id_when_empty(tmp_path: Path):
    with pytest.raises(MCPToolError) as exc:
        query_tool("missing", "SELECT 1")
    assert "No projects are registered" in str(exc.value)


@pytest.mark.parametrize("bad_input,expected", [
    ("", "non-empty"),
    (None, "non-empty"),
    (123, "non-empty"),
])
def test_query_rejects_bad_project_id(bad_input, expected):
    with pytest.raises(MCPToolError, match=expected):
        query_tool(bad_input, "SELECT 1")


# ── cohort_artifacts_tool (proposal 0009) ────────────────────────────────────


def _project_with_cohort_artifact(tmp_path: Path, *, censor: bool) -> str:
    """Init a registered project, seed 2 assays, register a cohort artifact,
    optionally censor one input. Returns the project_id."""
    from casetrack_qc.cohort_artifacts_cli import cmd_append_cohort

    proj = tmp_path / "project-cohort"
    casetrack.cmd_init(_init_ns(proj))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES ('P1'), ('P2');"
                "INSERT INTO specimens (specimen_id, patient_id) VALUES "
                "  ('P1-t', 'P1'), ('P2-t', 'P2');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('P1-t-ONT', 'P1-t', 'ONT'), ('P2-t-ONT', 'P2-t', 'ONT');"
            )
    finally:
        conn.close()
    cmd_append_cohort(argparse.Namespace(
        project_dir=str(proj), analysis="joint_genotype", run_tag="run1",
        path="/cohort.vcf.gz", inputs="P1-t-ONT,P2-t-ONT", inputs_from=None,
        stats=None, checksum=None, created_by=None,
    ))
    if censor:
        conn = casetrack.open_project_db(proj / "casetrack.db")
        try:
            with casetrack.begin_immediate(conn):
                conn.execute(
                    "UPDATE assays SET qc_status='censored' "
                    "WHERE assay_id='P1-t-ONT'"
                )
        finally:
            conn.close()
    return "project-cohort"


def test_cohort_artifacts_tool_unknown_project(populated_project: Path):
    with pytest.raises(MCPToolError) as exc:
        cohort_artifacts_tool("never-heard-of-it")
    assert "not in the casetrack registry" in str(exc.value)


def test_cohort_artifacts_tool_fresh(tmp_path: Path):
    pid = _project_with_cohort_artifact(tmp_path, censor=False)
    payload = cohort_artifacts_tool(pid)
    assert payload["n_artifacts"] == 1
    assert payload["n_stale"] == 0
    art = payload["artifacts"][0]
    assert art["run_tag"] == "run1"
    assert art["stale"] is False
    assert art["n_censored_inputs"] == 0


def test_cohort_artifacts_tool_flags_stale(tmp_path: Path):
    pid = _project_with_cohort_artifact(tmp_path, censor=True)
    payload = cohort_artifacts_tool(pid)
    assert payload["n_stale"] == 1
    art = payload["artifacts"][0]
    assert art["stale"] is True
    assert art["censored_inputs"] == ["P1-t-ONT"]


def test_cohort_artifacts_tool_stale_only(tmp_path: Path):
    pid = _project_with_cohort_artifact(tmp_path, censor=False)
    payload = cohort_artifacts_tool(pid, stale_only=True)
    assert payload["n_artifacts"] == 0
    assert payload["artifacts"] == []


def test_query_rejects_empty_sql(populated_project: Path):
    with pytest.raises(MCPToolError, match="sql must be a non-empty string"):
        query_tool("project-alpha", "")
    with pytest.raises(MCPToolError, match="sql must be a non-empty string"):
        query_tool("project-alpha", "   ")


# ── query_tool — read-only guard ──────────────────────────────────────────────


@pytest.mark.parametrize("sql", [
    "SELECT * FROM patients",
    "select patient_id from patients",
    "  \n\t SELECT 1",
    "-- comment\nSELECT 1",
    "/* comment */ SELECT 1",
    "WITH cte AS (SELECT 1) SELECT * FROM cte",
])
def test_query_accepts_read_only_sql(populated_project: Path, sql: str):
    payload = query_tool("project-alpha", sql)
    assert "rows" in payload
    assert "columns" in payload


@pytest.mark.parametrize("sql", [
    "INSERT INTO patients (patient_id) VALUES ('X')",
    "UPDATE patients SET patient_id = 'Y' WHERE patient_id = 'HG006'",
    "DELETE FROM patients",
    "DROP TABLE patients",
    "ALTER TABLE patients ADD COLUMN foo TEXT",
    "CREATE TABLE foo (id INTEGER)",
    "REPLACE INTO patients VALUES ('X')",
])
def test_query_rejects_non_select(populated_project: Path, sql: str):
    with pytest.raises(MCPToolError) as exc:
        query_tool("project-alpha", sql)
    assert "SELECT" in str(exc.value) or "select" in str(exc.value).lower()


# ── query_tool — happy path payload shape ────────────────────────────────────


def test_query_payload_shape(populated_project: Path):
    payload = query_tool("project-alpha", "SELECT patient_id FROM patients")
    assert payload["project_id"] == "project-alpha"
    assert str(populated_project) in payload["project_path"]
    assert payload["columns"] == ["patient_id"]
    assert payload["rows"] == [{"patient_id": "HG006"}]
    assert payload["row_count"] == 1
    assert payload["truncated"] is False
    assert payload["row_limit"] == 10_000


def test_query_bubbles_sql_errors_as_tool_errors(populated_project: Path):
    """Invalid SQL produces MCPToolError, not a raw sqlite3 exception."""
    with pytest.raises(MCPToolError, match="SQL error"):
        query_tool("project-alpha", "SELECT column_that_does_not_exist FROM patients")


def test_query_row_limit_truncates(tmp_path: Path):
    """Insert more rows than the row_limit and confirm truncated=True."""
    proj = tmp_path / "big-proj"
    casetrack.cmd_init(_init_ns(proj))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            for i in range(25):
                conn.execute(
                    "INSERT INTO patients (patient_id) VALUES (?)",
                    (f"P{i:04d}",),
                )
    finally:
        conn.close()
    payload = query_tool(
        "big-proj", "SELECT patient_id FROM patients", row_limit=10,
    )
    assert payload["row_count"] == 10
    assert payload["truncated"] is True


# ── query_tool — hard gate on legacy projects ────────────────────────────────


def test_query_legacy_project_blocked_by_default(tmp_path: Path, monkeypatch):
    """Pre-v0.6 projects (no project_meta, no TOML project_id) must fail
    the MCP tool by default, matching the CLI hard-gate behavior."""
    monkeypatch.delenv("CASETRACK_ALLOW_LEGACY", raising=False)
    # Build a legacy project by stripping identity after init.
    proj = tmp_path / "legacy-cohort"
    casetrack.cmd_init(_init_ns(proj))
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
    # Registry entry is still there — query should get past the lookup
    # and then hit the hard gate inside query_tool.
    with pytest.raises(MCPToolError, match="missing v0.6 identity wiring"):
        query_tool("legacy-cohort", "SELECT 1")


def test_query_legacy_project_bypass_via_env(tmp_path: Path, monkeypatch):
    """CASETRACK_ALLOW_LEGACY=1 in the server env unblocks legacy reads."""
    # Same setup as above.
    proj = tmp_path / "legacy-ok"
    casetrack.cmd_init(_init_ns(proj))
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
    monkeypatch.setenv("CASETRACK_ALLOW_LEGACY", "1")
    payload = query_tool("legacy-ok", "SELECT 1 AS one")
    assert payload["rows"] == [{"one": 1}]


# ── query_tool — stale registry entry ────────────────────────────────────────


def test_query_handles_missing_db(tmp_path: Path):
    """Registry entry points at a deleted DB — tool emits a helpful
    'stale' message instead of a generic crash."""
    proj = tmp_path / "ephemeral"
    casetrack.cmd_init(_init_ns(proj))
    (proj / "casetrack.db").unlink()
    with pytest.raises(MCPToolError, match="stale"):
        query_tool("ephemeral", "SELECT 1")


# ── query_tool touches the registry ─────────────────────────────────────────


def test_query_touches_last_seen(populated_project: Path):
    before = casetrack._registry_load()["projects"]["project-alpha"]["last_seen"]
    query_tool("project-alpha", "SELECT 1")
    after = casetrack._registry_load()["projects"]["project-alpha"]["last_seen"]
    assert after >= before
