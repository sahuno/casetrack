# Region-scoped artifacts + contrast roles — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a nullable `region_scope` to cohort artifacts and a nullable `role` to their inputs (proposal 0013), where a scope label matching a registered reference key auto-captures a 0010 `reference_usage` edge so scope-driven staleness reuses existing machinery.

**Architecture:** Two `ALTER TABLE ADD COLUMN`s on the existing 0009 sibling tables — no new tables. Column-presence-guarded so pre-0013 projects keep working. The reference-resolve "door" is a single call into the already-shipped `reference_artifacts.record_usage(scope="cohort", ...)`. All read paths (`cohort-artifacts`, `_cohort_artifacts` view, `status`, dashboard, MCP, export) gain the new columns; staleness logic is untouched.

**Tech Stack:** Python 3.10+, SQLite (stdlib `sqlite3`), DuckDB (read views), pytest. Code lives in `casetrack_qc/` alongside the 0009/0010 modules; argparse wiring in `casetrack_qc/cli.py`; monolith hooks in `casetrack.py`.

**Branch:** `feature/region-scoped-artifacts-0013` (already exists; proposal committed).

**Spec:** `docs/proposals/0013-region-scoped-artifacts.md`

**Conventions to honor:**
- Every commit message ends with the `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` trailer.
- Run the focused test after every implementation step. Full suite (`python3 -m pytest tests/ -q`) before the final version-bump commit.
- Module functions take an open `conn`; the command layer owns the transaction (`casetrack.begin_immediate`) and provenance. Never break this split.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `casetrack_qc/cohort_artifacts.py` | DDL, dataclass, CRUD, input-staleness | Add `region_scope` to DDL/dataclass/CRUD; add `role` to inputs; add `ensure_region_scope_columns`; `artifact_input_roles` reader |
| `casetrack_qc/cohort_artifacts_cli.py` | `append-cohort` / `migrate-cohort` / `cohort-artifacts` commands | `--region-scope`, `assay:role` parsing, reference-resolve capture, `migrate-region-scope`, `--scope` filter, scope in output |
| `casetrack_qc/cli.py` | argparse wiring + dispatch | New flags on `append-cohort` + `cohort-artifacts`; new `migrate-region-scope` parser; dispatch entry |
| `casetrack_qc/reader.py` | DuckDB `_cohort_artifacts` view | Add `region_scope` + derived `scope_ref_key`, presence-guarded |
| `casetrack.py` | status section, dashboard html, MCP info dict | Show `region_scope` in status + dashboard |
| `casetrack_mcp/tools.py` | `casetrack_cohort_artifacts` tool | Include `region_scope` in returned rows |
| `tests/test_region_scope_*.py` | new test modules | schema / cli / readpaths / reference-resolve |
| `CHANGELOG.md`, `README.md`, `setup.py`, `casetrack.py`, `CLAUDE.md`, skill | docs + version | v0.11.0 bump + 0013 docs |

---

## Task 1: Schema — `region_scope` + `role` columns

**Files:**
- Modify: `casetrack_qc/cohort_artifacts.py`
- Test: `tests/test_region_scope_schema.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_region_scope_schema.py`:

```python
"""Tests for proposal 0013 schema: region_scope + input role columns.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

import casetrack
from casetrack_qc import cohort_artifacts as ca


def _init_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    ns = argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    )
    casetrack.cmd_init(ns)
    return proj


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def test_fresh_init_has_region_scope_and_role(tmp_path: Path):
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert "region_scope" in _cols(conn, "cohort_artifacts")
        assert "role" in _cols(conn, "cohort_artifact_inputs")
    finally:
        conn.close()


def test_ensure_region_scope_columns_is_idempotent_and_additive(tmp_path: Path):
    """Drop the columns to emulate a pre-0013 project, then re-add them."""
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            # Emulate pre-0013: rebuild the 0009 tables without the new columns.
            conn.execute("DROP TABLE IF EXISTS cohort_artifact_inputs")
            conn.execute("DROP TABLE IF EXISTS cohort_artifacts")
            conn.execute(
                "CREATE TABLE cohort_artifacts (artifact_id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, analysis TEXT NOT NULL, run_tag TEXT NOT NULL, "
                "path TEXT NOT NULL, checksum TEXT, n_inputs INTEGER NOT NULL, "
                "stats_json TEXT, created_at TEXT NOT NULL, created_by TEXT, "
                "transaction_id TEXT NOT NULL, UNIQUE (analysis, run_tag))"
            )
            conn.execute(
                "CREATE TABLE cohort_artifact_inputs (artifact_id INTEGER NOT NULL, "
                "assay_id TEXT NOT NULL, PRIMARY KEY (artifact_id, assay_id))"
            )
        assert "region_scope" not in _cols(conn, "cohort_artifacts")
        with casetrack.begin_immediate(conn):
            executed = ca.ensure_region_scope_columns(conn)
        assert any("region_scope" in s for s in executed)
        assert any("role" in s for s in executed)
        assert "region_scope" in _cols(conn, "cohort_artifacts")
        assert "role" in _cols(conn, "cohort_artifact_inputs")
        # Second call is a no-op.
        with casetrack.begin_immediate(conn):
            assert ca.ensure_region_scope_columns(conn) == []
    finally:
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_region_scope_schema.py -v`
Expected: FAIL — `AttributeError: module 'casetrack_qc.cohort_artifacts' has no attribute 'ensure_region_scope_columns'` and the fresh-init assertion fails (`region_scope` not in columns).

- [ ] **Step 3: Add `region_scope` to the DDL**

In `casetrack_qc/cohort_artifacts.py`, edit `cohort_artifacts_ddl()` to add the column before the `UNIQUE` line:

```python
def cohort_artifacts_ddl() -> str:
    return (
        "CREATE TABLE cohort_artifacts (\n"
        "    artifact_id    INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    analysis       TEXT NOT NULL,\n"
        "    run_tag        TEXT NOT NULL,\n"
        "    path           TEXT NOT NULL,\n"
        "    checksum       TEXT,\n"
        "    n_inputs       INTEGER NOT NULL,\n"
        "    stats_json     TEXT,\n"
        "    region_scope   TEXT,\n"
        "    created_at     TEXT NOT NULL,\n"
        "    created_by     TEXT,\n"
        "    transaction_id TEXT NOT NULL,\n"
        "    UNIQUE (analysis, run_tag)\n"
        ")"
    )
```

And edit `cohort_artifact_inputs_ddl()` to add `role`:

```python
def cohort_artifact_inputs_ddl() -> str:
    return (
        "CREATE TABLE cohort_artifact_inputs (\n"
        "    artifact_id INTEGER NOT NULL "
        "REFERENCES cohort_artifacts(artifact_id) ON DELETE CASCADE,\n"
        "    assay_id    TEXT NOT NULL REFERENCES assays(assay_id) ON DELETE RESTRICT,\n"
        "    role        TEXT,\n"
        "    PRIMARY KEY (artifact_id, assay_id)\n"
        ")"
    )
```

- [ ] **Step 4: Add the column-ensure helper and wire it into `ensure_cohort_artifacts_schema`**

Add a `_column_exists` helper and `ensure_region_scope_columns` near the introspection helpers (after `_table_exists`):

```python
def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return any(r[1] == column for r in rows)


def ensure_region_scope_columns(conn: sqlite3.Connection) -> list[str]:
    """Add the proposal-0013 columns to existing 0009 tables. Idempotent.

    ``region_scope`` on ``cohort_artifacts`` and ``role`` on
    ``cohort_artifact_inputs``. No-op (returns ``[]``) when the tables are
    absent (pre-0009) or the columns already exist (fresh DDL or already
    migrated). Caller owns the transaction.
    """
    executed: list[str] = []
    if _table_exists(conn, "cohort_artifacts") and not _column_exists(
        conn, "cohort_artifacts", "region_scope"
    ):
        sql = "ALTER TABLE cohort_artifacts ADD COLUMN region_scope TEXT"
        conn.execute(sql)
        executed.append(sql)
    if _table_exists(conn, "cohort_artifact_inputs") and not _column_exists(
        conn, "cohort_artifact_inputs", "role"
    ):
        sql = "ALTER TABLE cohort_artifact_inputs ADD COLUMN role TEXT"
        conn.execute(sql)
        executed.append(sql)
    return executed
```

Then, at the end of `ensure_cohort_artifacts_schema`, before `return executed`, fold the column-ensure in so every schema-ensure path (init, append-cohort, migrate-cohort) also has the 0013 columns:

```python
    if executed:
        for idx_ddl in cohort_artifacts_indexes():
            conn.execute(idx_ddl)
            executed.append(idx_ddl)
    executed.extend(ensure_region_scope_columns(conn))
    return executed
```

(When `executed` is non-empty the tables were just created from the updated DDL, so `ensure_region_scope_columns` finds the columns present and returns `[]`. When the tables already existed pre-0013, `ensure_region_scope_columns` adds them. Either way the post-condition holds.)

Add both names to `__all__`:

```python
    "ensure_cohort_artifacts_schema",
    "ensure_region_scope_columns",
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_region_scope_schema.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Run the existing schema tests to confirm no regression**

Run: `python3 -m pytest tests/test_cohort_artifacts_schema.py -q`
Expected: PASS (the executed-SQL-count assertions still hold — the DDL path adds no extra ALTERs).

- [ ] **Step 7: Commit**

```bash
git add casetrack_qc/cohort_artifacts.py tests/test_region_scope_schema.py
git commit -m "$(printf 'feat(0013): region_scope + input role columns (additive, idempotent)\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

## Task 2: CRUD — persist `region_scope` and per-input `role`

**Files:**
- Modify: `casetrack_qc/cohort_artifacts.py`
- Test: `tests/test_region_scope_crud.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_region_scope_crud.py`:

```python
"""Tests for proposal 0013 CRUD: storing region_scope + input roles.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import casetrack
from casetrack_qc import cohort_artifacts as ca


def _project_with_assays(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    ))
    pt = tmp_path / "p.tsv"
    pt.write_text("patient_id\nP1\n")
    casetrack.cmd_add_metadata(argparse.Namespace(
        project_dir=str(proj), level="patient", metadata=str(pt),
        allow_new=True, yes=True, overwrite=False, key=None))
    sp = tmp_path / "s.tsv"
    sp.write_text("specimen_id\tpatient_id\nS1\tP1\n")
    casetrack.cmd_add_metadata(argparse.Namespace(
        project_dir=str(proj), level="specimen", metadata=str(sp),
        allow_new=True, yes=True, overwrite=False, key=None))
    asy = tmp_path / "a.tsv"
    asy.write_text("assay_id\tspecimen_id\nA_T\tS1\nA_N\tS1\n")
    casetrack.cmd_add_metadata(argparse.Namespace(
        project_dir=str(proj), level="assay", metadata=str(asy),
        allow_new=True, yes=True, overwrite=False, key=None))
    return proj


def test_insert_artifact_stores_region_scope(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            ca.ensure_cohort_artifacts_schema(conn)
            aid = ca.insert_artifact(
                conn, analysis="dss_dmr", run_tag="rt1", path="/x/dmr.bed",
                n_inputs=2, transaction_id="t1", region_scope="promoters_EPDnew")
        art = ca.get_artifact(conn, aid)
        assert art.region_scope == "promoters_EPDnew"
        # default is NULL when omitted
        with casetrack.begin_immediate(conn):
            aid2 = ca.insert_artifact(
                conn, analysis="dss_dmr", run_tag="rt2", path="/x/d2.bed",
                n_inputs=1, transaction_id="t2")
        assert ca.get_artifact(conn, aid2).region_scope is None
    finally:
        conn.close()


def test_add_artifact_inputs_stores_roles(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            ca.ensure_cohort_artifacts_schema(conn)
            aid = ca.insert_artifact(
                conn, analysis="dss_dmr", run_tag="rt1", path="/x/dmr.bed",
                n_inputs=2, transaction_id="t1")
            ca.add_artifact_inputs(
                conn, aid, ["A_T", "A_N"], roles={"A_T": "tumor", "A_N": "normal"})
        assert ca.artifact_inputs(conn, aid) == ["A_N", "A_T"]
        assert ca.artifact_input_roles(conn, aid) == {"A_N": "normal", "A_T": "tumor"}

    finally:
        conn.close()


def test_add_artifact_inputs_without_roles_is_backward_compatible(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            ca.ensure_cohort_artifacts_schema(conn)
            aid = ca.insert_artifact(
                conn, analysis="x", run_tag="rt1", path="/x", n_inputs=1,
                transaction_id="t1")
            ca.add_artifact_inputs(conn, aid, ["A_T"])
        assert ca.artifact_input_roles(conn, aid) == {"A_T": None}
    finally:
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_region_scope_crud.py -v`
Expected: FAIL — `insert_artifact() got an unexpected keyword argument 'region_scope'`.

- [ ] **Step 3: Extend the dataclass, columns, and `insert_artifact`**

In `casetrack_qc/cohort_artifacts.py`, add `region_scope` to the dataclass (place it after `stats_json` to match the DDL column order):

```python
@dataclass
class CohortArtifact:
    artifact_id: int
    analysis: str
    run_tag: str
    path: str
    checksum: str | None
    n_inputs: int
    stats_json: str | None
    region_scope: str | None
    created_at: str
    created_by: str | None
    transaction_id: str

    def to_dict(self) -> dict:
        return asdict(self)


_ARTIFACT_COLS = (
    "artifact_id, analysis, run_tag, path, checksum, n_inputs, "
    "stats_json, region_scope, created_at, created_by, transaction_id"
)
```

Update `insert_artifact` to accept and store `region_scope`:

```python
def insert_artifact(
    conn: sqlite3.Connection,
    *,
    analysis: str,
    run_tag: str,
    path: str,
    n_inputs: int,
    transaction_id: str,
    checksum: str | None = None,
    stats_json: str | None = None,
    region_scope: str | None = None,
    created_by: str | None = None,
    created_at: str | None = None,
) -> int:
    if get_artifact_by_key(conn, analysis, run_tag) is not None:
        raise CohortArtifactError(
            f"cohort artifact already exists for analysis={analysis!r} "
            f"run_tag={run_tag!r}; use a distinct run_tag for a new run"
        )
    if created_at is None:
        created_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    cur = conn.execute(
        """
        INSERT INTO cohort_artifacts
            (analysis, run_tag, path, checksum, n_inputs, stats_json,
             region_scope, created_at, created_by, transaction_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (analysis, run_tag, path, checksum, n_inputs, stats_json,
         region_scope, created_at, created_by, transaction_id),
    )
    return cur.lastrowid
```

- [ ] **Step 4: Extend `add_artifact_inputs` with optional roles + add `artifact_input_roles`**

Replace `add_artifact_inputs` and add the roles reader after `artifact_inputs`:

```python
def add_artifact_inputs(
    conn: sqlite3.Connection,
    artifact_id: int,
    assay_ids: Iterable[str],
    roles: dict[str, str | None] | None = None,
) -> int:
    """Link contributing assays to an artifact. Returns the number inserted.

    ``roles`` optionally maps assay_id → contrast role (e.g. ``"tumor"`` /
    ``"normal"``); a missing key stores NULL (proposal 0013). Validates every
    assay exists first so a typo surfaces as a clear error.
    """
    ids = list(assay_ids)
    roles = roles or {}
    for assay_id in ids:
        row = conn.execute(
            "SELECT 1 FROM assays WHERE assay_id = ?", (assay_id,)
        ).fetchone()
        if row is None:
            raise CohortArtifactError(f"unknown assay {assay_id!r}")
    conn.executemany(
        "INSERT OR IGNORE INTO cohort_artifact_inputs "
        "(artifact_id, assay_id, role) VALUES (?, ?, ?)",
        [(artifact_id, a, roles.get(a)) for a in ids],
    )
    return len(ids)


def artifact_input_roles(
    conn: sqlite3.Connection, artifact_id: int
) -> dict[str, str | None]:
    """Map assay_id → role for one artifact's inputs (role NULL when unset)."""
    rows = conn.execute(
        "SELECT assay_id, role FROM cohort_artifact_inputs "
        "WHERE artifact_id = ? ORDER BY assay_id",
        (artifact_id,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}
```

Add `artifact_input_roles` to `__all__`.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_region_scope_crud.py -v`
Expected: PASS (all three tests).

- [ ] **Step 6: Run the existing cohort CRUD/CLI tests for regression**

Run: `python3 -m pytest tests/test_cohort_artifacts.py tests/test_cohort_artifacts_cli.py -q`
Expected: PASS. (`CohortArtifact` gained a field; the existing tests construct it via the module functions, not positionally, so they remain valid. If any test constructs `CohortArtifact(...)` positionally and fails, that is a real signal — fix that test to pass `region_scope=None`.)

- [ ] **Step 7: Commit**

```bash
git add casetrack_qc/cohort_artifacts.py tests/test_region_scope_crud.py
git commit -m "$(printf 'feat(0013): persist region_scope + per-input roles in CRUD\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

## Task 3: `migrate-region-scope` command + CLI wiring

**Files:**
- Modify: `casetrack_qc/cohort_artifacts_cli.py`, `casetrack_qc/cli.py`
- Test: `tests/test_region_scope_migrate.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_region_scope_migrate.py`:

```python
"""Tests for `casetrack migrate-region-scope` (proposal 0013).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import casetrack
from casetrack_qc.cohort_artifacts_cli import cmd_migrate_region_scope


def _pre0013_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    ))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    with casetrack.begin_immediate(conn):
        # Emulate pre-0013 by dropping the new columns (recreate without them).
        conn.execute("DROP TABLE IF EXISTS cohort_artifact_inputs")
        conn.execute("DROP TABLE IF EXISTS cohort_artifacts")
        conn.execute(
            "CREATE TABLE cohort_artifacts (artifact_id INTEGER PRIMARY KEY "
            "AUTOINCREMENT, analysis TEXT NOT NULL, run_tag TEXT NOT NULL, "
            "path TEXT NOT NULL, checksum TEXT, n_inputs INTEGER NOT NULL, "
            "stats_json TEXT, created_at TEXT NOT NULL, created_by TEXT, "
            "transaction_id TEXT NOT NULL, UNIQUE (analysis, run_tag))"
        )
        conn.execute(
            "CREATE TABLE cohort_artifact_inputs (artifact_id INTEGER NOT NULL, "
            "assay_id TEXT NOT NULL, PRIMARY KEY (artifact_id, assay_id))"
        )
    conn.close()
    return proj


def _cols(proj: Path, table: str) -> set[str]:
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    finally:
        conn.close()


def test_migrate_region_scope_adds_columns(tmp_path: Path, capsys):
    proj = _pre0013_project(tmp_path)
    assert "region_scope" not in _cols(proj, "cohort_artifacts")
    cmd_migrate_region_scope(argparse.Namespace(project_dir=str(proj), dry_run=False))
    assert "region_scope" in _cols(proj, "cohort_artifacts")
    assert "role" in _cols(proj, "cohort_artifact_inputs")


def test_migrate_region_scope_dry_run_changes_nothing(tmp_path: Path, capsys):
    proj = _pre0013_project(tmp_path)
    cmd_migrate_region_scope(argparse.Namespace(project_dir=str(proj), dry_run=True))
    assert "region_scope" not in _cols(proj, "cohort_artifacts")
    assert "dry-run" in capsys.readouterr().out.lower()


def test_migrate_region_scope_is_idempotent(tmp_path: Path, capsys):
    proj = _pre0013_project(tmp_path)
    cmd_migrate_region_scope(argparse.Namespace(project_dir=str(proj), dry_run=False))
    cmd_migrate_region_scope(argparse.Namespace(project_dir=str(proj), dry_run=False))
    out = capsys.readouterr().out.lower()
    assert "no migration needed" in out

    # provenance recorded the migration
    prov = (proj / "provenance.jsonl").read_text().splitlines()
    assert any(json.loads(l).get("action") == "migrate_region_scope" for l in prov)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_region_scope_migrate.py -v`
Expected: FAIL — `ImportError: cannot import name 'cmd_migrate_region_scope'`.

- [ ] **Step 3: Implement `cmd_migrate_region_scope`**

In `casetrack_qc/cohort_artifacts_cli.py`, add after `cmd_migrate_cohort` (and import is already `from casetrack_qc import cohort_artifacts as ca`):

```python
def _region_scope_columns_present(conn) -> bool:
    if not ca.cohort_artifacts_schema_exists(conn):
        return False
    art_cols = {r[1] for r in conn.execute(
        'PRAGMA table_info("cohort_artifacts")').fetchall()}
    in_cols = {r[1] for r in conn.execute(
        'PRAGMA table_info("cohort_artifact_inputs")').fetchall()}
    return "region_scope" in art_cols and "role" in in_cols


def cmd_migrate_region_scope(args) -> None:
    project_dir, _ = casetrack._resolve_project(
        args.project_dir, bypass_legacy_gate=True
    )
    db_path = project_dir / casetrack.PROJECT_DB_NAME

    conn = casetrack.open_project_db(db_path)
    try:
        if not ca.cohort_artifacts_schema_exists(conn):
            print(
                "Error: project has no cohort-artifact schema. Run "
                f"`casetrack migrate-cohort --project-dir {project_dir}` first.",
                file=sys.stderr,
            )
            sys.exit(1)
        if _region_scope_columns_present(conn):
            print("No migration needed — region_scope/role columns already present.")
            return
        if getattr(args, "dry_run", False):
            print(
                "[dry-run] Would add cohort_artifacts.region_scope and "
                "cohort_artifact_inputs.role (additive ALTER TABLE)."
            )
            return
        txn_id = casetrack._new_transaction_id()
        with casetrack.begin_immediate(conn):
            executed = ca.ensure_region_scope_columns(conn)
        casetrack.log_project_provenance(
            project_dir,
            {
                "action": "migrate_region_scope",
                "executed_sql": executed,
                "transaction_id": txn_id,
            },
        )
        print(f"Added region_scope/role columns ({len(executed)} statements).")
    finally:
        conn.close()
```

Update `__all__` in this file:

```python
__all__ = [
    "cmd_append_cohort", "cmd_migrate_cohort", "cmd_cohort_artifacts",
    "cmd_migrate_region_scope",
]
```

- [ ] **Step 4: Wire the parser + dispatch in `casetrack_qc/cli.py`**

Add to the import from `cohort_artifacts_cli`:

```python
from casetrack_qc.cohort_artifacts_cli import (
    cmd_append_cohort,
    cmd_cohort_artifacts,
    cmd_migrate_cohort,
    cmd_migrate_region_scope,
)
```

Add a parser block after the `migrate-cohort` block (after line ~173):

```python
    # ── migrate-region-scope ── (proposal 0013)
    p_migrs = subparsers.add_parser(
        "migrate-region-scope",
        help="[v0.11] Additive: add region_scope/role columns to a pre-0013 project",
    )
    p_migrs.add_argument("--project-dir", required=True)
    p_migrs.add_argument("--dry-run", action="store_true",
                         help="Print the plan, make no changes")
```

Add the dispatch entry in `qc_command_dispatch()`:

```python
        "migrate-region-scope": cmd_migrate_region_scope,
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_region_scope_migrate.py -v`
Expected: PASS (all three tests).

- [ ] **Step 6: Commit**

```bash
git add casetrack_qc/cohort_artifacts_cli.py casetrack_qc/cli.py tests/test_region_scope_migrate.py
git commit -m "$(printf 'feat(0013): migrate-region-scope command + CLI wiring\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

## Task 4: `append-cohort` — `--region-scope`, `assay:role`, reference-resolve

**Files:**
- Modify: `casetrack_qc/cohort_artifacts_cli.py`, `casetrack_qc/cli.py`
- Test: `tests/test_region_scope_append.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_region_scope_append.py`:

```python
"""Tests for `append-cohort` region_scope + roles + reference-resolve (0013).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import casetrack
from casetrack_qc import cohort_artifacts as ca
from casetrack_qc import reference_artifacts as ra
from casetrack_qc.cohort_artifacts_cli import cmd_append_cohort


def _project_with_assays(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    ))
    for level, fn, body in [
        ("patient", "p.tsv", "patient_id\nP1\n"),
        ("specimen", "s.tsv", "specimen_id\tpatient_id\nS1\tP1\n"),
        ("assay", "a.tsv", "assay_id\tspecimen_id\nA_T\tS1\nA_N\tS1\n"),
    ]:
        f = tmp_path / fn
        f.write_text(body)
        casetrack.cmd_add_metadata(argparse.Namespace(
            project_dir=str(proj), level=level, metadata=str(f),
            allow_new=True, yes=True, overwrite=False, key=None))
    return proj


def _append_ns(proj, **kw):
    base = dict(
        project_dir=str(proj), analysis="dss_dmr", run_tag="rt1",
        path="/x/dmr.bed", inputs="A_T,A_N", inputs_from=None, stats=None,
        checksum=None, created_by=None, uses_references=None, derived_from=None,
        region_scope=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_append_stores_region_scope_and_roles(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    cmd_append_cohort(_append_ns(
        proj, region_scope="genome-wide", inputs="A_T:tumor,A_N:normal"))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "dss_dmr", "rt1")
        assert art.region_scope == "genome-wide"
        assert ca.artifact_input_roles(conn, art.artifact_id) == {
            "A_N": "normal", "A_T": "tumor"}
    finally:
        conn.close()


def test_region_scope_matching_ref_key_captures_reference_usage(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            ra.ensure_reference_schema(conn)
            ra.sync_references_from_toml(conn, {
                "promoters_EPDnew": {
                    "path": "/db/prom.bed", "version": "2026-04-14",
                    "kind": "intervals"}})
    finally:
        conn.close()

    cmd_append_cohort(_append_ns(proj, region_scope="promoters_EPDnew"))

    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "dss_dmr", "rt1")
        st = ra.output_staleness(conn, scope="cohort", artifact_id=art.artifact_id)
        assert st["state"] == "fresh"
        # bump the reference version → the artifact becomes ref-stale
        with casetrack.begin_immediate(conn):
            ra.sync_references_from_toml(conn, {
                "promoters_EPDnew": {
                    "path": "/db/prom.bed", "version": "2026-05-01",
                    "kind": "intervals"}})
        st2 = ra.output_staleness(conn, scope="cohort", artifact_id=art.artifact_id)
        assert st2["state"] == "STALE"
        assert any("promoters_EPDnew" in r for r in st2["reasons"])
    finally:
        conn.close()


def test_label_only_scope_captures_no_reference_usage(tmp_path: Path):
    proj = _project_with_assays(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            ra.ensure_reference_schema(conn)  # tables exist but no matching key
    finally:
        conn.close()
    cmd_append_cohort(_append_ns(proj, region_scope="chr17:7565097-7590856"))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "dss_dmr", "rt1")
        st = ra.output_staleness(conn, scope="cohort", artifact_id=art.artifact_id)
        assert st["state"] == "untracked"  # no usage rows captured
    finally:
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_region_scope_append.py -v`
Expected: FAIL — `_append_ns` passes `region_scope=` and `inputs="A_T:tumor,..."`; `_read_inputs` doesn't parse roles and `insert_artifact` isn't called with `region_scope`, so assertions fail.

- [ ] **Step 3: Parse `assay:role` in `_read_inputs`**

In `casetrack_qc/cohort_artifacts_cli.py`, replace `_read_inputs` with a version that returns `(ids, roles)`:

```python
def _read_inputs(args) -> tuple[list[str], dict[str, str | None]]:
    """Collect contributing assay_ids + optional roles.

    ``--inputs`` is comma-separated ``assay_id`` or ``assay_id:role`` items.
    ``--inputs-from`` is a file with one assay_id per line; a leading
    ``assay_id`` header is skipped, the first tab-separated column is the id,
    and a ``role`` column (named in the header) is honored when present.
    Returns ``(ids, roles)`` where ``roles`` maps id → role (or NULL).
    """
    roles: dict[str, str | None] = {}
    inputs = getattr(args, "inputs", None)
    if inputs:
        ids: list[str] = []
        for item in (s.strip() for s in inputs.split(",")):
            if not item:
                continue
            if ":" in item:
                aid, role = item.split(":", 1)
                aid, role = aid.strip(), role.strip() or None
            else:
                aid, role = item, None
            ids.append(aid)
            roles[aid] = role
        return ids, roles
    inputs_from = getattr(args, "inputs_from", None)
    if inputs_from:
        lines = Path(inputs_from).read_text().splitlines()
        ids = []
        role_idx: int | None = None
        for i, line in enumerate(lines):
            s = line.strip()
            if not s:
                continue
            cols = [c.strip() for c in s.split("\t")]
            if i == 0 and cols and cols[0].lower() == "assay_id":
                if "role" in [c.lower() for c in cols]:
                    role_idx = [c.lower() for c in cols].index("role")
                continue
            aid = cols[0]
            ids.append(aid)
            roles[aid] = (
                cols[role_idx] if role_idx is not None and len(cols) > role_idx
                and cols[role_idx] else None
            )
        return ids, roles
    return [], {}
```

- [ ] **Step 4: Use scope + roles + reference-resolve in `cmd_append_cohort`**

In `cmd_append_cohort`, change the inputs unpacking and the insert/link calls, and add the reference-resolve block. Replace the body from `inputs = _read_inputs(args)` through the `add_artifact_inputs` call and the `--uses-references` block with:

```python
    inputs, roles = _read_inputs(args)
    if not inputs:
        print(
            "Error: no contributing assays — pass --inputs a,b,c or "
            "--inputs-from FILE.",
            file=sys.stderr,
        )
        sys.exit(2)
```

…and inside the `with casetrack.begin_immediate(conn):` block:

```python
                ca.ensure_cohort_artifacts_schema(conn)
                art_id = ca.insert_artifact(
                    conn,
                    analysis=args.analysis,
                    run_tag=args.run_tag,
                    path=args.path,
                    n_inputs=len(inputs),
                    transaction_id=txn_id,
                    checksum=checksum,
                    stats_json=stats_json,
                    region_scope=getattr(args, "region_scope", None),
                    created_by=created_by,
                )
                ca.add_artifact_inputs(conn, art_id, inputs, roles=roles)

                # Reference-resolve door (proposal 0013): a region_scope that
                # names a registered ref_key auto-captures a cohort-scope
                # reference_usage edge, so scope changes drive 0010 ref_stale.
                from casetrack_qc import reference_artifacts as _ra
                region_scope = getattr(args, "region_scope", None)
                refs = getattr(args, "uses_references", None)
                ref_keys = [s.strip() for s in (refs or "").split(",") if s.strip()]
                if region_scope or ref_keys:
                    _ra.ensure_reference_schema(conn)
                    current = {r.ref_key: r.version
                               for r in _ra.list_references(conn)}
                    if region_scope and region_scope in current:
                        ref_keys.append(region_scope)
                    for ref_key in dict.fromkeys(ref_keys):  # de-dupe, keep order
                        if ref_key in current:
                            _ra.record_usage(
                                conn, scope="cohort", artifact_id=art_id,
                                ref_key=ref_key, version_used=current[ref_key],
                                transaction_id=txn_id)
```

Leave the existing `--derived-from` block that follows unchanged. Add `"region_scope"` and `"roles"` to the provenance dict:

```python
                "n_inputs": len(inputs),
                "inputs": inputs,
                "region_scope": getattr(args, "region_scope", None),
                "roles": roles,
                "artifact_id": art_id,
```

(`record_usage` does DELETE-then-INSERT keyed on `(artifact_id, ref_key)`, so a panel listed in *both* `--uses-references` and `--region-scope` collapses to one row — idempotency is inherited from 0010, satisfying §5.2 of the spec. The `dict.fromkeys` de-dupe avoids a redundant DELETE/INSERT within the same call.)

- [ ] **Step 5: Add the `--region-scope` argument in `casetrack_qc/cli.py`**

In the `append-cohort` parser block (after the `--derived-from` line, ~line 164), add:

```python
    p_appc.add_argument("--region-scope", dest="region_scope", default=None,
                        help="[v0.11] Genomic scope label for this artifact "
                             "(e.g. genome-wide, promoters_EPDnew, "
                             "chr17:7565097-7590856). A label matching a "
                             "registered reference key auto-tracks ref-staleness.")
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 -m pytest tests/test_region_scope_append.py -v`
Expected: PASS (all three tests).

- [ ] **Step 7: Run existing append-cohort tests for regression**

Run: `python3 -m pytest tests/test_cohort_artifacts_cli.py tests/test_reference_artifacts_cli.py -q`
Expected: PASS. (`_read_inputs` now returns a tuple; the only caller is `cmd_append_cohort`, already updated. If any test calls `_read_inputs` directly and breaks, update it to unpack the tuple.)

- [ ] **Step 8: Commit**

```bash
git add casetrack_qc/cohort_artifacts_cli.py casetrack_qc/cli.py tests/test_region_scope_append.py
git commit -m "$(printf 'feat(0013): append-cohort --region-scope, assay:role inputs, reference-resolve\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

## Task 5: `cohort-artifacts` — show scope + `--scope` filter

**Files:**
- Modify: `casetrack_qc/cohort_artifacts_cli.py`, `casetrack_qc/cli.py`
- Test: `tests/test_region_scope_listcmd.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_region_scope_listcmd.py`:

```python
"""Tests for `cohort-artifacts` region_scope display + --scope filter (0013).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import casetrack
from casetrack_qc.cohort_artifacts_cli import cmd_append_cohort, cmd_cohort_artifacts


def _project_with_two_scoped_artifacts(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    ))
    for level, fn, body in [
        ("patient", "p.tsv", "patient_id\nP1\n"),
        ("specimen", "s.tsv", "specimen_id\tpatient_id\nS1\tP1\n"),
        ("assay", "a.tsv", "assay_id\tspecimen_id\nA1\tS1\n"),
    ]:
        f = tmp_path / fn
        f.write_text(body)
        casetrack.cmd_add_metadata(argparse.Namespace(
            project_dir=str(proj), level=level, metadata=str(f),
            allow_new=True, yes=True, overwrite=False, key=None))

    def _ns(**kw):
        base = dict(project_dir=str(proj), path="/x", inputs="A1",
                    inputs_from=None, stats=None, checksum=None, created_by=None,
                    uses_references=None, derived_from=None, region_scope=None)
        base.update(kw)
        return argparse.Namespace(**base)

    cmd_append_cohort(_ns(analysis="dss_dmr", run_tag="gw", region_scope="genome-wide"))
    cmd_append_cohort(_ns(analysis="dss_dmr", run_tag="prom",
                          region_scope="promoters_EPDnew"))
    return proj


def test_json_output_includes_region_scope(tmp_path: Path, capsys):
    proj = _project_with_two_scoped_artifacts(tmp_path)
    cmd_cohort_artifacts(argparse.Namespace(
        project_dir=str(proj), fmt="json", stale_only=False, scope=None))
    rows = json.loads(capsys.readouterr().out)
    scopes = {r["run_tag"]: r["region_scope"] for r in rows}
    assert scopes == {"gw": "genome-wide", "prom": "promoters_EPDnew"}


def test_scope_filter_narrows_rows(tmp_path: Path, capsys):
    proj = _project_with_two_scoped_artifacts(tmp_path)
    cmd_cohort_artifacts(argparse.Namespace(
        project_dir=str(proj), fmt="json", stale_only=False,
        scope="promoters_EPDnew"))
    rows = json.loads(capsys.readouterr().out)
    assert [r["run_tag"] for r in rows] == ["prom"]


def test_table_output_shows_scope(tmp_path: Path, capsys):
    proj = _project_with_two_scoped_artifacts(tmp_path)
    cmd_cohort_artifacts(argparse.Namespace(
        project_dir=str(proj), fmt="table", stale_only=False, scope=None))
    out = capsys.readouterr().out
    assert "genome-wide" in out and "promoters_EPDnew" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_region_scope_listcmd.py -v`
Expected: FAIL — rows have no `region_scope` key and `cmd_cohort_artifacts` ignores `scope`.

- [ ] **Step 3: Add `region_scope` to the row dict + the `--scope` filter**

In `casetrack_qc/cohort_artifacts_cli.py`, in `_artifact_rows`, add `region_scope` to each row dict:

```python
        rows.append(
            {
                "artifact_id": art.artifact_id,
                "analysis": art.analysis,
                "run_tag": art.run_tag,
                "path": art.path,
                "n_inputs": art.n_inputs,
                "region_scope": art.region_scope,
                "stale": len(censored) > 0,
                "n_censored_inputs": len(censored),
                "censored_inputs": censored,
            }
        )
```

In `cmd_cohort_artifacts`, after computing `rows = _artifact_rows(conn)` and before the `stale_only` filter, add the scope filter:

```python
        rows = _artifact_rows(conn)
        scope = getattr(args, "scope", None)
        if scope:
            rows = [r for r in rows if r["region_scope"] == scope]
        if getattr(args, "stale_only", False):
            rows = [r for r in rows if r["stale"]]
```

In the `tsv` branch, add `region_scope` to `cols`:

```python
            cols = ["artifact_id", "analysis", "run_tag", "region_scope",
                    "n_inputs", "stale", "n_censored_inputs", "path"]
```

In the `table` branch, append scope to the per-row line when set:

```python
            for r in rows:
                flag = "STALE" if r["stale"] else "fresh"
                line = (
                    f"[{flag}] {r['analysis']}/{r['run_tag']}  "
                    f"id={r['artifact_id']}  inputs={r['n_inputs']}"
                )
                if r["region_scope"]:
                    line += f"  scope={r['region_scope']}"
                if r["stale"]:
                    line += (
                        f"  censored={r['n_censored_inputs']} "
                        f"({', '.join(r['censored_inputs'])})"
                    )
                print(line)
```

- [ ] **Step 4: Add the `--scope` argument in `casetrack_qc/cli.py`**

In the `cohort-artifacts` parser block (after `--stale-only`, ~line 184), add:

```python
    p_calist.add_argument("--scope", default=None,
                         help="[v0.11] Show only artifacts with this region_scope label")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_region_scope_listcmd.py -v`
Expected: PASS (all three tests).

- [ ] **Step 6: Commit**

```bash
git add casetrack_qc/cohort_artifacts_cli.py casetrack_qc/cli.py tests/test_region_scope_listcmd.py
git commit -m "$(printf 'feat(0013): cohort-artifacts shows region_scope + --scope filter\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

## Task 6: `_cohort_artifacts` DuckDB view — `region_scope` + `scope_ref_key`

**Files:**
- Modify: `casetrack_qc/reader.py`
- Test: `tests/test_region_scope_view.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_region_scope_view.py`:

```python
"""Tests for the _cohort_artifacts view region_scope + scope_ref_key (0013).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import casetrack
from casetrack_qc import reference_artifacts as ra
from casetrack_qc.cohort_artifacts_cli import cmd_append_cohort


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    ))
    for level, fn, body in [
        ("patient", "p.tsv", "patient_id\nP1\n"),
        ("specimen", "s.tsv", "specimen_id\tpatient_id\nS1\tP1\n"),
        ("assay", "a.tsv", "assay_id\tspecimen_id\nA1\tS1\n"),
    ]:
        f = tmp_path / fn
        f.write_text(body)
        casetrack.cmd_add_metadata(argparse.Namespace(
            project_dir=str(proj), level=level, metadata=str(f),
            allow_new=True, yes=True, overwrite=False, key=None))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    with casetrack.begin_immediate(conn):
        ra.ensure_reference_schema(conn)
        ra.sync_references_from_toml(conn, {
            "promoters_EPDnew": {"path": "/db/p.bed", "version": "v1",
                                 "kind": "intervals"}})
    conn.close()

    def _ns(**kw):
        base = dict(project_dir=str(proj), path="/x", inputs="A1",
                    inputs_from=None, stats=None, checksum=None, created_by=None,
                    uses_references=None, derived_from=None, region_scope=None)
        base.update(kw)
        return argparse.Namespace(**base)

    cmd_append_cohort(_ns(analysis="dss_dmr", run_tag="prom",
                          region_scope="promoters_EPDnew"))
    cmd_append_cohort(_ns(analysis="dss_dmr", run_tag="gw",
                          region_scope="genome-wide"))
    return proj


def test_view_exposes_region_scope_and_scope_ref_key(tmp_path: Path):
    proj = _project(tmp_path)
    rows = casetrack.run_project_query(
        proj,
        "SELECT run_tag, region_scope, scope_ref_key FROM _cohort_artifacts "
        "ORDER BY run_tag",
    )
    # rows: list of dict-like; normalize to (run_tag -> (scope, ref_key))
    got = {r["run_tag"]: (r["region_scope"], r["scope_ref_key"]) for r in rows}
    assert got["prom"] == ("promoters_EPDnew", "promoters_EPDnew")
    assert got["gw"] == ("genome-wide", None)  # label-only → no ref resolution
```

> **Note for implementer:** `casetrack.run_project_query` is the helper the existing
> `test_cohort_artifacts_readpaths.py` uses to run a DuckDB query against a project and
> get dict rows. **Before writing this test, open `tests/test_cohort_artifacts_readpaths.py`
> and copy its exact query-helper invocation** (function name + signature may differ —
> e.g. it may be a fixture or `casetrack._run_duckdb_query`). Match that pattern rather
> than assuming `run_project_query`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_region_scope_view.py -v`
Expected: FAIL — `Binder Error: Referenced column "region_scope" not found` (view doesn't select it yet) or `scope_ref_key` not found.

- [ ] **Step 3: Add presence-guarded `region_scope` + `scope_ref_key` to all view tiers**

In `casetrack_qc/reader.py`, in `install_cohort_artifact_view`, just before the three `sql_*` definitions, compute a scope expression guarded by column presence (so pre-0013 DBs don't break the view):

```python
    # Proposal 0013: region_scope (cohort_artifacts column) + derived
    # scope_ref_key (the ref_key it resolves to, NULL when label-only).
    # Column-presence-guarded so pre-0013 projects keep a working view.
    try:
        have_scope = duckdb_con.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'cohort_artifacts' "
            "AND column_name = 'region_scope'"
        ).fetchone() is not None
    except Exception:
        have_scope = False
    scope_expr = "ca.region_scope" if have_scope else "CAST(NULL AS VARCHAR)"
    scope_ref_key_expr = (
        "(SELECT rr.ref_key FROM proj.reference_artifacts rr "
        f"WHERE rr.ref_key = {scope_expr})"
    )
```

Then add the two columns to each SQL variant.

In `sql_with_ref_stale_and_derived`, change the SELECT list to include them (insert after `ca.created_at,`):

```python
    sql_with_ref_stale_and_derived = f"""
        CREATE VIEW "_cohort_artifacts" AS
        WITH RECURSIVE {_derived_stale_cte(self_safe=True)}
        SELECT ca.artifact_id, ca.analysis, ca.run_tag, ca.path, ca.checksum,
               ca.n_inputs, ca.stats_json, ca.created_at,
               {scope_expr} AS region_scope,
               {scope_ref_key_expr} AS scope_ref_key,
               {censored_count} AS n_censored_inputs,
               ({censored_count} > 0) AS stale,
               {ref_stale_subquery} AS ref_stale,
               COALESCE(d.derived_stale, FALSE) AS derived_stale
        FROM proj.cohort_artifacts ca
        LEFT JOIN derived d
               ON d.node = 'cohort:' || ca.analysis || '@' || ca.run_tag
    """
```

In `sql_with_ref_stale_only` (reference tables present, no 0011), same two columns — `scope_ref_key_expr` is valid here because `reference_artifacts` exists:

```python
    sql_with_ref_stale_only = f"""
        CREATE VIEW "_cohort_artifacts" AS
        SELECT ca.artifact_id, ca.analysis, ca.run_tag, ca.path, ca.checksum,
               ca.n_inputs, ca.stats_json, ca.created_at,
               {scope_expr} AS region_scope,
               {scope_ref_key_expr} AS scope_ref_key,
               {censored_count} AS n_censored_inputs,
               ({censored_count} > 0) AS stale,
               {ref_stale_subquery} AS ref_stale
        FROM proj.cohort_artifacts ca
    """
```

In `sql_without_ref_stale` (pre-0010: no `reference_artifacts` table), include `region_scope` but **not** `scope_ref_key` (the subquery would reference a missing table). Emit a typed NULL so the column still exists:

```python
    sql_without_ref_stale = f"""
        CREATE VIEW "_cohort_artifacts" AS
        SELECT ca.artifact_id, ca.analysis, ca.run_tag, ca.path, ca.checksum,
               ca.n_inputs, ca.stats_json, ca.created_at,
               {scope_expr} AS region_scope,
               CAST(NULL AS VARCHAR) AS scope_ref_key,
               {censored_count} AS n_censored_inputs,
               ({censored_count} > 0) AS stale
        FROM proj.cohort_artifacts ca
    """
```

The existing try/except tier-fallback is unchanged — it already degrades gracefully if a tier's SQL fails.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_region_scope_view.py -v`
Expected: PASS.

- [ ] **Step 5: Run the existing read-path tests for regression**

Run: `python3 -m pytest tests/test_cohort_artifacts_readpaths.py tests/test_reference_artifacts_readpaths.py tests/test_artifact_derivation_readpaths.py -q`
Expected: PASS — the view gained two columns; existing queries select named columns and are unaffected.

- [ ] **Step 6: Commit**

```bash
git add casetrack_qc/reader.py tests/test_region_scope_view.py
git commit -m "$(printf 'feat(0013): _cohort_artifacts view exposes region_scope + scope_ref_key\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

## Task 7: Surface scope in `status`, dashboard, MCP

**Files:**
- Modify: `casetrack.py` (status section, dashboard html + its info dict), `casetrack_mcp/tools.py`
- Test: `tests/test_region_scope_surfaces.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_region_scope_surfaces.py`:

```python
"""Tests that region_scope surfaces in status, dashboard, and MCP (0013).

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-05-22
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import casetrack
from casetrack_qc.cohort_artifacts_cli import cmd_append_cohort


def _scoped_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    ))
    for level, fn, body in [
        ("patient", "p.tsv", "patient_id\nP1\n"),
        ("specimen", "s.tsv", "specimen_id\tpatient_id\nS1\tP1\n"),
        ("assay", "a.tsv", "assay_id\tspecimen_id\nA1\tS1\n"),
    ]:
        f = tmp_path / fn
        f.write_text(body)
        casetrack.cmd_add_metadata(argparse.Namespace(
            project_dir=str(proj), level=level, metadata=str(f),
            allow_new=True, yes=True, overwrite=False, key=None))
    cmd_append_cohort(argparse.Namespace(
        project_dir=str(proj), analysis="dss_dmr", run_tag="gw", path="/x",
        inputs="A1", inputs_from=None, stats=None, checksum=None,
        created_by=None, uses_references=None, derived_from=None,
        region_scope="genome-wide"))
    return proj


def test_status_section_shows_scope(tmp_path: Path):
    proj = _scoped_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            casetrack._emit_cohort_artifacts_section(conn)
        assert "genome-wide" in buf.getvalue()
    finally:
        conn.close()


def test_mcp_tool_includes_region_scope(tmp_path: Path, monkeypatch):
    proj = _scoped_project(tmp_path)
    # Resolve the project by path through the MCP helper. The readpaths MCP
    # tests register the project first; copy that registration pattern here.
    from casetrack_mcp import tools
    # See tests/test_cohort_artifacts_readpaths.py (or test_*_mcp*.py) for the
    # exact registration/resolution fixture — reuse it to get a project_id.
    pid = casetrack.derive_project_id_from_dir(proj)  # adjust to real helper
    payload = tools.cohort_artifacts_tool(pid)
    assert payload["artifacts"][0]["region_scope"] == "genome-wide"
```

> **Note for implementer:** the MCP test needs a registered/resolvable `project_id`.
> Open `tests/` for the existing MCP test (grep `cohort_artifacts_tool` under `tests/`)
> and reuse its exact project-registration fixture and resolution call. If MCP tests
> are environment-gated/skipped in this repo, mark this one `@pytest.mark.skipif`
> consistently with the others and rely on the `status` assertion as the primary check.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_region_scope_surfaces.py::test_status_section_shows_scope -v`
Expected: FAIL — the status section line has no scope.

- [ ] **Step 3: Add scope to the status section**

In `casetrack.py`, in `_emit_cohort_artifacts_section`, append scope to the per-artifact line:

```python
    for a in arts:
        censored = stale_map.get(a.artifact_id, [])
        flag = "STALE" if censored else "fresh"
        line = f"  [{flag}] {a.analysis}/{a.run_tag}  inputs={a.n_inputs}"
        if a.region_scope:
            line += f"  scope={a.region_scope}"
        if censored:
            line += f"  censored: {', '.join(censored)}"
        print(line)
```

- [ ] **Step 4: Add scope to the dashboard info dict + html**

Find the dashboard info-dict construction (around `casetrack.py:5716`, `qc_info["cohort_artifacts"] = [ ... ]`). Add `region_scope` to each dict built there:

```python
            qc_info["cohort_artifacts"] = [
                {
                    "analysis": a.analysis,
                    "run_tag": a.run_tag,
                    "n_inputs": a.n_inputs,
                    "region_scope": a.region_scope,
                    "stale": bool(_stale_map.get(a.artifact_id)),
                    "censored": _stale_map.get(a.artifact_id, []),
                }
                for a in _list_artifacts(conn)
            ]
```

> **Note for implementer:** match the exact existing dict keys/loop at that line — the
> snippet above shows the *shape*; preserve whatever keys are already there and just add
> `"region_scope": a.region_scope`.

In `_cohort_artifacts_html` (around `casetrack.py:6021`), add a scope column to the table. Add the cell in the row builder and the header:

```python
        rows.append(
            "<tr>"
            f"<td>{esc(a['analysis'])}</td>"
            f"<td class='id'>{esc(a['run_tag'])}</td>"
            f"<td class='id'>{esc(a.get('region_scope') or '')}</td>"
            f"<td>{a['n_inputs']}</td>"
            f"<td>{badge}</td>"
            f"<td class='id'>{detail}</td>"
            "</tr>"
        )
```

and the header row:

```python
        "<th>analysis</th><th>run_tag</th><th>scope</th><th>inputs</th>"
        "<th>status</th><th>censored inputs</th>"
```

- [ ] **Step 5: Add `region_scope` to the MCP tool**

In `casetrack_mcp/tools.py`, in `cohort_artifacts_tool`, add `region_scope` to each appended dict:

```python
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
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 -m pytest tests/test_region_scope_surfaces.py -v`
Expected: PASS (status test passes; MCP test passes or is skipped consistently with sibling MCP tests).

- [ ] **Step 7: Verify export carries scope automatically**

The export path reads `SELECT * FROM cohort_artifacts` (casetrack.py ~6511), so `region_scope` rides along with no code change. Confirm:

Run: `python3 -m pytest tests/ -q -k "export and cohort"`
Expected: PASS. If an export test asserts an exact column list for the cohort_artifacts sheet, update that expected list to include `region_scope`.

- [ ] **Step 8: Commit**

```bash
git add casetrack.py casetrack_mcp/tools.py tests/test_region_scope_surfaces.py
git commit -m "$(printf 'feat(0013): surface region_scope in status, dashboard, MCP\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

## Task 8: Full suite, version bump, docs

**Files:**
- Modify: `setup.py`, `casetrack.py` (`_CASETRACK_VERSION`), `CHANGELOG.md`, `README.md`, `CLAUDE.md`, `docs/proposals/0013-region-scoped-artifacts.md` (status → Accepted), the casetrack skill SKILL.md
- Test: full suite

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS — all prior tests plus the new `test_region_scope_*` modules. Investigate and fix any failure before proceeding; do not bump the version over a red suite.

- [ ] **Step 2: Bump the version in both locations**

Per the project's version-locations note, the version lives in `setup.py`, `casetrack.py`, CLI help strings (already tagged `[v0.11]` above), and CHANGELOG/tags.

Edit `setup.py`:

```python
    version="0.11.0",
```

Edit `casetrack.py`:

```python
_CASETRACK_VERSION = "0.11.0"
```

- [ ] **Step 3: Add the CHANGELOG entry**

Prepend a section to `CHANGELOG.md` (match the existing heading style):

```markdown
## v0.11.0 — region-scoped artifacts + contrast roles (proposal 0013)

### Added
- `cohort_artifacts.region_scope` (nullable) — a genomic scope label on each
  cohort artifact (`genome-wide`, a panel key, or a raw `chr:start-end`).
- `cohort_artifact_inputs.role` (nullable) — contrast role per input
  (`tumor` / `normal` / …); descriptive, not staleness-bearing.
- **Reference-resolve door**: a `region_scope` that matches a registered
  reference key (0010) auto-captures a cohort-scope `reference_usage` edge, so
  scope-version changes drive the existing `ref_stale` flag — no new staleness
  code.
- `append-cohort --region-scope`; `--inputs assay:role` and a `role` column in
  `--inputs-from`.
- `cohort-artifacts --scope <label>` filter; `region_scope` in table/tsv/json.
- `migrate-region-scope` — additive ALTER for pre-0013 projects.
- `_cohort_artifacts` DuckDB view gains `region_scope` + derived `scope_ref_key`.
- `region_scope` surfaced in `status`, the HTML dashboard, the
  `casetrack_cohort_artifacts` MCP tool, and `export`.

### Notes
- Fully additive and backward-compatible: pre-0013 artifacts read as
  `region_scope = NULL` / inputs `role = NULL`.
- Deferred (proposal 0013 §7): per-region findings store, interval/overlap
  queries, scope on sample-level analyses (A2) and any-node scope (A3).
```

- [ ] **Step 4: Update README command table + CLAUDE.md**

In `README.md`, add `region-scope` entries to the cohort-artifacts command rows (match the existing table format): document `append-cohort --region-scope`, `cohort-artifacts --scope`, and `migrate-region-scope`.

In `CLAUDE.md`, add a row to the Commands table:

```markdown
| region-scoped artifacts (0013) | `migrate-region-scope` — add `region_scope`/`role` columns; `append-cohort --region-scope` + `--inputs assay:role`; `cohort-artifacts --scope` |
```

Update the "Current release" line to `v0.11.0 — region-scoped artifacts + contrast roles (proposal 0013)` and add a one-line entry to the proposals key-context table pointing at `docs/proposals/0013-region-scoped-artifacts.md`.

- [ ] **Step 5: Flip the proposal status to Accepted/Shipped**

In `docs/proposals/0013-region-scoped-artifacts.md`, change the header `| **Status** | **Draft** |` to `| **Status** | **Shipped (v0.11.0)** |` and set the target release to `v0.11.0`.

- [ ] **Step 6: Update the casetrack skill**

In the casetrack skill `SKILL.md` (`/home/ahunos/.claude/skills/casetrack/SKILL.md`), add a `region-scope` row to the §1 command table and a short subsection (mirroring the 0009/0010 sections) covering `--region-scope`, `assay:role`, the reference-resolve behavior, and `migrate-region-scope`. This keeps future sessions accurate.

- [ ] **Step 7: Re-run the full suite once more**

Run: `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add setup.py casetrack.py CHANGELOG.md README.md CLAUDE.md docs/proposals/0013-region-scoped-artifacts.md
git commit -m "$(printf 'release: v0.11.0 — region-scoped artifacts + contrast roles (0013)\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
git add /home/ahunos/.claude/skills/casetrack/SKILL.md 2>/dev/null || true
git commit -m "$(printf 'docs(skill): document region-scoped artifacts (0013)\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')" 2>/dev/null || true
```

> The skill file lives outside the repo; if `git add` of it fails, that's expected — edit it in place and skip its commit.

- [ ] **Step 9: Push and update the PR**

```bash
git push
```

The branch already has PR #23 open; the new commits update it. Add a comment summarizing that the implementation landed:

```bash
gh pr comment 23 --body "Implementation of 0013 landed: region_scope + role columns, reference-resolve auto-capture, migrate-region-scope, full read-path surfacing, v0.11.0. All tests green."
```

---

## Self-Review

**Spec coverage** (against `docs/proposals/0013-region-scoped-artifacts.md`):

| Spec item | Task |
|---|---|
| §5.1 `region_scope` column (nullable) | Task 1, 2 |
| §5.1 `role` column (nullable) | Task 1, 2 |
| §5.1 grouping index on `region_scope` | **Gap — see note below** |
| §5.2 reference-resolve auto-captures `reference_usage` | Task 4 |
| §5.2 label-only scope = no staleness | Task 4 (`test_label_only_scope...`) |
| §5.2 idempotency vs `uses` | Task 4 (inherited from 0010 `record_usage`; `dict.fromkeys` de-dupe) |
| §5.2 three orthogonal flags preserved | Task 6 (view keeps `stale`/`ref_stale`/`derived_stale`) |
| §5.3 `--region-scope`, `assay:role`, `--inputs-from` role col | Task 4 |
| §5.3 `--scope` filter, `migrate-region-scope` | Task 3, 5 |
| §5.4 `_cohort_artifacts` view: `region_scope` + `scope_ref_key` | Task 6 |
| §5.4 status / export / dashboard / MCP | Task 7 |
| §5.4 validate: no new invariant | No task needed (explicitly none) |
| §3 `migrate-region-scope` | Task 3 |

**Gap found — §5.1 index:** the spec lists `CREATE INDEX idx_cohort_artifacts_scope ON cohort_artifacts(region_scope)`. Add it to `ensure_region_scope_columns` in Task 1 Step 4 so it's created alongside the column:

```python
    # after adding the region_scope column, create the grouping index:
    if executed and _table_exists(conn, "cohort_artifacts"):
        idx = ("CREATE INDEX IF NOT EXISTS idx_cohort_artifacts_scope "
               "ON cohort_artifacts(region_scope)")
        conn.execute(idx)
        executed.append(idx)
```

Place this just before `return executed` in `ensure_region_scope_columns`. (`IF NOT EXISTS` keeps it idempotent for the fresh-DDL path where the column already existed but the index didn't.) Update the Task 1 test `test_ensure_region_scope_columns_is_idempotent_and_additive` to also assert the index exists via `SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_cohort_artifacts_scope'`.

**Placeholder scan:** no TBD/TODO in implementation steps. Two implementer notes (Task 6 query helper, Task 7 MCP fixture) point to existing test files to copy exact helper names — these are deliberate "match the codebase" instructions, not placeholders, because the precise helper name must be read from the repo at implementation time rather than guessed.

**Type consistency:** `region_scope` is the column/attr/arg name everywhere (DDL, dataclass, `insert_artifact(region_scope=...)`, row dicts, view, status, MCP). `role`/`roles` consistent (`add_artifact_inputs(..., roles=dict)`, `artifact_input_roles() -> dict`). `ensure_region_scope_columns` / `cmd_migrate_region_scope` / `migrate-region-scope` consistent across module, CLI, dispatch. `scope_ref_key` consistent in view + test. `_read_inputs` returns `(ids, roles)` and its sole caller (`cmd_append_cohort`) unpacks the tuple.
