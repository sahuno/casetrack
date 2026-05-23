"""Pure-Python tool implementations for the casetrack MCP server.

Kept separate from `server.py` so the tools are unit-testable without
the `mcp` SDK installed. The MCP adapter just wraps these functions in
`@app.call_tool()` handlers.

Two exported helpers:
  - list_projects_tool(): reads ~/.casetrack/registry.json.
  - query_tool(project_id, sql): resolves project_id via the registry,
    opens the project (enforcing the v0.6 hard-error gate), runs a
    read-only SELECT, returns rows as a list of dicts.

Design choices documented in proposal 0005 §5.6:
  - Closed-world lookup — unknown project_id returns a fail-fast error
    listing the valid set, not a generic "not found." Agents can't
    invent paths because paths are never input.
  - Read-only by default — non-SELECT SQL is rejected with
    `MCPToolError`. Agents that need to mutate casetrack state go
    through the regular CLI.
  - Legacy projects are refused unless CASETRACK_ALLOW_LEGACY=1 is set
    in the server's env, matching the v0.6 hard-gate semantics.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any  # noqa: F401 — kept for downstream callers

import casetrack


class MCPToolError(Exception):
    """Raised by a tool helper when the inputs are valid JSON but the
    requested operation can't proceed (e.g. unknown project_id, non-SELECT
    SQL). The server adapter surfaces this as a user-visible tool error
    instead of a protocol-level exception."""


# Only the first non-whitespace, non-comment token matters for the
# read-only check — this is a heuristic, not a SQL parser, and it's
# explicitly scoped to "block obvious mutations." A user determined to
# cause damage will use the CLI; the MCP path just shouldn't be the
# default attack surface.
_READ_ONLY_PREFIX = re.compile(r"\A\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*\s*(select|with)\b",
                               re.IGNORECASE | re.DOTALL)


def _read_registry() -> dict:
    """Read the registry — shallow wrapper so tests can monkeypatch one spot."""
    return casetrack._registry_load()


def list_projects_tool(status: str = "active") -> dict:
    """Return a machine-readable summary of the local registry.

    Parameters
    ----------
    status:
        Lifecycle filter. ``"active"`` (default) returns only active
        projects. ``"all"`` returns every project. ``"complete"`` or
        ``"archived"`` return those subsets. Comma-separated values are
        supported (e.g. ``"active,complete"``).

    Shape:
        {
            "registry": "/home/user/.casetrack/registry.json",
            "schema_v": 1,
            "status_filter": "active",
            "projects": [
                {
                    "project_id": "hgsoc-2026",
                    "name": "HGSOC methylation cohort",
                    "status": "active",
                    "path": "/data/...",
                    "created": "...",
                    "last_seen": "...",
                },
                ...
            ],
        }

    Designed to be small, flat, and JSON-serializable so an LLM can
    pick a project_id from the list and pass it back to query_tool.
    """
    from casetrack_lifecycle.schema import get_status as _get_lifecycle_status

    reg = _read_registry()
    all_entries = sorted(
        (
            {"project_id": pid, **info}
            for pid, info in reg.get("projects", {}).items()
        ),
        key=lambda e: e["project_id"],
    )

    # Enrich with lifecycle status.
    for e in all_entries:
        path = e.get("path")
        db_path = Path(path) / casetrack.PROJECT_DB_NAME if path else None
        if db_path and db_path.exists():
            try:
                conn = casetrack.open_project_db(db_path)
                e["status"] = _get_lifecycle_status(conn)
                conn.close()
            except Exception:
                e["status"] = "active"
        else:
            e["status"] = "active"

    # Apply status filter.
    if status == "all":
        entries = all_entries
    else:
        wanted = {s.strip() for s in status.split(",")}
        entries = [e for e in all_entries if e.get("status", "active") in wanted]

    return {
        "registry": str(casetrack._registry_path()),
        "schema_v": reg.get("schema_v"),
        "status_filter": status,
        "projects": entries,
    }


def _is_read_only_sql(sql: str) -> bool:
    return bool(_READ_ONLY_PREFIX.match(sql))


def query_tool(project_id: str, sql: str, *, row_limit: int = 10_000) -> dict:
    """Resolve `project_id` via the registry, run a read-only SELECT,
    return rows as a list of dicts.

    Raises MCPToolError with specific messages for the three failure
    modes an agent might hit: unknown project_id, non-SELECT SQL, or
    SQL execution error. Never raises a raw sqlite3 exception to the
    caller — all paths produce a user-visible string.

    `row_limit` caps how many rows we serialize; too-big result sets
    crash LLM context windows and the cap keeps the tool responsive.
    """
    if not isinstance(project_id, str) or not project_id:
        raise MCPToolError("project_id must be a non-empty string")
    if not isinstance(sql, str) or not sql.strip():
        raise MCPToolError("sql must be a non-empty string")

    resolved = casetrack.registry_resolve(project_id)
    if resolved is None:
        known = sorted((_read_registry().get("projects") or {}).keys())
        if known:
            hint = f" Known project_ids: {', '.join(known)}."
        else:
            hint = " No projects are registered. Run `casetrack init ...` to create one."
        raise MCPToolError(
            f"project_id {project_id!r} is not in the casetrack registry.{hint}"
        )

    if not _is_read_only_sql(sql):
        raise MCPToolError(
            "casetrack_query accepts SELECT / WITH statements only. "
            "To mutate data, use the `casetrack` CLI directly."
        )

    db_path = resolved / casetrack.PROJECT_DB_NAME
    if not db_path.exists():
        raise MCPToolError(
            f"casetrack.db not found for project {project_id!r} at {resolved}. "
            f"The registry entry may be stale — run `casetrack projects "
            f"deregister {project_id}` or point it at the right path."
        )

    # The hard-gate check (proposal 0005 §9 step 4) is enforced by
    # _resolve_project, which also catches TOML/DB drift. We don't
    # import that helper directly here because it calls sys.exit() on
    # failure — instead, we re-implement the gate inline so we can
    # raise MCPToolError cleanly.
    try:
        schema = casetrack.load_schema(resolved / casetrack.PROJECT_TOML_NAME)
    except casetrack.SchemaError as e:
        raise MCPToolError(
            f"schema load failed for project {project_id!r}: {e}"
        ) from e

    conn = casetrack.open_project_db(db_path)
    try:
        try:
            casetrack.require_project_identity_or_fail(conn, schema, resolved)
        except ValueError as e:
            raise MCPToolError(str(e)) from e
        try:
            casetrack.check_project_identity_consistency(conn, schema, resolved)
        except ValueError as e:
            raise MCPToolError(str(e)) from e

        # Run the query with a row cap. Using `execute + fetchmany(cap+1)`
        # so we can detect truncation and flag it to the agent.
        try:
            cursor = conn.execute(sql)
        except sqlite3.Error as e:
            raise MCPToolError(f"SQL error: {e}") from e
        cols = [d[0] for d in (cursor.description or [])]
        rows_raw = cursor.fetchmany(row_limit + 1)
        truncated = len(rows_raw) > row_limit
        rows_raw = rows_raw[:row_limit]
        rows = [dict(zip(cols, r)) for r in rows_raw]
    finally:
        conn.close()

    casetrack.registry_touch(project_id)
    return {
        "project_id": project_id,
        "project_path": str(resolved),
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "row_limit": row_limit,
    }


def _resolve_and_open(project_id: str):
    """Resolve a registry project_id to (resolved_path, open sqlite conn),
    enforcing the same closed-world lookup + hard-gate as ``query_tool``.

    Raises :class:`MCPToolError` on every failure mode so the server adapter
    surfaces a readable message. Caller must close the returned connection.
    """
    if not isinstance(project_id, str) or not project_id:
        raise MCPToolError("project_id must be a non-empty string")
    resolved = casetrack.registry_resolve(project_id)
    if resolved is None:
        known = sorted((_read_registry().get("projects") or {}).keys())
        hint = (f" Known project_ids: {', '.join(known)}." if known
                else " No projects are registered. Run `casetrack init ...` to create one.")
        raise MCPToolError(
            f"project_id {project_id!r} is not in the casetrack registry.{hint}"
        )
    db_path = resolved / casetrack.PROJECT_DB_NAME
    if not db_path.exists():
        raise MCPToolError(
            f"casetrack.db not found for project {project_id!r} at {resolved}. "
            f"The registry entry may be stale."
        )
    try:
        schema = casetrack.load_schema(resolved / casetrack.PROJECT_TOML_NAME)
    except casetrack.SchemaError as e:
        raise MCPToolError(f"schema load failed for project {project_id!r}: {e}") from e
    conn = casetrack.open_project_db(db_path)
    try:
        casetrack.require_project_identity_or_fail(conn, schema, resolved)
        casetrack.check_project_identity_consistency(conn, schema, resolved)
    except ValueError as e:
        conn.close()
        raise MCPToolError(str(e)) from e
    return resolved, conn


def references_tool(project_id: str, *, stale_only: bool = False) -> dict:
    """Return reference artifacts + ref-staleness for a project (proposal 0010).

    An output is ``stale`` when the reference version used at analysis time no
    longer matches the current declared version. This is the agent-facing
    companion to the `casetrack references` CLI.

    Returns ``{project_id, project_path, references:[...], stale_outputs:[...], outputs:[...]}``.
    On a pre-0010 project (no reference tables), all lists are empty.
    """
    from casetrack_qc.reference_artifacts import (
        reference_schema_exists as _ref_schema_exists,
        list_references as _list_references,
        all_stale_outputs as _all_stale_outputs,
    )

    resolved, conn = _resolve_and_open(project_id)
    try:
        refs: list[dict] = []
        stale: list[dict] = []
        tracked: list[dict] = []
        if _ref_schema_exists(conn):
            refs = [r.to_dict() for r in _list_references(conn)]
            all_outputs = _all_stale_outputs(conn)
            stale = [o for o in all_outputs if o["state"] == "STALE"]
            tracked = stale if stale_only else all_outputs
    finally:
        conn.close()

    casetrack.registry_touch(project_id)
    return {
        "project_id": project_id,
        "project_path": str(resolved),
        "references": refs,
        "stale_outputs": stale,
        "outputs": tracked,
    }


def derivation_tool(project_id: str, *, stale_only: bool = False) -> dict:
    """Return artifact-to-artifact lineage edges + derived-staleness (proposal 0011).

    A node is ``derived_stale`` when any upstream artifact it derives from is
    stale by any cause (0009 input / 0010 ref / 0011 transitive). Agent-facing
    companion to the `casetrack derivation` CLI command.

    Returns ``{project_id, project_path, edges:[...], derived_stale_outputs:[...],
    outputs:[...]}`` where ``derived_stale_outputs`` is the subset of tracked
    down_nodes whose derived state is STALE and ``outputs`` is the full list (or
    just the stale subset when ``stale_only`` is True).

    On a pre-0011 project (no ``artifact_derivation`` table), all lists are empty.
    """
    from casetrack_qc.artifact_derivation import (
        derivation_schema_exists as _deriv_exists,
        list_edges as _list_edges,
        all_derived_stale as _all_derived_stale,
    )

    resolved, conn = _resolve_and_open(project_id)
    try:
        edges: list[dict] = []
        stale: list[dict] = []
        tracked: list[dict] = []
        if _deriv_exists(conn):
            edges = _list_edges(conn)
            all_outputs = _all_derived_stale(conn)
            stale = [r for r in all_outputs if r["state"] == "STALE"]
            tracked = stale if stale_only else all_outputs
    finally:
        conn.close()

    casetrack.registry_touch(project_id)
    return {
        "project_id": project_id,
        "project_path": str(resolved),
        "edges": edges,
        "derived_stale_outputs": stale,
        "outputs": tracked,
    }


def cohort_artifacts_tool(project_id: str, *, stale_only: bool = False) -> dict:
    """Return cohort-level artifacts (proposal 0009) with read-time staleness.

    An artifact is ``stale`` when one or more of its contributing assays is
    currently censored or consent-revoked (the §4.4 cascade). This is the
    agent-facing companion to the `casetrack cohort-artifacts` CLI command —
    discoverable without the agent having to hand-write the cascade SQL.

    Returns ``{project_id, project_path, n_artifacts, n_stale, artifacts:[...]}``.
    On a pre-0009 project (no cohort-artifact tables), ``artifacts`` is empty.
    """
    from casetrack_qc.cohort_artifacts import (
        artifact_staleness as _artifact_staleness,
        cohort_artifacts_schema_exists as _ca_schema_exists,
        list_artifacts as _list_artifacts,
    )

    resolved, conn = _resolve_and_open(project_id)
    try:
        artifacts: list[dict] = []
        if _ca_schema_exists(conn):
            stale_map = _artifact_staleness(conn)
            for a in _list_artifacts(conn):
                censored = stale_map.get(a.artifact_id, [])
                if stale_only and not censored:
                    continue
                artifacts.append({
                    "artifact_id": a.artifact_id,
                    "analysis": a.analysis,
                    "run_tag": a.run_tag,
                    "path": a.path,
                    "n_inputs": a.n_inputs,
                    "region_scope": a.region_scope,
                    "stale": bool(censored),
                    "n_censored_inputs": len(censored),
                    "censored_inputs": censored,
                })
    finally:
        conn.close()

    casetrack.registry_touch(project_id)
    return {
        "project_id": project_id,
        "project_path": str(resolved),
        "n_artifacts": len(artifacts),
        "n_stale": sum(1 for a in artifacts if a["stale"]),
        "artifacts": artifacts,
    }
