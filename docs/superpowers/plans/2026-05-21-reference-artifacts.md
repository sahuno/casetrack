# Reference Artifacts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class, per-file versioned reference artifacts (genome, annotation, known-variant sets) declared in a TOML `[references]` block, with read-time downstream staleness when a reference version changes — implementing proposal 0010.

**Architecture:** Two additive sibling tables mirroring the proposal-0009 pattern (`reference_artifacts` = canonical set materialized from TOML; `reference_usage` = edges recording which output used which ref at which version). Staleness is derived live at read time (three states: `fresh`/`STALE`/`untracked`), kept orthogonal to 0009's input-staleness. The three-level core and the 0009 tables are untouched.

**Tech Stack:** Python 3.10+, sqlite3 (WAL + FK), tomllib/tomli for TOML, DuckDB for query views, pytest. New code lives in `casetrack_qc/` (the subpackage alongside `casetrack.py`), mirroring `casetrack_qc/cohort_artifacts.py`.

**Reference implementation to mirror:** `casetrack_qc/cohort_artifacts.py` (module), `casetrack_qc/cohort_artifacts_cli.py` (CLI), `casetrack_qc/cli.py` (wiring), `tests/test_cohort_artifacts*.py` (tests). Read these first.

**Spec:** `docs/proposals/0010-reference-artifacts.md`. Read §6 (design), §6.2 (staleness), §6.1 (schema) before starting.

**Conventions in this codebase (do not violate):**
- Module functions take an open `sqlite3.Connection`; the **command layer** owns the transaction (`with casetrack.begin_immediate(conn): ...`) and writes provenance, so the row + provenance land together.
- All DDL is idempotent (guarded by `_table_exists`).
- Friendly errors (raise a typed exception) instead of letting raw `IntegrityError` surface.
- TOML is the contract; DB tables are a cached materialization synced on `schema apply`.
- Run the full suite with `python3 -m pytest tests/ -q` (~2 min). Per-test runs use `pytest tests/test_x.py::test_y -v`.

---

## File Structure

**Create:**
- `casetrack_qc/reference_artifacts.py` — DDL, introspection, dataclasses, CRUD, read-time staleness. Mirrors `cohort_artifacts.py`.
- `casetrack_qc/reference_artifacts_cli.py` — `cmd_migrate_references`, `cmd_references`, and the `capture_reference_usage` helper that `append`/`append-cohort` call.
- `tests/test_reference_artifacts_schema.py` — DDL + TOML `[references]`/`uses` parse & validation.
- `tests/test_reference_artifacts.py` — CRUD + three-state staleness derivation.
- `tests/test_reference_artifacts_cli.py` — `migrate-references`, `references`, capture-on-append.
- `tests/test_reference_artifacts_readpaths.py` — status / query view / export / validate / dashboard / MCP.

**Modify:**
- `casetrack.py` — `_validate_analyses` (add `uses`), new `_validate_references`, init hook (~line 1517), `schema apply` sync, `cmd_append_project` capture hook, query views, status section, export flag, dashboard section, validate invariants, append argparse flags.
- `casetrack_qc/cli.py` — register `migrate-references` + `references` subparsers and dispatch.
- `casetrack_mcp/server.py` + `casetrack_mcp/tools.py` — `casetrack_references` tool.
- `examples/nextflow/casetrack.nf` + `examples/nextflow/subworkflows/local/cohort_artifact_tracked.nf` — optional `uses_references`.
- `README.md`, `CLAUDE.md`, `.claude/skills/casetrack/SKILL.md` — docs.

---

## Task 1: Schema DDL + introspection

**Files:**
- Create: `casetrack_qc/reference_artifacts.py`
- Test: `tests/test_reference_artifacts_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reference_artifacts_schema.py
import sqlite3
import pytest
from casetrack_qc import reference_artifacts as ra


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    # minimal three-level + cohort_artifacts so FKs resolve
    conn.execute("CREATE TABLE assays (assay_id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE cohort_artifacts (artifact_id INTEGER PRIMARY KEY AUTOINCREMENT)"
    )
    return conn


def test_ensure_schema_is_idempotent_and_creates_both_tables():
    conn = _conn()
    first = ra.ensure_reference_schema(conn)
    assert ra.reference_schema_exists(conn) is True
    assert any("reference_artifacts" in s for s in first)
    assert any("reference_usage" in s for s in first)
    # second call is a no-op
    second = ra.ensure_reference_schema(conn)
    assert second == []


def test_reference_schema_exists_false_when_absent():
    conn = _conn()
    assert ra.reference_schema_exists(conn) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'casetrack_qc.reference_artifacts'`

- [ ] **Step 3: Write minimal implementation**

```python
# casetrack_qc/reference_artifacts.py
"""Reference artifacts: schema, CRUD, and read-time staleness.

Proposal 0010. A reference artifact is a versioned, per-file external input
(genome, annotation, known-variant set, …) declared in the TOML [references]
block and materialized here. ``reference_usage`` records which output consumed
which reference at which version; staleness is derived live at read time when
a recorded version no longer matches the current canonical version.

Mirrors casetrack_qc/cohort_artifacts.py: module functions take an open conn,
the command layer owns the transaction + provenance, all DDL is idempotent.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import datetime
import sqlite3
from dataclasses import asdict, dataclass

TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S"

REFERENCE_KINDS = (
    "genome", "annotation", "known_variants", "repeats", "intervals", "other",
)


def reference_artifacts_ddl() -> str:
    return (
        "CREATE TABLE reference_artifacts (\n"
        "    ref_key    TEXT PRIMARY KEY,\n"
        "    path       TEXT NOT NULL,\n"
        "    version    TEXT NOT NULL,\n"
        "    kind       TEXT,\n"
        "    checksum   TEXT,\n"
        "    updated_at TEXT NOT NULL\n"
        ")"
    )


def reference_usage_ddl() -> str:
    return (
        "CREATE TABLE reference_usage (\n"
        "    usage_id       INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    scope          TEXT NOT NULL,\n"
        "    entity_level   TEXT,\n"
        "    entity_id      TEXT,\n"
        "    analysis       TEXT,\n"
        "    artifact_id    INTEGER REFERENCES cohort_artifacts(artifact_id) "
        "ON DELETE CASCADE,\n"
        "    ref_key        TEXT NOT NULL,\n"
        "    version_used   TEXT NOT NULL,\n"
        "    recorded_at    TEXT NOT NULL,\n"
        "    transaction_id TEXT\n"
        ")"
    )


def reference_usage_indexes() -> list[str]:
    return [
        "CREATE UNIQUE INDEX idx_refusage_analysis ON reference_usage("
        "entity_level, entity_id, analysis, ref_key) WHERE scope = 'analysis'",
        "CREATE UNIQUE INDEX idx_refusage_cohort ON reference_usage("
        "artifact_id, ref_key) WHERE scope = 'cohort'",
        "CREATE INDEX idx_refusage_refkey ON reference_usage(ref_key)",
    ]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def reference_schema_exists(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "reference_artifacts") and _table_exists(
        conn, "reference_usage"
    )


def ensure_reference_schema(conn: sqlite3.Connection) -> list[str]:
    """Create any missing reference objects. Idempotent. Returns executed SQL."""
    executed: list[str] = []
    if not _table_exists(conn, "reference_artifacts"):
        ddl = reference_artifacts_ddl()
        conn.execute(ddl)
        executed.append(ddl)
    if not _table_exists(conn, "reference_usage"):
        ddl = reference_usage_ddl()
        conn.execute(ddl)
        executed.append(ddl)
    if executed:
        for idx in reference_usage_indexes():
            conn.execute(idx)
            executed.append(idx)
    return executed


__all__ = [
    "TIMESTAMP_FMT", "REFERENCE_KINDS",
    "reference_artifacts_ddl", "reference_usage_ddl", "reference_usage_indexes",
    "reference_schema_exists", "ensure_reference_schema",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts_schema.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add casetrack_qc/reference_artifacts.py tests/test_reference_artifacts_schema.py
git commit -m "feat(refs): reference_artifacts + reference_usage schema (0010 §6.1)"
```

---

## Task 2: TOML `[references]` + `[analyses].uses` validation

**Files:**
- Modify: `casetrack.py` — `_validate_analyses` (line 500–533), add `_validate_references`, call it from the schema validator.
- Test: `tests/test_reference_artifacts_schema.py` (append)

- [ ] **Step 1: Write the failing test (append to the file)**

```python
import casetrack


def test_validate_references_accepts_well_formed_block():
    refs = {
        "genome": {"path": "/db/hg38.fa", "version": "hg38_v0", "kind": "genome"},
        "dbsnp": {"path": "/db/dbsnp.vcf.gz", "version": "b156",
                  "kind": "known_variants"},
    }
    casetrack._validate_references(refs)  # no raise


def test_validate_references_requires_path_and_version():
    with pytest.raises(casetrack.SchemaError):
        casetrack._validate_references({"genome": {"version": "hg38_v0"}})
    with pytest.raises(casetrack.SchemaError):
        casetrack._validate_references({"genome": {"path": "/db/hg38.fa"}})


def test_validate_references_rejects_bad_kind_and_key():
    with pytest.raises(casetrack.SchemaError):
        casetrack._validate_references(
            {"genome": {"path": "/p", "version": "v", "kind": "nonsense"}}
        )
    with pytest.raises(casetrack.SchemaError):
        casetrack._validate_references(
            {"1bad": {"path": "/p", "version": "v"}}
        )


def test_validate_analyses_uses_must_be_known_refs():
    analyses = {"clair3": {"level": "specimen", "uses": ["genome", "dbsnp"]}}
    refs = {"genome": {"path": "/p", "version": "v"},
            "dbsnp": {"path": "/p", "version": "v"}}
    casetrack._validate_analyses(analyses, references=refs)  # no raise
    with pytest.raises(casetrack.SchemaError):
        casetrack._validate_analyses(
            {"clair3": {"level": "specimen", "uses": ["ghost"]}}, references=refs
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts_schema.py -k validate -v`
Expected: FAIL — `AttributeError: module 'casetrack' has no attribute '_validate_references'` and `_validate_analyses() got an unexpected keyword argument 'references'`

- [ ] **Step 3: Write minimal implementation**

In `casetrack.py`, add after `_validate_analyses` (after line 533):

```python
def _validate_references(references: dict) -> None:
    if not isinstance(references, dict):
        raise SchemaError("[references] must be a table")
    import re as _re
    from casetrack_qc.reference_artifacts import REFERENCE_KINDS
    key_re = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for ref_key, spec in references.items():
        if not key_re.match(ref_key):
            raise SchemaError(
                f"[references.{ref_key}] key must be a valid identifier"
            )
        if not isinstance(spec, dict):
            raise SchemaError(f"[references.{ref_key}] must be an inline table")
        for required in ("path", "version"):
            val = spec.get(required)
            if not isinstance(val, str) or not val:
                raise SchemaError(
                    f"[references.{ref_key}] missing/invalid required key: {required}"
                )
        kind = spec.get("kind")
        if kind is not None and kind not in REFERENCE_KINDS:
            raise SchemaError(
                f"[references.{ref_key}] kind={kind!r} must be one of "
                f"{list(REFERENCE_KINDS)}"
            )
```

Change the `_validate_analyses` signature (line 500) and add the `uses` check inside the loop (after the `summary_tsv` block, line 533):

```python
def _validate_analyses(analyses: dict, references: dict | None = None) -> None:
    ...
    # (inside the for-loop, after summary_tsv validation:)
        uses = spec.get("uses")
        if uses is not None:
            if not isinstance(uses, list) or not all(isinstance(u, str) for u in uses):
                raise SchemaError(
                    f"[analyses.{tool}] uses must be a list of reference keys"
                )
            known = set((references or {}).keys())
            for ref_key in uses:
                if known and ref_key not in known:
                    raise SchemaError(
                        f"[analyses.{tool}] uses references unknown ref_key "
                        f"{ref_key!r}; declare it under [references.{ref_key}]"
                    )
```

Then find the call site that validates the parsed schema (search `_validate_analyses(` in `casetrack.py`) and update it to pass references and call `_validate_references`:

```python
    references = schema.get("references", {})
    if references:
        _validate_references(references)
    if analyses:
        _validate_analyses(analyses, references=references)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts_schema.py -k validate -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the existing schema tests to confirm no regression**

Run: `python3 -m pytest tests/ -q -k "schema or analyses or layout or path_infer"`
Expected: all PASS (the new `references` kwarg defaults to None; existing callers unaffected)

- [ ] **Step 6: Commit**

```bash
git add casetrack.py tests/test_reference_artifacts_schema.py
git commit -m "feat(refs): validate [references] block + [analyses].uses (0010 §6.1)"
```

---

## Task 3: CRUD + `sync_references_from_toml`

**Files:**
- Modify: `casetrack_qc/reference_artifacts.py`
- Test: `tests/test_reference_artifacts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reference_artifacts.py
import sqlite3
import pytest
from casetrack_qc import reference_artifacts as ra


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE assays (assay_id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE cohort_artifacts (artifact_id INTEGER PRIMARY KEY AUTOINCREMENT)"
    )
    ra.ensure_reference_schema(conn)
    return conn


def test_sync_inserts_updates_and_reports_version_changes():
    conn = _conn()
    toml_refs = {
        "genome": {"path": "/db/hg38.fa", "version": "hg38_v0", "kind": "genome"},
        "gtf": {"path": "/db/g.v47.gtf", "version": "v47", "kind": "annotation"},
    }
    changes = ra.sync_references_from_toml(conn, toml_refs)
    assert {c["ref_key"] for c in changes} == {"genome", "gtf"}
    assert all(c["old_version"] is None for c in changes)  # all new

    # bump genome version, leave gtf
    toml_refs["genome"]["version"] = "hg38_v1"
    changes = ra.sync_references_from_toml(conn, toml_refs)
    assert changes == [
        {"ref_key": "genome", "old_version": "hg38_v0", "new_version": "hg38_v1"}
    ]
    assert ra.get_reference(conn, "genome").version == "hg38_v1"


def test_record_usage_is_idempotent_per_output_and_ref():
    conn = _conn()
    ra.sync_references_from_toml(
        conn, {"genome": {"path": "/p", "version": "v1"}}
    )
    ra.record_usage(conn, scope="analysis", entity_level="specimen",
                    entity_id="S1", analysis="clair3", ref_key="genome",
                    version_used="v1", transaction_id="t1")
    # same edge again with a newer version_used overwrites (re-append semantics)
    ra.record_usage(conn, scope="analysis", entity_level="specimen",
                    entity_id="S1", analysis="clair3", ref_key="genome",
                    version_used="v2", transaction_id="t2")
    rows = conn.execute(
        "SELECT version_used FROM reference_usage WHERE entity_id='S1'"
    ).fetchall()
    assert rows == [("v2",)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'sync_references_from_toml'`

- [ ] **Step 3: Write minimal implementation (append to `reference_artifacts.py`)**

```python
class ReferenceError(Exception):
    """Raised when a reference operation violates an enforced invariant."""


@dataclass
class ReferenceArtifact:
    ref_key: str
    path: str
    version: str
    kind: str | None
    checksum: str | None
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def get_reference(conn, ref_key: str) -> "ReferenceArtifact | None":
    row = conn.execute(
        "SELECT ref_key, path, version, kind, checksum, updated_at "
        "FROM reference_artifacts WHERE ref_key = ?", (ref_key,)
    ).fetchone()
    return ReferenceArtifact(*row) if row else None


def list_references(conn) -> list["ReferenceArtifact"]:
    rows = conn.execute(
        "SELECT ref_key, path, version, kind, checksum, updated_at "
        "FROM reference_artifacts ORDER BY ref_key"
    ).fetchall()
    return [ReferenceArtifact(*r) for r in rows]


def sync_references_from_toml(conn, toml_refs: dict) -> list[dict]:
    """Make reference_artifacts match the TOML [references] block.

    Inserts new refs, updates changed path/version/kind/checksum. Returns the
    list of version changes ({ref_key, old_version, new_version}) so the caller
    can write reference_version_change provenance. Removal from TOML does NOT
    delete the row here (a usage row pointing at a removed ref must still read
    STALE — 0010 §6.1); removal handling is left to a future doctor check.
    """
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    changes: list[dict] = []
    for ref_key, spec in toml_refs.items():
        existing = get_reference(conn, ref_key)
        new_version = spec["version"]
        if existing is None:
            conn.execute(
                "INSERT INTO reference_artifacts "
                "(ref_key, path, version, kind, checksum, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ref_key, spec["path"], new_version, spec.get("kind"),
                 spec.get("checksum"), now),
            )
            changes.append({"ref_key": ref_key, "old_version": None,
                            "new_version": new_version})
        elif (existing.version != new_version or existing.path != spec["path"]
              or existing.kind != spec.get("kind")
              or existing.checksum != spec.get("checksum")):
            conn.execute(
                "UPDATE reference_artifacts SET path=?, version=?, kind=?, "
                "checksum=?, updated_at=? WHERE ref_key=?",
                (spec["path"], new_version, spec.get("kind"),
                 spec.get("checksum"), now, ref_key),
            )
            if existing.version != new_version:
                changes.append({"ref_key": ref_key,
                                "old_version": existing.version,
                                "new_version": new_version})
    return changes


def record_usage(conn, *, scope: str, ref_key: str, version_used: str,
                 transaction_id: str, entity_level: str | None = None,
                 entity_id: str | None = None, analysis: str | None = None,
                 artifact_id: int | None = None,
                 recorded_at: str | None = None) -> None:
    """Upsert one (output × ref) usage edge. Re-append overwrites version_used."""
    if recorded_at is None:
        recorded_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    if scope == "analysis":
        conn.execute(
            "DELETE FROM reference_usage WHERE scope='analysis' AND "
            "entity_level=? AND entity_id=? AND analysis=? AND ref_key=?",
            (entity_level, entity_id, analysis, ref_key),
        )
    elif scope == "cohort":
        conn.execute(
            "DELETE FROM reference_usage WHERE scope='cohort' AND "
            "artifact_id=? AND ref_key=?", (artifact_id, ref_key),
        )
    else:
        raise ReferenceError(f"unknown usage scope {scope!r}")
    conn.execute(
        "INSERT INTO reference_usage (scope, entity_level, entity_id, analysis, "
        "artifact_id, ref_key, version_used, recorded_at, transaction_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scope, entity_level, entity_id, analysis, artifact_id, ref_key,
         version_used, recorded_at, transaction_id),
    )
```

Add the new names to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add casetrack_qc/reference_artifacts.py tests/test_reference_artifacts.py
git commit -m "feat(refs): CRUD + sync_references_from_toml + record_usage (0010 §6.1)"
```

---

## Task 4: Read-time staleness (three states + reason)

**Files:**
- Modify: `casetrack_qc/reference_artifacts.py`
- Test: `tests/test_reference_artifacts.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_staleness_three_states_and_reason():
    conn = _conn()
    ra.sync_references_from_toml(conn, {
        "genome": {"path": "/p", "version": "hg38_v1"},
        "gtf": {"path": "/p", "version": "v47"},
    })
    # fresh: used current version
    ra.record_usage(conn, scope="analysis", entity_level="specimen",
                    entity_id="S_fresh", analysis="clair3", ref_key="genome",
                    version_used="hg38_v1", transaction_id="t")
    # stale: used an old version
    ra.record_usage(conn, scope="analysis", entity_level="specimen",
                    entity_id="S_stale", analysis="clair3", ref_key="genome",
                    version_used="hg38_v0", transaction_id="t")

    s_fresh = ra.output_staleness(conn, scope="analysis",
                                  entity_level="specimen", entity_id="S_fresh",
                                  analysis="clair3")
    assert s_fresh["state"] == "fresh" and s_fresh["reasons"] == []

    s_stale = ra.output_staleness(conn, scope="analysis",
                                  entity_level="specimen", entity_id="S_stale",
                                  analysis="clair3")
    assert s_stale["state"] == "STALE"
    assert s_stale["reasons"] == ["genome: hg38_v0 -> hg38_v1"]

    # untracked: an output with no usage rows
    s_unk = ra.output_staleness(conn, scope="analysis", entity_level="specimen",
                                entity_id="S_none", analysis="modkit")
    assert s_unk["state"] == "untracked"


def test_staleness_removed_ref_key_is_stale():
    conn = _conn()
    ra.sync_references_from_toml(conn, {"dbsnp": {"path": "/p", "version": "b156"}})
    ra.record_usage(conn, scope="analysis", entity_level="specimen",
                    entity_id="S1", analysis="clair3", ref_key="dbsnp",
                    version_used="b156", transaction_id="t")
    # remove dbsnp from the canonical set
    conn.execute("DELETE FROM reference_artifacts WHERE ref_key='dbsnp'")
    s = ra.output_staleness(conn, scope="analysis", entity_level="specimen",
                            entity_id="S1", analysis="clair3")
    assert s["state"] == "STALE"
    assert s["reasons"] == ["reference removed: dbsnp"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts.py -k staleness -v`
Expected: FAIL — `AttributeError: ... has no attribute 'output_staleness'`

- [ ] **Step 3: Write minimal implementation (append)**

```python
def _current_versions(conn) -> dict[str, str]:
    return {r.ref_key: r.version for r in list_references(conn)}


def output_staleness(conn, *, scope: str, entity_level: str | None = None,
                     entity_id: str | None = None, analysis: str | None = None,
                     artifact_id: int | None = None) -> dict:
    """Return {'state': fresh|STALE|untracked, 'reasons': [str]} for one output.

    STALE when any usage row's version_used != current canonical version, or the
    ref_key was removed from the canonical set. untracked when no usage rows.
    Derived purely at read time (0010 §6.2).
    """
    if scope == "analysis":
        rows = conn.execute(
            "SELECT ref_key, version_used FROM reference_usage WHERE "
            "scope='analysis' AND entity_level=? AND entity_id=? AND analysis=?",
            (entity_level, entity_id, analysis),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ref_key, version_used FROM reference_usage WHERE "
            "scope='cohort' AND artifact_id=?", (artifact_id,),
        ).fetchall()
    if not rows:
        return {"state": "untracked", "reasons": []}
    current = _current_versions(conn)
    reasons: list[str] = []
    for ref_key, version_used in rows:
        if ref_key not in current:
            reasons.append(f"reference removed: {ref_key}")
        elif current[ref_key] != version_used:
            reasons.append(f"{ref_key}: {version_used} -> {current[ref_key]}")
    return {"state": "STALE" if reasons else "fresh", "reasons": sorted(reasons)}


def all_stale_outputs(conn) -> list[dict]:
    """Every output with >=1 usage row, annotated with its staleness state.

    Used by the `references --stale-only` CLI and read paths.
    """
    out: list[dict] = []
    # analysis-scope outputs (distinct entity+analysis)
    for level, eid, analysis in conn.execute(
        "SELECT DISTINCT entity_level, entity_id, analysis FROM reference_usage "
        "WHERE scope='analysis'"
    ).fetchall():
        s = output_staleness(conn, scope="analysis", entity_level=level,
                             entity_id=eid, analysis=analysis)
        out.append({"scope": "analysis", "entity_level": level, "entity_id": eid,
                    "analysis": analysis, "artifact_id": None, **s})
    for (aid,) in conn.execute(
        "SELECT DISTINCT artifact_id FROM reference_usage WHERE scope='cohort'"
    ).fetchall():
        s = output_staleness(conn, scope="cohort", artifact_id=aid)
        out.append({"scope": "cohort", "entity_level": None, "entity_id": None,
                    "analysis": None, "artifact_id": aid, **s})
    return out
```

Add `output_staleness`, `all_stale_outputs`, `ReferenceArtifact`, `ReferenceError`, `get_reference`, `list_references`, `sync_references_from_toml`, `record_usage` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add casetrack_qc/reference_artifacts.py tests/test_reference_artifacts.py
git commit -m "feat(refs): read-time staleness — fresh/STALE/untracked + reasons (0010 §6.2)"
```

---

## Task 5: Create schema on `init`; `migrate-references` CLI

**Files:**
- Modify: `casetrack.py` (init hook ~line 1517)
- Create: `casetrack_qc/reference_artifacts_cli.py`
- Modify: `casetrack_qc/cli.py` (subparsers were already added in 0010 design — add them now; dispatch map)
- Test: `tests/test_reference_artifacts_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reference_artifacts_cli.py
import subprocess
import sys
import sqlite3
from pathlib import Path
import pytest
import casetrack
from casetrack_qc import reference_artifacts as ra


def _init_project(tmp_path) -> Path:
    pdir = tmp_path / "proj"
    subprocess.run(
        [sys.executable, "-m", "casetrack", "init", "--project-dir", str(pdir),
         "--project-name", "proj"], check=True, capture_output=True, text=True)
    return pdir


def test_init_creates_reference_schema(tmp_path):
    pdir = _init_project(tmp_path)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    assert ra.reference_schema_exists(conn) is True


def test_migrate_references_is_idempotent(tmp_path):
    pdir = _init_project(tmp_path)
    # drop the tables to simulate a pre-0010 project
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    conn.execute("DROP TABLE reference_usage")
    conn.execute("DROP TABLE reference_artifacts")
    conn.commit(); conn.close()

    r = subprocess.run(
        [sys.executable, "-m", "casetrack", "migrate-references",
         "--project-dir", str(pdir)], capture_output=True, text=True)
    assert r.returncode == 0
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    assert ra.reference_schema_exists(conn)
    # second run: no-op
    r2 = subprocess.run(
        [sys.executable, "-m", "casetrack", "migrate-references",
         "--project-dir", str(pdir)], capture_output=True, text=True)
    assert "No migration needed" in r2.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts_cli.py -k "init_creates or migrate" -v`
Expected: FAIL — `reference_schema_exists` is False after init (hook not added); `migrate-references` is an unknown command.

- [ ] **Step 3a: Add the init hook** in `casetrack.py` (after line 1517, alongside the cohort-artifact hook):

```python
            from casetrack_qc.reference_artifacts import (
                ensure_reference_schema as _ensure_reference_schema,
            )
            # Proposal 0010: reference-artifact sibling tables, same init txn.
            _ensure_reference_schema(conn)
```

- [ ] **Step 3b: Create `casetrack_qc/reference_artifacts_cli.py`** with `cmd_migrate_references` (mirror `cmd_migrate_cohort` exactly):

```python
"""CLI commands for reference artifacts (proposal 0010 §6.3)."""
from __future__ import annotations

import json
import sys

import casetrack
from casetrack_qc import reference_artifacts as ra


def cmd_migrate_references(args) -> None:
    project_dir, _ = casetrack._resolve_project(
        args.project_dir, bypass_legacy_gate=True)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        if ra.reference_schema_exists(conn):
            print("No migration needed — reference schema already in place.")
            return
        if getattr(args, "dry_run", False):
            print("[dry-run] Would create reference_artifacts + "
                  "reference_usage tables (+ indexes).")
            return
        txn_id = casetrack._new_transaction_id()
        with casetrack.begin_immediate(conn):
            executed = ra.ensure_reference_schema(conn)
        casetrack.log_project_provenance(
            project_dir,
            {"action": "migrate_references", "executed_sql": executed,
             "transaction_id": txn_id})
        print(f"Created reference schema ({len(executed)} statements).")
    finally:
        conn.close()


__all__ = ["cmd_migrate_references"]
```

- [ ] **Step 3c: Wire into `casetrack_qc/cli.py`** — add the subparser (near the cohort block) and the dispatch entry:

```python
    # ── migrate-references ── (proposal 0010)
    p_migr = subparsers.add_parser(
        "migrate-references",
        help="[v0.8] Additive: create reference-artifact tables on a pre-0010 project")
    p_migr.add_argument("--project-dir", required=True)
    p_migr.add_argument("--dry-run", action="store_true")
```

At the top import: `from casetrack_qc.reference_artifacts_cli import cmd_migrate_references`
In `qc_command_dispatch()`: add `"migrate-references": cmd_migrate_references,`

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts_cli.py -k "init_creates or migrate" -v`
Expected: PASS

- [ ] **Step 5: Run init/migrate regression tests**

Run: `python3 -m pytest tests/ -q -k "init or migrate_cohort or migrate_qc"`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add casetrack.py casetrack_qc/reference_artifacts_cli.py casetrack_qc/cli.py tests/test_reference_artifacts_cli.py
git commit -m "feat(refs): create schema on init + migrate-references command (0010 §6.3)"
```

---

## Task 6: `schema apply` syncs references + writes provenance

**Files:**
- Modify: `casetrack.py` — the `schema apply` command path (search `def cmd_schema` and the `apply` action).
- Test: `tests/test_reference_artifacts_cli.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_schema_apply_syncs_references_and_logs_version_change(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    text = toml.read_text()
    text += (
        '\n[references.genome]\n'
        'path = "/db/hg38.fa"\nversion = "hg38_v0"\nkind = "genome"\n'
    )
    toml.write_text(text)
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True,
                   capture_output=True, text=True)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    assert ra.get_reference(conn, "genome").version == "hg38_v0"
    conn.close()

    # bump the version and re-apply
    toml.write_text(text.replace("hg38_v0", "hg38_v1"))
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True,
                   capture_output=True, text=True)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    assert ra.get_reference(conn, "genome").version == "hg38_v1"
    conn.close()
    prov = (pdir / "provenance.jsonl").read_text()
    assert "reference_version_change" in prov
    assert "hg38_v0" in prov and "hg38_v1" in prov
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts_cli.py -k schema_apply -v`
Expected: FAIL — `get_reference` returns None (sync not wired into schema apply).

- [ ] **Step 3: Write minimal implementation**

In `casetrack.py`, locate the `schema apply` handler (where it calls `apply_schema(conn, schema)` and bumps `schema_v`). After the existing apply + within/after its transaction, add:

```python
    # Proposal 0010: materialize [references] into reference_artifacts and
    # record version moves to provenance (the event that flips outputs stale).
    references = schema.get("references", {})
    if references:
        from casetrack_qc.reference_artifacts import (
            ensure_reference_schema, sync_references_from_toml,
        )
        txn_id = _new_transaction_id()
        with begin_immediate(conn):
            ensure_reference_schema(conn)
            ref_changes = sync_references_from_toml(conn, references)
        for ch in ref_changes:
            if ch["old_version"] is not None:  # version move, not first insert
                log_project_provenance(project_dir, {
                    "action": "reference_version_change",
                    "ref_key": ch["ref_key"],
                    "old_version": ch["old_version"],
                    "new_version": ch["new_version"],
                    "transaction_id": txn_id,
                })
```

(Match the exact variable names available in that function — `conn`, `schema`, `project_dir`. If `project_dir` isn't in scope, derive it from `args.project_dir` via `_resolve_project`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts_cli.py -k schema_apply -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add casetrack.py tests/test_reference_artifacts_cli.py
git commit -m "feat(refs): schema apply syncs references + logs version changes (0010 §6.3)"
```

---

## Task 7: Capture usage on `append` (auto + override + opt-out)

**Files:**
- Modify: `casetrack_qc/reference_artifacts_cli.py` — add `capture_reference_usage` helper.
- Modify: `casetrack.py` — `cmd_append_project` (after columns + `_done` written, before/after provenance) + append argparse flags `--uses-references`, `--no-track-references`.
- Test: `tests/test_reference_artifacts_cli.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def _bootstrap_one_specimen(pdir):
    # patient -> specimen so an append target exists; uses add-metadata
    import csv, io
    for level, hdr, row in [
        ("patient", "patient_id\tcohort", "P1\tc"),
        ("specimen", "specimen_id\tpatient_id\ttissue", "S1\tP1\ttumor"),
    ]:
        f = pdir / f"{level}.tsv"
        f.write_text(hdr + "\n" + row + "\n")
        subprocess.run([sys.executable, "-m", "casetrack", "add-metadata",
                        "--project-dir", str(pdir), "--level", level,
                        "--metadata", str(f), "--allow-new", "--yes"],
                       check=True, capture_output=True, text=True)


def test_append_auto_captures_declared_uses(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n'
        '\n[analyses.clair3]\nlevel="specimen"\ncolumn_prefix="clair3"\nuses=["genome"]\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)

    summary = pdir / "clair3_summary.tsv"
    summary.write_text("specimen_id\tn_snv\nS1\t1000\n")
    subprocess.run([sys.executable, "-m", "casetrack", "append",
                    "--project-dir", str(pdir), "--analysis", "clair3",
                    "--results", str(summary), "--overwrite"],
                   check=True, capture_output=True, text=True)

    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    s = ra.output_staleness(conn, scope="analysis", entity_level="specimen",
                            entity_id="S1", analysis="clair3")
    assert s["state"] == "fresh"  # used hg38_v0, current is hg38_v0
    conn.close()


def test_no_track_references_skips_capture(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n'
        '\n[analyses.clair3]\nlevel="specimen"\ncolumn_prefix="clair3"\nuses=["genome"]\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)
    summary = pdir / "clair3_summary.tsv"
    summary.write_text("specimen_id\tn_snv\nS1\t1000\n")
    subprocess.run([sys.executable, "-m", "casetrack", "append",
                    "--project-dir", str(pdir), "--analysis", "clair3",
                    "--results", str(summary), "--overwrite",
                    "--no-track-references"], check=True, capture_output=True, text=True)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    s = ra.output_staleness(conn, scope="analysis", entity_level="specimen",
                            entity_id="S1", analysis="clair3")
    assert s["state"] == "untracked"
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts_cli.py -k "auto_captures or no_track" -v`
Expected: FAIL — `--no-track-references` unrecognized; staleness `untracked` in the auto test.

- [ ] **Step 3a: Add `capture_reference_usage` to `reference_artifacts_cli.py`:**

```python
def capture_reference_usage(conn, *, schema: dict, analysis: str, level: str,
                            entity_ids: list[str], transaction_id: str,
                            override_refs: list[str] | None = None) -> int:
    """Record reference_usage for every (entity, ref) this analysis consumed.

    ref keys come from override_refs if given, else [analyses.<analysis>].uses.
    The version recorded is the current canonical version from reference_artifacts.
    No-op (returns 0) when there are no ref keys. Caller owns the transaction.
    """
    if override_refs is not None:
        ref_keys = override_refs
    else:
        ref_keys = (schema.get("analyses", {}).get(analysis, {}) or {}).get("uses", [])
    if not ref_keys:
        return 0
    ra.ensure_reference_schema(conn)
    current = {r.ref_key: r.version for r in ra.list_references(conn)}
    n = 0
    for ref_key in ref_keys:
        version = current.get(ref_key)
        if version is None:
            # declared-but-unsynced ref: skip silently; validate/doctor flags it
            continue
        for eid in entity_ids:
            ra.record_usage(conn, scope="analysis", entity_level=level,
                            entity_id=eid, analysis=analysis, ref_key=ref_key,
                            version_used=version, transaction_id=transaction_id)
            n += 1
    return n
```

- [ ] **Step 3b: Hook into `cmd_append_project`** (`casetrack.py` ~line 4288+). After the analysis columns and `_done` are written and the key column values (`entity_ids`) are known, inside the same `begin_immediate` block (or a follow-up one sharing `txn_id`), add:

```python
    if not getattr(args, "no_track_references", False):
        from casetrack_qc.reference_artifacts_cli import capture_reference_usage
        override = None
        if getattr(args, "uses_references", None):
            override = [s.strip() for s in args.uses_references.split(",") if s.strip()]
        capture_reference_usage(
            conn, schema=schema, analysis=args.analysis, level=level,
            entity_ids=list(key_values), transaction_id=txn_id,
            override_refs=override,
        )
```

(Use whatever the function already calls the resolved level and the list of key-column values written — inspect the surrounding code; they exist because the UPDATE/INSERT loop uses them. If `txn_id` isn't already created in this function, reuse the existing transaction id variable or create one with `_new_transaction_id()`.)

- [ ] **Step 3c: Add append argparse flags.** Find the `append` subparser in `casetrack.py` `main()` and add:

```python
    p_append.add_argument("--uses-references", dest="uses_references", default=None,
        help="[v0.8] Comma-separated reference keys this run consumed "
             "(overrides [analyses.<tool>].uses)")
    p_append.add_argument("--no-track-references", dest="no_track_references",
        action="store_true", help="[v0.8] Skip reference-usage capture for this append")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts_cli.py -k "auto_captures or no_track" -v`
Expected: PASS

- [ ] **Step 5: Run the append regression suite**

Run: `python3 -m pytest tests/ -q -k "append"`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add casetrack.py casetrack_qc/reference_artifacts_cli.py tests/test_reference_artifacts_cli.py
git commit -m "feat(refs): capture reference_usage on append (auto/override/opt-out) (0010 §6.3)"
```

---

## Task 8: `references` listing command (+ `--stale-only`, `--fmt`)

**Files:**
- Modify: `casetrack_qc/reference_artifacts_cli.py` — `cmd_references`.
- Modify: `casetrack_qc/cli.py` — subparser + dispatch.
- Test: `tests/test_reference_artifacts_cli.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_references_command_lists_and_filters_stale(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n'
        '\n[analyses.clair3]\nlevel="specimen"\ncolumn_prefix="clair3"\nuses=["genome"]\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)
    summary = pdir / "clair3_summary.tsv"; summary.write_text("specimen_id\tn_snv\nS1\t1\n")
    subprocess.run([sys.executable, "-m", "casetrack", "append", "--project-dir",
                    str(pdir), "--analysis", "clair3", "--results", str(summary),
                    "--overwrite"], check=True, capture_output=True, text=True)

    # list: genome present
    r = subprocess.run([sys.executable, "-m", "casetrack", "references",
                        "--project-dir", str(pdir), "--fmt", "json"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "genome" in r.stdout

    # bump version -> S1 becomes stale
    toml.write_text(toml.read_text().replace("hg38_v0", "hg38_v1"))
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    r2 = subprocess.run([sys.executable, "-m", "casetrack", "references",
                         "--project-dir", str(pdir), "--stale-only"],
                        capture_output=True, text=True)
    assert "S1" in r2.stdout and "STALE" in r2.stdout
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts_cli.py -k references_command -v`
Expected: FAIL — `references` is an unknown command.

- [ ] **Step 3a: Add `cmd_references`** to `reference_artifacts_cli.py` (mirror `cmd_cohort_artifacts`):

```python
def cmd_references(args) -> None:
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        if not ra.reference_schema_exists(conn):
            print(f"Error: project has no reference schema. Run "
                  f"`casetrack migrate-references --project-dir {project_dir}`.",
                  file=sys.stderr)
            sys.exit(1)
        stale_only = getattr(args, "stale_only", False)
        fmt = getattr(args, "fmt", None) or "table"
        if stale_only:
            rows = [r for r in ra.all_stale_outputs(conn) if r["state"] == "STALE"]
            if fmt == "json":
                print(json.dumps(rows, indent=2))
            elif fmt == "tsv":
                cols = ["scope", "entity_level", "entity_id", "analysis",
                        "artifact_id", "state"]
                print("#" + "\t".join(cols))
                for r in rows:
                    print("\t".join(str(r.get(c)) for c in cols))
            else:
                if not rows:
                    print("No stale outputs.")
                    return
                for r in rows:
                    who = (f"{r['entity_level']}:{r['entity_id']}/{r['analysis']}"
                           if r["scope"] == "analysis"
                           else f"cohort_artifact:{r['artifact_id']}")
                    print(f"[STALE] {who}  ({'; '.join(r['reasons'])})")
            return
        # default: the canonical set + per-ref usage tallies
        refs = ra.list_references(conn)
        stale_outputs = ra.all_stale_outputs(conn)
        out = []
        for ref in refs:
            using = [o for o in stale_outputs]  # all tracked outputs
            out.append({"ref_key": ref.ref_key, "version": ref.version,
                        "kind": ref.kind, "path": ref.path})
        if fmt == "json":
            print(json.dumps(out, indent=2))
        elif fmt == "tsv":
            print("#ref_key\tversion\tkind\tpath")
            for r in out:
                print(f"{r['ref_key']}\t{r['version']}\t{r['kind']}\t{r['path']}")
        else:
            if not out:
                print("No references declared.")
                return
            for r in out:
                print(f"{r['ref_key']}  version={r['version']}  kind={r['kind']}")
```

Add `cmd_references` to `__all__`.

- [ ] **Step 3b: Wire `references` subparser in `casetrack_qc/cli.py`:**

```python
    p_refs = subparsers.add_parser(
        "references", help="[v0.8] List reference artifacts + ref-staleness")
    p_refs.add_argument("--project-dir", required=True)
    p_refs.add_argument("--fmt", choices=["table", "tsv", "json"], default="table")
    p_refs.add_argument("--stale-only", dest="stale_only", action="store_true")
```

Import `cmd_references`; add `"references": cmd_references,` to the dispatch map.

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts_cli.py -k references_command -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add casetrack_qc/reference_artifacts_cli.py casetrack_qc/cli.py tests/test_reference_artifacts_cli.py
git commit -m "feat(refs): references listing command + --stale-only (0010 §6.3)"
```

---

## Task 9: `query` views — `_reference_usage` + `_cohort_artifacts.ref_stale`

**Files:**
- Modify: `casetrack.py` — the DuckDB query connection setup (search `_cohort_artifacts` view, ~line 5968).
- Test: `tests/test_reference_artifacts_readpaths.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reference_artifacts_readpaths.py
import subprocess, sys, json, sqlite3
from pathlib import Path
import casetrack
from casetrack_qc import reference_artifacts as ra
# reuse helpers
from tests.test_reference_artifacts_cli import _init_project, _bootstrap_one_specimen


def _stale_setup(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n'
        '\n[analyses.clair3]\nlevel="specimen"\ncolumn_prefix="clair3"\nuses=["genome"]\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)
    summary = pdir / "clair3_summary.tsv"; summary.write_text("specimen_id\tn_snv\nS1\t1\n")
    subprocess.run([sys.executable, "-m", "casetrack", "append", "--project-dir",
                    str(pdir), "--analysis", "clair3", "--results", str(summary),
                    "--overwrite"], check=True, capture_output=True, text=True)
    toml.write_text(toml.read_text().replace("hg38_v0", "hg38_v1"))
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    return pdir


def test_query_reference_usage_view_exposes_is_stale(tmp_path):
    pdir = _stale_setup(tmp_path)
    r = subprocess.run([sys.executable, "-m", "casetrack", "query",
                        "--project-dir", str(pdir), "--sql",
                        "SELECT entity_id, ref_key, version_used, current_version, "
                        "is_stale FROM _reference_usage"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "S1" in r.stdout
    # is_stale true (used hg38_v0, current hg38_v1)
    assert "1" in r.stdout or "true" in r.stdout.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts_readpaths.py -k reference_usage_view -v`
Expected: FAIL — `Catalog Error: Table with name _reference_usage does not exist`.

- [ ] **Step 3: Write minimal implementation.** In `casetrack.py`, where the `_cohort_artifacts` DuckDB view is created (search `_cohort_artifacts` near line 5968), add a sibling `_reference_usage` view. The DuckDB connection ATTACHes the SQLite DB; create the view from the attached tables:

```python
    # Proposal 0010: _reference_usage view with derived current_version/is_stale.
    # No-op when reference_usage is absent (pre-0010 project).
    try:
        con.execute("""
            CREATE OR REPLACE VIEW _reference_usage AS
            SELECT u.scope, u.entity_level, u.entity_id, u.analysis,
                   u.artifact_id, u.ref_key, u.version_used,
                   r.version AS current_version,
                   CASE WHEN r.version IS NULL THEN TRUE
                        WHEN r.version <> u.version_used THEN TRUE
                        ELSE FALSE END AS is_stale
            FROM reference_usage u
            LEFT JOIN reference_artifacts r ON r.ref_key = u.ref_key
        """)
    except Exception:
        pass  # tables absent on pre-0010 projects
```

(Match the existing `_cohort_artifacts` view's guard style — it already swallows the absent-table case. Use the same connection variable name used there.)

For `_cohort_artifacts.ref_stale`: extend the existing `_cohort_artifacts` view definition to add a column:

```sql
    -- add to the existing _cohort_artifacts SELECT:
    , EXISTS (SELECT 1 FROM reference_usage ru
              LEFT JOIN reference_artifacts rr ON rr.ref_key = ru.ref_key
              WHERE ru.scope='cohort' AND ru.artifact_id = ca.artifact_id
                AND (rr.version IS NULL OR rr.version <> ru.version_used)) AS ref_stale
```

(If wiring the subquery into the existing view is awkward, instead expose `ref_stale` in the Python row-builder that backs the view; keep the existing input-`stale` column untouched — they must remain distinct per 0010 §6.2.)

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts_readpaths.py -k reference_usage_view -v`
Expected: PASS

- [ ] **Step 5: Run query regression**

Run: `python3 -m pytest tests/ -q -k "query or cohort_artifacts_readpaths"`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add casetrack.py tests/test_reference_artifacts_readpaths.py
git commit -m "feat(refs): _reference_usage query view + _cohort_artifacts.ref_stale (0010 §6.4)"
```

---

## Task 10: `status` section + `export --include-references` + `validate` invariants

**Files:**
- Modify: `casetrack.py` — status output (search `_emit_cohort_artifacts_section`, line 6881), export (`--include-cohort-artifacts`, line ~6232 / argparse line ~7950), validate.
- Test: `tests/test_reference_artifacts_readpaths.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
def test_status_shows_reference_section(tmp_path):
    pdir = _stale_setup(tmp_path)
    r = subprocess.run([sys.executable, "-m", "casetrack", "status",
                        "--project-dir", str(pdir)], capture_output=True, text=True)
    assert r.returncode == 0
    assert "eference" in r.stdout  # "References" section heading


def test_export_include_references(tmp_path):
    pdir = _stale_setup(tmp_path)
    out = pdir / "export.xlsx"
    r = subprocess.run([sys.executable, "-m", "casetrack", "export",
                        "--project-dir", str(pdir), "--include-references",
                        "--output", str(out)], capture_output=True, text=True)
    assert r.returncode == 0 and out.exists()


def test_validate_flags_orphan_usage(tmp_path):
    pdir = _stale_setup(tmp_path)
    # orphan: a usage row whose ref_key isn't in reference_artifacts
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    conn.execute("INSERT INTO reference_usage (scope, entity_level, entity_id, "
                 "analysis, ref_key, version_used, recorded_at) VALUES "
                 "('analysis','specimen','S1','modkit','ghostref','v',datetime('now'))")
    conn.commit(); conn.close()
    r = subprocess.run([sys.executable, "-m", "casetrack", "validate",
                        "--project-dir", str(pdir)], capture_output=True, text=True)
    assert "ghostref" in (r.stdout + r.stderr) or "orphan" in (r.stdout + r.stderr).lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_reference_artifacts_readpaths.py -k "status_shows or export_include or validate_flags" -v`
Expected: FAIL — no References section; `--include-references` unrecognized; validate silent on orphan.

- [ ] **Step 3a: Status section.** Near `_emit_cohort_artifacts_section` (line 6881), add `_emit_references_section(conn)` and call it from the status command right after the cohort-artifacts section:

```python
def _emit_references_section(conn: sqlite3.Connection) -> None:
    from casetrack_qc.reference_artifacts import (
        reference_schema_exists, list_references, all_stale_outputs)
    if not reference_schema_exists(conn):
        return
    refs = list_references(conn)
    if not refs:
        return
    stale = [o for o in all_stale_outputs(conn) if o["state"] == "STALE"]
    print(f"\nReferences ({len(refs)} declared; {len(stale)} stale output(s)):")
    for r in refs:
        print(f"  {r.ref_key}  version={r.version}  kind={r.kind}")
    for o in stale:
        who = (f"{o['entity_level']}:{o['entity_id']}/{o['analysis']}"
               if o["scope"] == "analysis" else f"cohort_artifact:{o['artifact_id']}")
        print(f"  [STALE] {who}  ({'; '.join(o['reasons'])})")
```

- [ ] **Step 3b: Export.** Mirror the `--include-cohort-artifacts` block (line ~6232) and the argparse flag (line ~7950):

```python
        include_refs = getattr(args, "include_references", False) or is_xlsx
        if include_refs:
            from casetrack_qc.reference_artifacts import reference_schema_exists
            if reference_schema_exists(conn):
                df_ra = pd.read_sql_query("SELECT * FROM reference_artifacts", conn)
                df_ru = pd.read_sql_query("SELECT * FROM reference_usage", conn)
                extra_tables += [("reference_artifacts", df_ra),
                                 ("reference_usage", df_ru)]
```

argparse (next to `--include-cohort-artifacts`):

```python
    p_export.add_argument("--include-references", action="store_true",
        help="[v0.8] Also export reference_artifacts + reference_usage")
```

- [ ] **Step 3c: Validate.** In the validate command, after the existing QC/cohort invariant checks, add:

```python
    from casetrack_qc.reference_artifacts import reference_schema_exists
    if reference_schema_exists(conn):
        orphans = conn.execute(
            "SELECT DISTINCT u.ref_key FROM reference_usage u "
            "LEFT JOIN reference_artifacts r ON r.ref_key = u.ref_key "
            "WHERE r.ref_key IS NULL").fetchall()
        for (ref_key,) in orphans:
            print(f"WARN: reference_usage references unknown ref_key {ref_key!r} "
                  f"(removed from [references]?)", file=sys.stderr)
            # increment the validator's warning/issue counter as the surrounding code does
```

(Match how the surrounding validate code accumulates and reports issues — append to its issues list rather than a bare print if that's the pattern.)

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_reference_artifacts_readpaths.py -k "status_shows or export_include or validate_flags" -v`
Expected: PASS

- [ ] **Step 5: Run status/export/validate regression**

Run: `python3 -m pytest tests/ -q -k "status or export or validate"`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add casetrack.py tests/test_reference_artifacts_readpaths.py
git commit -m "feat(refs): status section + export --include-references + validate orphans (0010 §6.4)"
```

---

## Task 11: Dashboard section

**Files:**
- Modify: `casetrack.py` — dashboard (`_cohort_artifacts_html`, line 5846; qc_info assembly, line 5564–5571; template, line 5818).
- Test: `tests/test_reference_artifacts_readpaths.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_dashboard_renders_references_section(tmp_path):
    pdir = _stale_setup(tmp_path)
    out = pdir / "dash.html"
    r = subprocess.run([sys.executable, "-m", "casetrack", "dashboard",
                        "--project-dir", str(pdir), "--output", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and out.exists()
    html = out.read_text()
    assert "References" in html and "genome" in html
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts_readpaths.py -k dashboard_renders -v`
Expected: FAIL — no "References" in the HTML.

- [ ] **Step 3: Write minimal implementation.** In the dashboard's `qc_info` assembly (line ~5564), add references data:

```python
        from casetrack_qc.reference_artifacts import (
            reference_schema_exists as _ra_exists, list_references as _ra_list,
            all_stale_outputs as _ra_stale)
        if _ra_exists(conn):
            qc_info["references"] = [r.to_dict() for r in _ra_list(conn)]
            qc_info["reference_stale"] = [
                o for o in _ra_stale(conn) if o["state"] == "STALE"]
```

Add a `_references_html(qc_info)` builder next to `_cohort_artifacts_html` (line 5846) and insert `{_references_html(qc_info)}` into the template near line 5818:

```python
def _references_html(qc_info: dict | None) -> str:
    if not qc_info or not qc_info.get("references"):
        return ""
    rows = "".join(
        f"<tr><td>{r['ref_key']}</td><td>{r['version']}</td>"
        f"<td>{r.get('kind') or ''}</td></tr>" for r in qc_info["references"])
    stale = qc_info.get("reference_stale") or []
    badge = (f'<p><span class="badge stale">{len(stale)} STALE output(s)</span></p>'
             if stale else '<p><span class="badge fresh">all fresh</span></p>')
    return (f'<section><h2>References</h2>{badge}'
            f'<table><tr><th>ref_key</th><th>version</th><th>kind</th></tr>'
            f'{rows}</table></section>')
```

(Reuse existing dashboard CSS classes; check `_cohort_artifacts_html` for the actual badge class names and table styling, and match them.)

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts_readpaths.py -k dashboard_renders -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add casetrack.py tests/test_reference_artifacts_readpaths.py
git commit -m "feat(refs): dashboard References section (0010 §6.4)"
```

---

## Task 12: MCP `casetrack_references` tool

**Files:**
- Modify: `casetrack_mcp/tools.py` (add `references_tool`), `casetrack_mcp/server.py` (register Tool + dispatch + input schema).
- Test: `tests/test_reference_artifacts_readpaths.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_mcp_references_tool(tmp_path, monkeypatch):
    pdir = _stale_setup(tmp_path)
    # register the project so the MCP slug resolver finds it
    subprocess.run([sys.executable, "-m", "casetrack", "projects", "register",
                    "--project-dir", str(pdir)], capture_output=True, text=True)
    from casetrack_mcp import tools
    # find the slug
    projs = tools.list_projects_tool()["projects"]
    slug = [p["project_id"] for p in projs if str(pdir) in p["path"]][0]
    payload = tools.references_tool(slug, stale_only=True)
    assert any(o["state"] == "STALE" for o in payload["stale_outputs"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts_readpaths.py -k mcp_references -v`
Expected: FAIL — `AttributeError: module 'casetrack_mcp.tools' has no attribute 'references_tool'`

- [ ] **Step 3a: Add `references_tool` to `casetrack_mcp/tools.py`** (mirror `cohort_artifacts_tool`, line 260):

```python
def references_tool(project_id: str, *, stale_only: bool = False) -> dict:
    """List reference artifacts + ref-staleness for a project (proposal 0010)."""
    db_path = _resolve_project_db(project_id)  # same resolver cohort_artifacts_tool uses
    import sqlite3
    from casetrack_qc.reference_artifacts import (
        reference_schema_exists, list_references, all_stale_outputs)
    conn = sqlite3.connect(db_path)
    try:
        if not reference_schema_exists(conn):
            return {"references": [], "stale_outputs": []}
        refs = [r.to_dict() for r in list_references(conn)]
        stale = [o for o in all_stale_outputs(conn) if o["state"] == "STALE"]
        if not stale_only:
            tracked = all_stale_outputs(conn)
        else:
            tracked = stale
        return {"references": refs, "stale_outputs": stale, "outputs": tracked}
    finally:
        conn.close()
```

(Use the exact project-db resolver `cohort_artifacts_tool` uses — open `tools.py` and copy that line, don't invent `_resolve_project_db`.)

- [ ] **Step 3b: Register in `casetrack_mcp/server.py`** — add an input schema constant, a `Tool(...)` entry in `_list_tools` (after `casetrack_cohort_artifacts`), and a dispatch branch in `_call_tool`:

```python
# schema constant near _COHORT_ARTIFACTS_SCHEMA:
_REFERENCES_SCHEMA = {
    "type": "object",
    "properties": {
        "project_id": {"type": "string"},
        "stale_only": {"type": "boolean", "default": False},
    },
    "required": ["project_id"],
}

# in _list_tools, a new Tool:
Tool(
    name="casetrack_references",
    description=(
        "List reference artifacts (genome, annotation, known-variant sets) for "
        "a project, each with read-time ref-staleness: an output is STALE when a "
        "reference version it used no longer matches the current declared version. "
        "Pass stale_only=true to see only stale outputs. project_id must be a slug "
        "from casetrack_list_projects."),
    inputSchema=_REFERENCES_SCHEMA,
),

# in _call_tool:
            elif name == "casetrack_references":
                payload = references_tool(
                    arguments.get("project_id"),
                    stale_only=bool(arguments.get("stale_only", False)))
```

Import `references_tool` at the top of `server.py` alongside `cohort_artifacts_tool`.

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts_readpaths.py -k mcp_references -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add casetrack_mcp/tools.py casetrack_mcp/server.py tests/test_reference_artifacts_readpaths.py
git commit -m "feat(refs): casetrack_references MCP tool (0010 §6.4)"
```

---

## Task 13: Nextflow — `append-cohort` `uses_references` passthrough

**Files:**
- Modify: `casetrack_qc/cli.py` — add `--uses-references` to the `append-cohort` parser; `casetrack_qc/cohort_artifacts_cli.py` — capture cohort-scope usage.
- Modify: `examples/nextflow/casetrack.nf` (`casetrack_append_cohort` process) + `examples/nextflow/subworkflows/local/cohort_artifact_tracked.nf`.
- Test: `tests/test_reference_artifacts_cli.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_append_cohort_uses_references(tmp_path):
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)
    # need an assay for cohort inputs
    a = pdir / "assay.tsv"; a.write_text("assay_id\tspecimen_id\nA1\tS1\n")
    subprocess.run([sys.executable, "-m", "casetrack", "add-metadata",
                    "--project-dir", str(pdir), "--level", "assay",
                    "--metadata", str(a), "--allow-new", "--yes"],
                   check=True, capture_output=True, text=True)
    vcf = pdir / "joint.vcf.gz"; vcf.write_text("x")
    subprocess.run([sys.executable, "-m", "casetrack", "append-cohort",
                    "--project-dir", str(pdir), "--analysis", "joint_genotype",
                    "--run-tag", "rt1", "--path", str(vcf), "--inputs", "A1",
                    "--uses-references", "genome"], check=True,
                   capture_output=True, text=True)
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    aid = conn.execute("SELECT artifact_id FROM cohort_artifacts").fetchone()[0]
    s = ra.output_staleness(conn, scope="cohort", artifact_id=aid)
    assert s["state"] == "fresh"
    conn.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_reference_artifacts_cli.py -k append_cohort_uses -v`
Expected: FAIL — `--uses-references` unrecognized on `append-cohort`.

- [ ] **Step 3a: Add the flag** in `casetrack_qc/cli.py` `append-cohort` parser (`p_appc`):

```python
    p_appc.add_argument("--uses-references", dest="uses_references", default=None,
        help="[v0.8] Comma-separated reference keys this cohort output consumed")
```

- [ ] **Step 3b: Capture in `cmd_append_cohort`** (`cohort_artifacts_cli.py`) — inside the `begin_immediate` block, after `add_artifact_inputs`:

```python
                refs = getattr(args, "uses_references", None)
                if refs:
                    from casetrack_qc import reference_artifacts as _ra
                    _ra.ensure_reference_schema(conn)
                    current = {r.ref_key: r.version for r in _ra.list_references(conn)}
                    for ref_key in [s.strip() for s in refs.split(",") if s.strip()]:
                        if ref_key in current:
                            _ra.record_usage(
                                conn, scope="cohort", artifact_id=art_id,
                                ref_key=ref_key, version_used=current[ref_key],
                                transaction_id=txn_id)
```

- [ ] **Step 3c: Nextflow.** In `examples/nextflow/casetrack.nf` `casetrack_append_cohort`, add an optional `uses_references` element to the input tuple following the **same `[]`-means-none pattern as the stats slot**: when the value is non-empty, append `--uses-references <comma-joined>` to the command; when `[]`, omit it. In `subworkflows/local/cohort_artifact_tracked.nf`, thread an optional `ch_uses_references` (default `[]`) through the assembled tuple. Mirror exactly how `stats` is currently handled in those two files (read them first).

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_reference_artifacts_cli.py -k append_cohort_uses -v`
Expected: PASS

- [ ] **Step 5: Run the Nextflow contract tests if present**

Run: `python3 -m pytest tests/ -q -k "nextflow or nf_ or cohort_artifact_tracked"`
Expected: all PASS (or unchanged skips)

- [ ] **Step 6: Commit**

```bash
git add casetrack_qc/cli.py casetrack_qc/cohort_artifacts_cli.py examples/nextflow/ tests/test_reference_artifacts_cli.py
git commit -m "feat(refs): append-cohort uses_references + NF passthrough (0010 §6.5)"
```

---

## Task 14: Docs + full-suite green + version/CHANGELOG

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `.claude/skills/casetrack/SKILL.md`, `CHANGELOG.md`, `setup.py`, `casetrack.py` (`_CASETRACK_VERSION`).

- [ ] **Step 1: Run the FULL suite — must be green before docs**

Run: `python3 -m pytest tests/ -q`
Expected: all PASS (previous count + the new reference tests). If anything fails, fix it before continuing — do not paper over with skips.

- [ ] **Step 2: Update the command tables**

- `README.md`: add `migrate-references`, `references` to the command table; add a short "Reference artifacts" subsection with the `[references]` + `uses` example and the bump→stale flow.
- `CLAUDE.md`: add a "Reference artifacts (proposal 0010)" bullet to the architecture section and the command-group table; link `docs/proposals/0010-reference-artifacts.md`.
- `.claude/skills/casetrack/SKILL.md`: add a §16 "Reference artifacts" (mirror the §15 cohort-artifacts section: tables, commands, three-state staleness, read paths) and a `references/reference-artifacts.md` deep-dive; add command-table rows + the frontmatter triggers ("reference version", "dbSNP bumped", "is my VCF stale"); renumber the "When to read the references" section.

- [ ] **Step 3: Bump version + CHANGELOG**

- `setup.py`: `0.7.0` → `0.8.0`.
- `casetrack.py`: `_CASETRACK_VERSION = "0.8.0"`.
- `CHANGELOG.md`: add a `## [0.8.0] — <date>` section summarizing reference artifacts (tables, `[references]`, `migrate-references`/`references` commands, `append`/`append-cohort` capture, read-path surfacing, MCP tool, NF passthrough).

(Note: the `[v0.8]` help strings written throughout match this bump.)

- [ ] **Step 4: Final full-suite run**

Run: `python3 -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md .claude/skills/casetrack/ CHANGELOG.md setup.py casetrack.py
git commit -m "docs(refs): document reference artifacts; bump v0.8.0 (0010)"
```

---

## Self-Review (completed during planning)

**Spec coverage:** every §6 subsection maps to a task — §6.1 schema → Tasks 1–3; §6.2 staleness → Task 4; §6.3 CLI (`schema apply`, `migrate-references`, `append`, `append-cohort`, `references`) → Tasks 5–8, 13; §6.4 read paths (status/query/export/dashboard/MCP/validate) → Tasks 9–12; §6.5 Nextflow → Task 13. Migration (init + `migrate-references`) → Task 5. Testing (§10) is interleaved per task. Docs/version → Task 14.

**Type consistency:** `ensure_reference_schema`, `reference_schema_exists`, `sync_references_from_toml`, `record_usage`, `output_staleness`, `all_stale_outputs`, `list_references`, `get_reference`, `ReferenceArtifact`, `references_tool`, `cmd_references`, `cmd_migrate_references`, `capture_reference_usage` are used with consistent signatures across tasks. The three-state contract `{"state": ..., "reasons": [...]}` is uniform.

**Open items the implementer must resolve against live code (flagged inline, not placeholders):** the exact local variable names inside `cmd_append_project` (resolved level + list of key values + transaction id) and the precise insertion point of the `_cohort_artifacts` view extension — both require reading the surrounding code, which the steps instruct.
