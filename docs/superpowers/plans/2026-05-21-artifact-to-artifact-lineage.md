# Artifact-to-artifact lineage (proposal 0011) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic `derived-from` edge between any two lineage nodes (cohort artifact / reference / sample-level output) so staleness propagates transitively across a multi-hop DAG, surfaced as a third `derived_stale` flag orthogonal to 0009 `stale` and 0010 `ref_stale`.

**Architecture:** One additive sibling table `artifact_derivation(down_node, up_node, …)` with canonical node-ref string endpoints, in `casetrack_qc/artifact_derivation.py` (mirrors `reference_artifacts.py`). A memoized, cycle-guarded read-time walk computes `derived_stale` by unioning three edge tables (`artifact_derivation` + the 0010 `reference_usage` edge + the 0009 `cohort_artifact_inputs` direct seed). The three-level core, 0009, and 0010 tables are untouched. The inspect command is `derivation` and the MCP tool `casetrack_derivation` — **not** `lineage`, which is taken by proposal 0006's assay-merge subsystem (`casetrack_lineage`).

**Tech Stack:** Python 3.10–3.13, stdlib `sqlite3`, DuckDB (query views, incl. `WITH RECURSIVE`), pytest, argparse, TOML (`tomllib`/`tomli`), MCP (`mcp` SDK), Nextflow (DSL2).

**Reference implementations to mirror (read these first):**
- `casetrack_qc/reference_artifacts.py` — module shape (DDL, `ensure_*_schema`, dataclass, read-time staleness).
- `casetrack_qc/reference_artifacts_cli.py` — `cmd_migrate_references`, `cmd_references`, `capture_reference_usage`.
- `casetrack_qc/cohort_artifacts_cli.py` — `cmd_append_cohort` (transaction + provenance pattern).
- `casetrack_qc/reader.py:192-298` — `install_cohort_artifact_view`, `install_reference_usage_view`.
- `casetrack_qc/cli.py:179-211` — subparser registration + `qc_command_dispatch()`.
- `casetrack_mcp/tools.py:260-298` — `references_tool`; `casetrack_mcp/server.py:185+` — tool registration.

**Conventions (match exactly):**
- Module functions take an open `conn`; the CLI layer owns the transaction (`with casetrack.begin_immediate(conn):`) + `casetrack.log_project_provenance(...)`.
- All DDL idempotent; `ensure_*` returns executed SQL list.
- Every new file header: `Author: Samuel Ahuno (ekwame001@gmail.com)` + a one-line purpose + the proposal reference.
- Run tests: `cd /data1/greenbab/users/ahunos/apps/casetrack && python3 -m pytest tests/ -q`.
- Commits: conventional-commit style, ending with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`. Work on branch `feature/artifact-lineage-0011` (already created).

---

## Canonical node-ref grammar (used throughout)

| Node type | Canonical string | Backing identity |
|---|---|---|
| cohort artifact | `cohort:<analysis>@<run_tag>` | `cohort_artifacts(analysis, run_tag)` |
| reference artifact | `reference:<ref_key>` | `reference_artifacts.ref_key` |
| sample-level output | `analysis:<entity_level>/<entity_id>/<analysis>` | `(entity_level, entity_id, analysis)` |

Separators are fixed: `cohort:` uses `@` between analysis and run_tag; `analysis:` uses `/` between its three fields; `reference:` has a single payload. Validation rejects a node-ref whose payload count is wrong for its scope.

---

## Task 1: Schema module + `LineageNode` helper

**Files:**
- Create: `casetrack_qc/artifact_derivation.py`
- Test: `tests/test_artifact_derivation_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_artifact_derivation_schema.py
"""Schema + LineageNode tests for proposal 0011 (artifact-to-artifact lineage)."""
import sqlite3
import pytest
from casetrack_qc import artifact_derivation as ad


def _conn():
    return sqlite3.connect(":memory:")


def test_ensure_schema_idempotent():
    conn = _conn()
    assert not ad.derivation_schema_exists(conn)
    first = ad.ensure_derivation_schema(conn)
    assert any("CREATE TABLE artifact_derivation" in s for s in first)
    assert ad.derivation_schema_exists(conn)
    # second call is a no-op
    assert ad.ensure_derivation_schema(conn) == []


def test_lineage_node_roundtrip():
    for s in (
        "cohort:joint_genotype@cohort147_v1",
        "reference:pon",
        "analysis:specimen/SPEC1/clair3",
    ):
        node = ad.LineageNode.parse(s)
        assert node.canonical() == s


def test_lineage_node_fields():
    c = ad.LineageNode.parse("cohort:joint_genotype@cohort147_v1")
    assert c.scope == "cohort" and c.analysis == "joint_genotype" and c.run_tag == "cohort147_v1"
    r = ad.LineageNode.parse("reference:pon")
    assert r.scope == "reference" and r.ref_key == "pon"
    a = ad.LineageNode.parse("analysis:specimen/SPEC1/clair3")
    assert a.scope == "analysis" and a.entity_level == "specimen" and a.entity_id == "SPEC1" and a.analysis == "clair3"


def test_lineage_node_rejects_malformed():
    for bad in ("", "bogus:x", "cohort:noatsign", "reference:", "analysis:only/two"):
        with pytest.raises(ad.DerivationError):
            ad.LineageNode.parse(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'casetrack_qc.artifact_derivation'`.

- [ ] **Step 3: Write minimal implementation**

```python
# casetrack_qc/artifact_derivation.py
"""Artifact-to-artifact lineage: schema, node addressing, edges, and the
read-time transitive staleness walk.

Proposal 0011. A `derived-from` edge links any two lineage nodes (cohort
artifact / reference / sample-level output). Staleness is derived live: a node
is ``derived_stale`` when any node it derives from is itself stale by any cause
(0009 input-stale, 0010 ref-stale, or 0011 derived-stale — recursively).

Mirrors casetrack_qc/reference_artifacts.py: module functions take an open
conn, the command layer owns the transaction + provenance, all DDL idempotent.
Lives in casetrack_qc/ (NOT a new top-level package) because the name
`casetrack_lineage` is already proposal 0006's assay-merge subsystem.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass

TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S"
NODE_SCOPES = ("cohort", "reference", "analysis")


class DerivationError(Exception):
    """Raised when a derivation operation violates an enforced invariant."""


# ── schema ──────────────────────────────────────────────────────────────────

def artifact_derivation_ddl() -> str:
    return (
        "CREATE TABLE artifact_derivation (\n"
        "    derivation_id  INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    down_node      TEXT NOT NULL,\n"
        "    up_node        TEXT NOT NULL,\n"
        "    recorded_at    TEXT NOT NULL,\n"
        "    transaction_id TEXT\n"
        ")"
    )


def artifact_derivation_indexes() -> list[str]:
    return [
        "CREATE UNIQUE INDEX idx_deriv_edge ON artifact_derivation(down_node, up_node)",
        "CREATE INDEX idx_deriv_up ON artifact_derivation(up_node)",
        "CREATE INDEX idx_deriv_down ON artifact_derivation(down_node)",
    ]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def derivation_schema_exists(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "artifact_derivation")


def ensure_derivation_schema(conn: sqlite3.Connection) -> list[str]:
    """Create the artifact_derivation table + indexes. Idempotent."""
    if _table_exists(conn, "artifact_derivation"):
        return []
    executed = [artifact_derivation_ddl()]
    conn.execute(executed[0])
    for idx in artifact_derivation_indexes():
        conn.execute(idx)
        executed.append(idx)
    return executed


# ── node addressing ───────────────────────────────────────────────────────────

@dataclass
class LineageNode:
    scope: str
    analysis: str | None = None      # cohort + analysis scopes
    run_tag: str | None = None       # cohort
    ref_key: str | None = None       # reference
    entity_level: str | None = None  # analysis
    entity_id: str | None = None     # analysis

    @classmethod
    def parse(cls, s: str) -> "LineageNode":
        if not s or ":" not in s:
            raise DerivationError(f"malformed node-ref {s!r}")
        scope, _, payload = s.partition(":")
        if scope == "cohort":
            if "@" not in payload:
                raise DerivationError(f"cohort node-ref needs <analysis>@<run_tag>: {s!r}")
            analysis, _, run_tag = payload.partition("@")
            if not analysis or not run_tag:
                raise DerivationError(f"cohort node-ref needs <analysis>@<run_tag>: {s!r}")
            return cls(scope="cohort", analysis=analysis, run_tag=run_tag)
        if scope == "reference":
            if not payload:
                raise DerivationError(f"reference node-ref needs a ref_key: {s!r}")
            return cls(scope="reference", ref_key=payload)
        if scope == "analysis":
            parts = payload.split("/")
            if len(parts) != 3 or not all(parts):
                raise DerivationError(
                    f"analysis node-ref needs <level>/<entity_id>/<analysis>: {s!r}")
            return cls(scope="analysis", entity_level=parts[0],
                       entity_id=parts[1], analysis=parts[2])
        raise DerivationError(f"unknown node scope {scope!r} in {s!r}")

    def canonical(self) -> str:
        if self.scope == "cohort":
            return f"cohort:{self.analysis}@{self.run_tag}"
        if self.scope == "reference":
            return f"reference:{self.ref_key}"
        if self.scope == "analysis":
            return f"analysis:{self.entity_level}/{self.entity_id}/{self.analysis}"
        raise DerivationError(f"unknown node scope {self.scope!r}")


__all__ = [
    "TIMESTAMP_FMT", "NODE_SCOPES", "DerivationError",
    "artifact_derivation_ddl", "artifact_derivation_indexes",
    "derivation_schema_exists", "ensure_derivation_schema",
    "LineageNode",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_artifact_derivation_schema.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add casetrack_qc/artifact_derivation.py tests/test_artifact_derivation_schema.py
git commit -m "feat(lineage): artifact_derivation schema + LineageNode (0011 §6.1-6.2)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Edge resolution, recording, and cycle prevention

**Files:**
- Modify: `casetrack_qc/artifact_derivation.py`
- Test: `tests/test_artifact_derivation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_artifact_derivation.py
"""Edge recording, resolution, cycle prevention, and the staleness walk (0011)."""
import sqlite3
import pytest

import casetrack
from casetrack_qc import artifact_derivation as ad
from casetrack_qc import cohort_artifacts as ca
from casetrack_qc import reference_artifacts as ra


def _project(tmp_path):
    """A real on-disk project DB with 0009 + 0010 + 0011 schemas + a few rows."""
    db = tmp_path / "casetrack.db"
    conn = casetrack.open_project_db(db)
    # minimal three-level rows so cohort_artifact_inputs FK + active cascade work
    conn.executescript(
        """
        CREATE TABLE patients(patient_id TEXT PRIMARY KEY, qc_status TEXT DEFAULT 'pass',
                              consent_status TEXT DEFAULT 'consented');
        CREATE TABLE specimens(specimen_id TEXT PRIMARY KEY, patient_id TEXT,
                               qc_status TEXT DEFAULT 'pass');
        CREATE TABLE assays(assay_id TEXT PRIMARY KEY, specimen_id TEXT,
                            qc_status TEXT DEFAULT 'pass');
        INSERT INTO patients(patient_id) VALUES ('P1');
        INSERT INTO specimens(specimen_id, patient_id) VALUES ('S1','P1');
        INSERT INTO assays(assay_id, specimen_id) VALUES ('A1','S1'),('A2','S1');
        """
    )
    ca.ensure_cohort_artifacts_schema(conn)
    ra.ensure_reference_schema(conn)
    ad.ensure_derivation_schema(conn)
    conn.commit()
    return conn


def test_record_edge_idempotent(tmp_path):
    conn = _project(tmp_path)
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t1")
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t2")
    rows = ad.list_edges(conn)
    assert len(rows) == 1
    assert rows[0]["down_node"] == "cohort:annot@v1"
    assert rows[0]["up_node"] == "cohort:joint@v1"


def test_record_edge_validates_node_refs(tmp_path):
    conn = _project(tmp_path)
    with pytest.raises(ad.DerivationError):
        ad.record_edge(conn, down="bogus:x", up="cohort:j@v1", transaction_id="t")


def test_cycle_refused_direct(tmp_path):
    conn = _project(tmp_path)
    with pytest.raises(ad.DerivationError):
        ad.record_edge(conn, down="cohort:a@v1", up="cohort:a@v1", transaction_id="t")


def test_cycle_refused_indirect(tmp_path):
    conn = _project(tmp_path)
    ad.record_edge(conn, down="cohort:b@v1", up="cohort:a@v1", transaction_id="t")
    ad.record_edge(conn, down="cohort:c@v1", up="cohort:b@v1", transaction_id="t")
    # c->b->a ; adding a->c would close the loop a->c->b->a
    with pytest.raises(ad.DerivationError):
        ad.record_edge(conn, down="cohort:a@v1", up="cohort:c@v1", transaction_id="t")


def test_upstream_of_node(tmp_path):
    conn = _project(tmp_path)
    ad.record_edge(conn, down="cohort:b@v1", up="cohort:a@v1", transaction_id="t")
    ad.record_edge(conn, down="cohort:b@v1", up="reference:pon", transaction_id="t")
    ups = sorted(ad.upstream_nodes(conn, "cohort:b@v1"))
    assert ups == ["cohort:a@v1", "reference:pon"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation.py -q`
Expected: FAIL — `AttributeError: module 'casetrack_qc.artifact_derivation' has no attribute 'record_edge'`.

- [ ] **Step 3: Write minimal implementation**

Append to `casetrack_qc/artifact_derivation.py` (before `__all__`), and extend `__all__`:

```python
# ── edges + cycle prevention ──────────────────────────────────────────────────

def list_edges(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT down_node, up_node, recorded_at, transaction_id "
        "FROM artifact_derivation ORDER BY down_node, up_node"
    ).fetchall()
    return [{"down_node": d, "up_node": u, "recorded_at": r, "transaction_id": t}
            for (d, u, r, t) in rows]


def upstream_nodes(conn: sqlite3.Connection, node: str) -> list[str]:
    """Direct artifact_derivation upstreams of *node* (one hop, 0011 edges only)."""
    return [u for (u,) in conn.execute(
        "SELECT up_node FROM artifact_derivation WHERE down_node = ?", (node,)
    ).fetchall()]


def _reaches(conn: sqlite3.Connection, start: str, target: str) -> bool:
    """True if *target* is reachable walking up_node edges from *start* (0011 only)."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(upstream_nodes(conn, cur))
    return False


def record_edge(conn: sqlite3.Connection, *, down: str, up: str,
                transaction_id: str, recorded_at: str | None = None) -> None:
    """Record one derived-from edge (down derives from up). Idempotent.

    Validates both node-refs and refuses an edge that would create a cycle in
    the artifact_derivation graph (0011 §6.4).
    """
    LineageNode.parse(down)  # validate
    LineageNode.parse(up)
    if down == up or _reaches(conn, up, down):
        raise DerivationError(
            f"refusing edge {down} <- {up}: would create a derivation cycle")
    if recorded_at is None:
        recorded_at = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    conn.execute(
        "INSERT OR IGNORE INTO artifact_derivation "
        "(down_node, up_node, recorded_at, transaction_id) VALUES (?, ?, ?, ?)",
        (down, up, recorded_at, transaction_id),
    )
```

Add to `__all__`: `"list_edges", "upstream_nodes", "record_edge"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_artifact_derivation.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add casetrack_qc/artifact_derivation.py tests/test_artifact_derivation.py
git commit -m "feat(lineage): record_edge + cycle prevention (0011 §6.4)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Read-time transitive staleness walk

**Files:**
- Modify: `casetrack_qc/artifact_derivation.py`
- Test: `tests/test_artifact_derivation.py` (extend)

The walk reuses existing single-hop staleness:
- 0009 direct input-stale of a **cohort** node: `cohort_artifacts.artifact_staleness(conn)` returns `{artifact_id: [censored_assay_ids]}`.
- 0010 direct ref-stale of **cohort** and **analysis** nodes: `reference_artifacts.output_staleness(conn, scope=..., ...)` returns `{"state": "fresh|STALE|untracked", "reasons": [...]}`.
- `upstream(node)` = `artifact_derivation` up edges **∪** `reference_usage` rows (consumer→`reference:<ref_key>`).

- [ ] **Step 1: Write the failing test (the core staleness matrix)**

```python
# append to tests/test_artifact_derivation.py
from casetrack_qc import events as qc_events  # noqa: E402


def _add_cohort(conn, analysis, run_tag, inputs):
    aid = ca.insert_artifact(conn, analysis=analysis, run_tag=run_tag,
                             path=f"/x/{run_tag}.vcf", n_inputs=len(inputs),
                             transaction_id="t", checksum=None, stats_json=None,
                             created_by="test")
    ca.add_artifact_inputs(conn, aid, inputs)
    conn.commit()
    return aid


def _censor_assay(conn, assay_id):
    conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id=?", (assay_id,))
    conn.commit()


def test_cohort_to_cohort_chain(tmp_path):
    conn = _project(tmp_path)
    _add_cohort(conn, "joint", "v1", ["A1", "A2"])
    _add_cohort(conn, "annot", "v1", ["A1", "A2"])
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t")
    conn.commit()
    # fresh before any censor
    assert ad.derived_staleness(conn, "cohort:annot@v1")["state"] == "fresh"
    # censor an input to the ROOT (joint); annot must read derived_stale via the chain
    _censor_assay(conn, "A2")
    s = ad.derived_staleness(conn, "cohort:annot@v1")
    assert s["state"] == "STALE"
    assert any("joint@v1" in r for r in s["reasons"])


def test_pon_as_reference_cascade(tmp_path):
    """The load-bearing case: censoring a PoN input cascades to a VCF that
    `uses` the pon reference, with NO TOML version bump (0011 §6.3)."""
    conn = _project(tmp_path)
    # PoN built from A1,A2 as a cohort artifact
    _add_cohort(conn, "make_pon", "cohort147_v1", ["A1", "A2"])
    # declare the pon reference (current version) and the derived-from edge
    ra.sync_references_from_toml(conn, {"pon": {"path": "/x/pon.vcf", "version": "pon_v1", "kind": "known_variants"}})
    ad.record_edge(conn, down="reference:pon", up="cohort:make_pon@cohort147_v1", transaction_id="t")
    # a downstream cohort VCF that USES the pon reference (0010 reference_usage, cohort scope)
    vcf_id = _add_cohort(conn, "call", "v1", ["A1"])
    ra.record_usage(conn, scope="cohort", artifact_id=vcf_id, ref_key="pon",
                    version_used="pon_v1", transaction_id="t")
    conn.commit()
    # nothing censored yet
    assert ad.derived_staleness(conn, "reference:pon")["state"] == "fresh"
    assert ad.derived_staleness(conn, "cohort:call@v1")["state"] == "fresh"
    # censor a PoN input — NO version bump
    _censor_assay(conn, "A2")
    assert ad.derived_staleness(conn, "reference:pon")["state"] == "STALE"
    s = ad.derived_staleness(conn, "cohort:call@v1")
    assert s["state"] == "STALE"  # reached pon via reference_usage edge
    assert any("pon" in r for r in s["reasons"])


def test_orthogonality_derived_only(tmp_path):
    conn = _project(tmp_path)
    _add_cohort(conn, "joint", "v1", ["A1", "A2"])
    annot = _add_cohort(conn, "annot", "v1", ["A1"])  # annot has its own fresh inputs
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t")
    _censor_assay(conn, "A2")  # only joint's input
    # annot: input-fresh (its own A1 ok) but derived_stale (joint is input-stale)
    stale_map = ca.artifact_staleness(conn)
    assert stale_map.get(annot, []) == []          # 0009 input-stale: NO
    assert ad.derived_staleness(conn, "cohort:annot@v1")["state"] == "STALE"  # 0011: YES


def test_leaf_no_edges_not_stale(tmp_path):
    conn = _project(tmp_path)
    _add_cohort(conn, "joint", "v1", ["A1", "A2"])
    # no derivation edges at all -> derived_stale False, NOT 'untracked'
    assert ad.derived_staleness(conn, "cohort:joint@v1")["state"] == "fresh"


def test_all_derived_stale_listing(tmp_path):
    conn = _project(tmp_path)
    _add_cohort(conn, "joint", "v1", ["A1", "A2"])
    _add_cohort(conn, "annot", "v1", ["A1"])
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t")
    _censor_assay(conn, "A2")
    stale = ad.all_derived_stale(conn)
    nodes = {r["node"] for r in stale if r["state"] == "STALE"}
    assert "cohort:annot@v1" in nodes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'derived_staleness'`.

- [ ] **Step 3: Write minimal implementation**

Append to `casetrack_qc/artifact_derivation.py` (before `__all__`); extend `__all__`:

```python
# ── transitive staleness walk ─────────────────────────────────────────────────

def _resolve_artifact_id(conn, analysis: str, run_tag: str) -> int | None:
    row = conn.execute(
        "SELECT artifact_id FROM cohort_artifacts WHERE analysis=? AND run_tag=?",
        (analysis, run_tag),
    ).fetchone()
    return row[0] if row else None


def _reference_usage_upstreams(conn, node: str) -> list[str]:
    """reference_usage rows whose consumer is *node* → reference:<ref_key> edges.

    Treats each 0010 usage edge as a derivation edge so a derived-stale
    reference reaches its consumers (0011 §6.3). No-op if reference_usage absent.
    """
    if not _table_exists(conn, "reference_usage"):
        return []
    n = LineageNode.parse(node)
    if n.scope == "cohort":
        aid = _resolve_artifact_id(conn, n.analysis, n.run_tag)
        if aid is None:
            return []
        rows = conn.execute(
            "SELECT ref_key FROM reference_usage WHERE scope='cohort' AND artifact_id=?",
            (aid,)).fetchall()
    elif n.scope == "analysis":
        rows = conn.execute(
            "SELECT ref_key FROM reference_usage WHERE scope='analysis' AND "
            "entity_level=? AND entity_id=? AND analysis=?",
            (n.entity_level, n.entity_id, n.analysis)).fetchall()
    else:
        return []
    return [f"reference:{rk}" for (rk,) in rows]


def _all_upstreams(conn, node: str) -> list[str]:
    """Union of 0011 derivation edges and the 0010 reference_usage edge."""
    return upstream_nodes(conn, node) + _reference_usage_upstreams(conn, node)


def _direct_stale(conn, node: str) -> list[str]:
    """A node's OWN direct staleness reasons (0009 input + 0010 ref), no recursion.

    reference nodes have no intrinsic direct staleness (only derived).
    """
    n = LineageNode.parse(node)
    reasons: list[str] = []
    if n.scope == "cohort":
        from casetrack_qc import cohort_artifacts as _ca
        aid = _resolve_artifact_id(conn, n.analysis, n.run_tag)
        if aid is not None:
            censored = _ca.artifact_staleness(conn).get(aid, [])
            if censored:
                reasons.append(f"inputs censored: {', '.join(sorted(censored))}")
            ref = _ref_state(conn, scope="cohort", artifact_id=aid)
            reasons += [f"ref {r}" for r in ref]
    elif n.scope == "analysis":
        ref = _ref_state(conn, scope="analysis", entity_level=n.entity_level,
                         entity_id=n.entity_id, analysis=n.analysis)
        reasons += [f"ref {r}" for r in ref]
    return reasons


def _ref_state(conn, **kw) -> list[str]:
    if not _table_exists(conn, "reference_usage"):
        return []
    from casetrack_qc import reference_artifacts as _ra
    s = _ra.output_staleness(conn, **kw)
    return s["reasons"] if s["state"] == "STALE" else []


def _is_stale(conn, node: str, memo: dict, path: set) -> tuple[bool, list[str]]:
    """is_stale(node) = direct OR any upstream is_stale. Memoized, cycle-guarded."""
    if node in memo:
        return memo[node]
    if node in path:           # back-edge: terminate, no contribution
        return (False, [])
    path = path | {node}
    reasons = list(_direct_stale(conn, node))
    for up in _all_upstreams(conn, node):
        up_stale, up_reasons = _is_stale(conn, up, memo, path)
        if up_stale:
            why = f"; {'; '.join(up_reasons)}" if up_reasons else ""
            reasons.append(f"upstream {up} is STALE{why}")
    result = (len(reasons) > 0, reasons)
    memo[node] = result
    return result


def derived_staleness(conn, node: str) -> dict:
    """{'state': fresh|STALE, 'reasons': [...]} for *node*.

    derived_stale = any upstream node is_stale (excludes the node's OWN direct
    causes — those are 0009 `stale` / 0010 `ref_stale`). Boolean rollup, not
    three-state: a node with no upstream-derivation edges is cleanly `fresh`.
    """
    memo: dict = {}
    reasons: list[str] = []
    for up in _all_upstreams(conn, node):
        up_stale, up_reasons = _is_stale(conn, up, memo, {node})
        if up_stale:
            why = f"; {'; '.join(up_reasons)}" if up_reasons else ""
            reasons.append(f"upstream {up} is STALE{why}")
    return {"state": "STALE" if reasons else "fresh", "reasons": sorted(reasons)}


def all_derived_stale(conn) -> list[dict]:
    """Every down_node with >=1 derivation edge, annotated with derived staleness."""
    out: list[dict] = []
    for (node,) in conn.execute(
        "SELECT DISTINCT down_node FROM artifact_derivation").fetchall():
        out.append({"node": node, **derived_staleness(conn, node)})
    return out
```

Add to `__all__`: `"derived_staleness", "all_derived_stale"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_artifact_derivation.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add casetrack_qc/artifact_derivation.py tests/test_artifact_derivation.py
git commit -m "feat(lineage): transitive derived_stale walk over 3 edge tables (0011 §6.3)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: CLI commands — `derived-from`, `derivation`, `migrate-derivation`

**Files:**
- Create: `casetrack_qc/artifact_derivation_cli.py`
- Test: `tests/test_artifact_derivation_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_artifact_derivation_cli.py
"""CLI tests for the 0011 derivation commands."""
import subprocess
import sys
import pytest

import casetrack
from casetrack_qc import cohort_artifacts as ca
from casetrack_qc import artifact_derivation as ad


def _run(args):
    return subprocess.run([sys.executable, "-m", "casetrack", *args],
                          capture_output=True, text=True)


def _project_with_artifacts(tmp_path):
    db = tmp_path / "casetrack.db"
    conn = casetrack.open_project_db(db)
    conn.executescript(
        """
        CREATE TABLE patients(patient_id TEXT PRIMARY KEY, qc_status TEXT DEFAULT 'pass',
                              consent_status TEXT DEFAULT 'consented');
        CREATE TABLE specimens(specimen_id TEXT PRIMARY KEY, patient_id TEXT,
                               qc_status TEXT DEFAULT 'pass');
        CREATE TABLE assays(assay_id TEXT PRIMARY KEY, specimen_id TEXT,
                            qc_status TEXT DEFAULT 'pass');
        INSERT INTO patients(patient_id) VALUES ('P1');
        INSERT INTO specimens(specimen_id, patient_id) VALUES ('S1','P1');
        INSERT INTO assays(assay_id, specimen_id) VALUES ('A1','S1'),('A2','S1');
        """)
    ca.ensure_cohort_artifacts_schema(conn)
    ad.ensure_derivation_schema(conn)
    for analysis, run_tag in (("joint", "v1"), ("annot", "v1")):
        aid = ca.insert_artifact(conn, analysis=analysis, run_tag=run_tag,
                                 path=f"/x/{run_tag}", n_inputs=2, transaction_id="t",
                                 checksum=None, stats_json=None, created_by="test")
        ca.add_artifact_inputs(conn, aid, ["A1", "A2"])
    conn.commit()
    conn.close()
    return tmp_path


def test_derived_from_records_edge(tmp_path):
    p = _project_with_artifacts(tmp_path)
    r = _run(["derived-from", "--project-dir", str(p),
              "--downstream", "cohort:annot@v1", "--upstream", "cohort:joint@v1"])
    assert r.returncode == 0, r.stderr
    conn = casetrack.open_project_db(p / "casetrack.db")
    assert len(ad.list_edges(conn)) == 1


def test_derived_from_refuses_cycle(tmp_path):
    p = _project_with_artifacts(tmp_path)
    _run(["derived-from", "--project-dir", str(p),
          "--downstream", "cohort:annot@v1", "--upstream", "cohort:joint@v1"])
    r = _run(["derived-from", "--project-dir", str(p),
              "--downstream", "cohort:joint@v1", "--upstream", "cohort:annot@v1"])
    assert r.returncode != 0
    assert "cycle" in (r.stderr + r.stdout).lower()


def test_derivation_lists_and_stale(tmp_path):
    p = _project_with_artifacts(tmp_path)
    _run(["derived-from", "--project-dir", str(p),
          "--downstream", "cohort:annot@v1", "--upstream", "cohort:joint@v1"])
    conn = casetrack.open_project_db(p / "casetrack.db")
    conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id='A2'")
    conn.commit(); conn.close()
    r = _run(["derivation", "--project-dir", str(p), "--fmt", "json", "--stale-only"])
    assert r.returncode == 0, r.stderr
    assert "cohort:annot@v1" in r.stdout


def test_migrate_derivation_dry_run(tmp_path):
    # a project WITHOUT the derivation table
    db = tmp_path / "casetrack.db"
    conn = casetrack.open_project_db(db); conn.close()
    r = _run(["migrate-derivation", "--project-dir", str(tmp_path), "--dry-run"])
    assert r.returncode == 0
    assert "dry-run" in r.stdout.lower()
    conn = casetrack.open_project_db(db)
    assert not ad.derivation_schema_exists(conn)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation_cli.py -q`
Expected: FAIL — `derived-from` is not a known command (argparse error / non-zero exit).

- [ ] **Step 3: Write minimal implementation**

```python
# casetrack_qc/artifact_derivation_cli.py
"""CLI commands for artifact-to-artifact lineage (proposal 0011 §6.4).

- ``migrate-derivation`` — additive: create artifact_derivation on a pre-0011 project.
- ``derived-from``       — record one or more derived-from edges (cycle-checked).
- ``derivation``         — list edges + per-node derived-staleness.

Mirrors casetrack_qc.reference_artifacts_cli. Resolve project → open db →
one begin_immediate write → provenance entry.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import json
import sys

import casetrack
from casetrack_qc import artifact_derivation as ad


def record_derivation_edges(conn, *, down: str, ups: list[str],
                            transaction_id: str) -> int:
    """Record down<-up edges for every up in *ups*. Caller owns the transaction.

    Used by `derived-from` and by the `--derived-from` convenience on
    `append` / `append-cohort`. Cycle-checked per edge.
    """
    ad.ensure_derivation_schema(conn)
    n = 0
    for up in ups:
        ad.record_edge(conn, down=down, up=up, transaction_id=transaction_id)
        n += 1
    return n


def cmd_derived_from(args) -> None:
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    ups = list(args.upstream or [])
    if not ups:
        print("Error: pass at least one --upstream <node-ref>.", file=sys.stderr)
        sys.exit(2)
    txn_id = casetrack._new_transaction_id()
    conn = casetrack.open_project_db(db_path)
    try:
        try:
            with casetrack.begin_immediate(conn):
                record_derivation_edges(conn, down=args.downstream, ups=ups,
                                        transaction_id=txn_id)
        except ad.DerivationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)
        for up in ups:
            casetrack.log_project_provenance(project_dir, {
                "action": "artifact_derivation_link",
                "down_node": args.downstream, "up_node": up,
                "transaction_id": txn_id,
            })
        print(f"Recorded {len(ups)} derived-from edge(s) for {args.downstream}.")
    finally:
        conn.close()


def cmd_migrate_derivation(args) -> None:
    project_dir, _ = casetrack._resolve_project(args.project_dir, bypass_legacy_gate=True)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        if ad.derivation_schema_exists(conn):
            print("No migration needed — derivation schema already in place.")
            return
        if getattr(args, "dry_run", False):
            print("[dry-run] Would create artifact_derivation table (+ indexes).")
            return
        txn_id = casetrack._new_transaction_id()
        with casetrack.begin_immediate(conn):
            executed = ad.ensure_derivation_schema(conn)
        casetrack.log_project_provenance(project_dir, {
            "action": "migrate_derivation", "executed_sql": executed,
            "transaction_id": txn_id,
        })
        print(f"Created derivation schema ({len(executed)} statements).")
    finally:
        conn.close()


def cmd_derivation(args) -> None:
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        if not ad.derivation_schema_exists(conn):
            print("Error: project has no derivation schema. Run "
                  f"`casetrack migrate-derivation --project-dir {project_dir}`.",
                  file=sys.stderr)
            sys.exit(1)
        fmt = getattr(args, "fmt", None) or "table"
        node = getattr(args, "node", None)
        stale_only = getattr(args, "stale_only", False)
        if node:
            rows = [{"node": node, "direction": "upstream", "other": u}
                    for u in ad.upstream_nodes(conn, node)]
            rows += [{"node": node, "direction": "downstream", "other": e["down_node"]}
                     for e in ad.list_edges(conn) if e["up_node"] == node]
            staleness = ad.derived_staleness(conn, node)
            payload = {"node": node, "edges": rows, **staleness}
            if fmt == "json":
                print(json.dumps(payload, indent=2))
            else:
                print(f"{node}  derived_stale={staleness['state']}")
                for r in rows:
                    print(f"  {r['direction']:>10}: {r['other']}")
                for reason in staleness["reasons"]:
                    print(f"  reason: {reason}")
            return
        rows = ad.all_derived_stale(conn)
        if stale_only:
            rows = [r for r in rows if r["state"] == "STALE"]
        if fmt == "json":
            print(json.dumps(rows, indent=2))
        elif fmt == "tsv":
            print("#node\tstate\treasons")
            for r in rows:
                print(f"{r['node']}\t{r['state']}\t{'; '.join(r['reasons'])}")
        else:
            if not rows:
                print("No derivation edges." if not stale_only else "No derived-stale outputs.")
                return
            for r in rows:
                line = f"[{r['state']}] {r['node']}"
                if r["reasons"]:
                    line += f"  ({'; '.join(r['reasons'])})"
                print(line)
    finally:
        conn.close()


__all__ = ["record_derivation_edges", "cmd_derived_from",
           "cmd_migrate_derivation", "cmd_derivation"]
```

- [ ] **Step 4: Run test to verify it passes (after Task 5 wires the subparsers)**

These subprocess tests need the subparsers from Task 5. Run them at the end of Task 5.
Run now (will still fail until Task 5): `python3 -m pytest tests/test_artifact_derivation_cli.py -q`

- [ ] **Step 5: Commit**

```bash
git add casetrack_qc/artifact_derivation_cli.py tests/test_artifact_derivation_cli.py
git commit -m "feat(lineage): derived-from / derivation / migrate-derivation CLI (0011 §6.4)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Register subcommands + dispatch

**Files:**
- Modify: `casetrack_qc/cli.py:14-21` (imports), `:179-211` (subparsers + dispatch)

- [ ] **Step 1: Add the imports**

In `casetrack_qc/cli.py`, after the `reference_artifacts_cli` import (line ~20):

```python
from casetrack_qc.artifact_derivation_cli import (
    cmd_derived_from, cmd_derivation, cmd_migrate_derivation,
)
```

- [ ] **Step 2: Register the subparsers**

In `build_qc_subparsers`, after the `references` parser block (after line ~195):

```python
    # ── migrate-derivation ── (proposal 0011)
    p_migd = subparsers.add_parser(
        "migrate-derivation",
        help="[v0.9] Additive: create the artifact_derivation table on a pre-0011 project",
    )
    p_migd.add_argument("--project-dir", required=True)
    p_migd.add_argument("--dry-run", action="store_true",
                        help="Print the plan, make no changes")

    # ── derived-from ── (proposal 0011)
    p_dfrom = subparsers.add_parser(
        "derived-from",
        help="[v0.9] Record a derived-from edge between two lineage nodes",
    )
    p_dfrom.add_argument("--project-dir", required=True)
    p_dfrom.add_argument("--downstream", required=True,
                         help="Canonical node-ref of the derived output")
    p_dfrom.add_argument("--upstream", action="append", required=True,
                         help="Canonical node-ref of a source artifact (repeatable)")

    # ── derivation ── (proposal 0011)
    p_deriv = subparsers.add_parser(
        "derivation",
        help="[v0.9] List derivation edges + per-node derived-staleness",
    )
    p_deriv.add_argument("--project-dir", required=True)
    p_deriv.add_argument("--node", default=None,
                         help="Inspect one node's up/downstream + root-cause chain")
    p_deriv.add_argument("--fmt", choices=["table", "tsv", "json"], default="table")
    p_deriv.add_argument("--stale-only", dest="stale_only", action="store_true",
                         help="Show only derived-stale outputs")
```

- [ ] **Step 3: Add dispatch entries**

In `qc_command_dispatch()` return dict, after `"references": cmd_references,`:

```python
        "migrate-derivation": cmd_migrate_derivation,
        "derived-from": cmd_derived_from,
        "derivation": cmd_derivation,
```

- [ ] **Step 4: Run the Task-4 CLI tests**

Run: `python3 -m pytest tests/test_artifact_derivation_cli.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add casetrack_qc/cli.py
git commit -m "feat(lineage): wire derivation subcommands + dispatch (0011 §6.4)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: `init` creates the table; `migrate-derivation`-on-init; TOML `derived_from` materialization on `schema apply`

**Files:**
- Modify: `casetrack.py:1551-1565` (init), `:4844-4866` (schema apply), `:552-575` (`_validate_references`)
- Test: `tests/test_artifact_derivation_cli.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_artifact_derivation_cli.py
import tomli_w  # if unavailable, write TOML text by hand

def test_init_creates_derivation_table(tmp_path):
    r = _run(["init", "--project-dir", str(tmp_path), "--project-name", "p0011",
              "--project-id", "p0011"])
    assert r.returncode == 0, r.stderr
    conn = casetrack.open_project_db(tmp_path / "casetrack.db")
    assert ad.derivation_schema_exists(conn)


def test_toml_derived_from_materialized_on_schema_apply(tmp_path):
    # init, then add a [references.pon] with derived_from, then schema apply
    _run(["init", "--project-dir", str(tmp_path), "--project-name", "p", "--project-id", "p"])
    # create the upstream cohort artifact the ref derives from
    conn = casetrack.open_project_db(tmp_path / "casetrack.db")
    from casetrack_qc import cohort_artifacts as _ca
    _ca.ensure_cohort_artifacts_schema(conn)
    aid = _ca.insert_artifact(conn, analysis="make_pon", run_tag="v1", path="/x/pon",
                              n_inputs=1, transaction_id="t", checksum=None,
                              stats_json=None, created_by="t")
    conn.commit(); conn.close()
    toml_path = tmp_path / "casetrack.toml"
    text = toml_path.read_text()
    text += (
        '\n[references.pon]\n'
        'path = "/x/pon.vcf"\nversion = "pon_v1"\nkind = "known_variants"\n'
        'derived_from = ["cohort:make_pon@v1"]\n'
    )
    toml_path.write_text(text)
    r = _run(["schema", "apply", "--project-dir", str(tmp_path)])
    assert r.returncode == 0, r.stderr
    conn = casetrack.open_project_db(tmp_path / "casetrack.db")
    edges = ad.list_edges(conn)
    assert any(e["down_node"] == "reference:pon" and e["up_node"] == "cohort:make_pon@v1"
               for e in edges)
```

(Note: if the `schema` subcommand uses `schema apply` as one token, match the existing test invocation in `tests/test_reference_artifacts_cli.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation_cli.py -k "init_creates or materialized" -q`
Expected: FAIL — table absent after init / no edge after schema apply.

- [ ] **Step 3a: init hook**

In `casetrack.py` `cmd_init_project`, extend the import block (line ~1551) and the init transaction (line ~1565):

```python
    from casetrack_qc.artifact_derivation import (
        ensure_derivation_schema as _ensure_derivation_schema,
    )
```

After `_ensure_reference_schema(conn)` inside the `with begin_immediate(conn):` block:

```python
            # Proposal 0011: artifact_derivation sibling table, same init txn.
            _ensure_derivation_schema(conn)
```

- [ ] **Step 3b: `_validate_references` accepts `derived_from`**

In `casetrack.py:_validate_references` (line ~552), allow the optional `derived_from` key (a list of node-ref strings). Add, inside the per-`ref_key` loop, after the existing `kind` validation:

```python
        df = spec.get("derived_from")
        if df is not None:
            if not isinstance(df, list) or not all(isinstance(x, str) for x in df):
                raise SchemaError(
                    f"[references.{ref_key}] derived_from must be a list of node-refs")
```

- [ ] **Step 3c: schema-apply materialization**

In `casetrack.py` `_schema_apply` (the reference sync block at line ~4844), after the `sync_references_from_toml` call completes and the connection is still open (extend the same `with begin_immediate(conn):`), materialize derived_from edges:

```python
            # Proposal 0011: materialize [references.<key>].derived_from edges.
            from casetrack_qc.artifact_derivation import (
                ensure_derivation_schema as _ensure_deriv, record_edge as _record_edge,
                DerivationError as _DerivErr,
            )
            _ensure_deriv(conn)
            for ref_key, spec in references.items():
                for up in (spec.get("derived_from") or []):
                    try:
                        _record_edge(conn, down=f"reference:{ref_key}", up=up,
                                     transaction_id=txn_id)
                    except _DerivErr as e:
                        print(f"Warning: skipping derived_from for {ref_key}: {e}",
                              file=sys.stderr)
```

(Place this inside the existing `with begin_immediate(conn):` that wraps `sync_references_from_toml`; `txn_id` is already defined there.)

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_artifact_derivation_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add casetrack.py tests/test_artifact_derivation_cli.py
git commit -m "feat(lineage): init creates table; TOML derived_from on schema apply (0011 §6.4)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: `--derived-from` convenience on `append` and `append-cohort`

**Files:**
- Modify: `casetrack.py:4568-4577` (append capture), `:7860-7863` (append argparse)
- Modify: `casetrack_qc/cohort_artifacts_cli.py:88-100` (append-cohort), `casetrack_qc/cli.py:134` (append-cohort argparse)
- Test: `tests/test_artifact_derivation_cli.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_artifact_derivation_cli.py
def test_append_cohort_derived_from(tmp_path):
    p = _project_with_artifacts(tmp_path)  # has joint@v1, annot@v1
    # register a new cohort artifact that derives from joint@v1
    r = _run(["append-cohort", "--project-dir", str(p),
              "--analysis", "vqsr", "--run-tag", "v1", "--path", "/x/vqsr.vcf",
              "--inputs", "A1,A2", "--derived-from", "cohort:joint@v1"])
    assert r.returncode == 0, r.stderr
    conn = casetrack.open_project_db(p / "casetrack.db")
    edges = ad.list_edges(conn)
    assert any(e["down_node"] == "cohort:vqsr@v1" and e["up_node"] == "cohort:joint@v1"
               for e in edges)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation_cli.py -k append_cohort_derived_from -q`
Expected: FAIL — `unrecognized arguments: --derived-from`.

- [ ] **Step 3a: append-cohort argparse**

In `casetrack_qc/cli.py`, the `p_appc` block (line ~134), add:

```python
    p_appc.add_argument("--derived-from", dest="derived_from", default=None,
                        help="Comma-separated upstream node-refs this artifact derives from")
```

- [ ] **Step 3b: append-cohort capture**

In `casetrack_qc/cohort_artifacts_cli.py` `cmd_append_cohort`, inside the `with casetrack.begin_immediate(conn):` block, after the `uses_references` handling (line ~100):

```python
                derived_from = getattr(args, "derived_from", None)
                if derived_from:
                    from casetrack_qc.artifact_derivation_cli import record_derivation_edges
                    ups = [s.strip() for s in derived_from.split(",") if s.strip()]
                    record_derivation_edges(
                        conn, down=f"cohort:{args.analysis}@{args.run_tag}",
                        ups=ups, transaction_id=txn_id)
```

Add `derived_from` to the provenance dict for `append_cohort`:

```python
                "derived_from": (
                    [s.strip() for s in (getattr(args, "derived_from", None) or "").split(",") if s.strip()]
                ),
```

- [ ] **Step 3c: append (sample-level) argparse + capture**

In `casetrack.py` `p_append` block (line ~7860), add:

```python
    p_append.add_argument("--derived-from", dest="derived_from", default=None,
                          help="Comma-separated upstream node-refs each appended output derives from")
```

In `cmd_append_project`, inside the append transaction after `capture_reference_usage(...)` (line ~4577):

```python
            if getattr(args, "derived_from", None):
                from casetrack_qc.artifact_derivation_cli import record_derivation_edges
                _ups = [s.strip() for s in args.derived_from.split(",") if s.strip()]
                for _eid in tsv_keys:
                    record_derivation_edges(
                        conn, down=f"analysis:{level}/{_eid}/{analysis}",
                        ups=_ups, transaction_id=txn_id)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_artifact_derivation_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add casetrack.py casetrack_qc/cohort_artifacts_cli.py casetrack_qc/cli.py tests/test_artifact_derivation_cli.py
git commit -m "feat(lineage): --derived-from convenience on append + append-cohort (0011 §6.4)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: DuckDB views — `_artifact_derivation` + `derived_stale` columns

**Files:**
- Modify: `casetrack_qc/reader.py` (add `install_artifact_derivation_view`; extend `_cohort_artifacts` / `_reference_usage`)
- Modify: `casetrack.py:6106-6113` (call the new installer)
- Test: `tests/test_artifact_derivation_readpaths.py`

The recursive transitive closure: `derived_stale(n)` is true iff `n` can reach (≥1 hop, over `artifact_derivation` up-edges **∪** `reference_usage` consumer→reference edges) some node whose **direct** staleness is true. Direct staleness is reused from the already-installed `_cohort_artifacts` (`stale OR ref_stale`) and `_reference_usage` (`is_stale`) views. The derivation view is installed **after** those two.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_artifact_derivation_readpaths.py
"""DuckDB view + read-path tests for 0011."""
import subprocess
import sys
import json

import casetrack
from casetrack_qc import cohort_artifacts as ca
from casetrack_qc import artifact_derivation as ad


def _run(args):
    return subprocess.run([sys.executable, "-m", "casetrack", *args],
                          capture_output=True, text=True)


def _proj(tmp_path):
    _run(["init", "--project-dir", str(tmp_path), "--project-name", "p", "--project-id", "p"])
    conn = casetrack.open_project_db(tmp_path / "casetrack.db")
    # ensure entity rows exist for the cascade; init may already create empty tables
    conn.execute("INSERT OR IGNORE INTO patients(patient_id) VALUES ('P1')")
    conn.execute("INSERT OR IGNORE INTO specimens(specimen_id, patient_id) VALUES ('S1','P1')")
    conn.execute("INSERT OR IGNORE INTO assays(assay_id, specimen_id) VALUES ('A1','S1'),('A2','S1')")
    for analysis, run_tag in (("joint", "v1"), ("annot", "v1")):
        aid = ca.insert_artifact(conn, analysis=analysis, run_tag=run_tag, path="/x",
                                 n_inputs=2, transaction_id="t", checksum=None,
                                 stats_json=None, created_by="t")
        ca.add_artifact_inputs(conn, aid, ["A1", "A2"])
    ad.ensure_derivation_schema(conn)
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t")
    conn.commit(); conn.close()
    return tmp_path


def test_query_artifact_derivation_view(tmp_path):
    p = _proj(tmp_path)
    r = _run(["query", "--project-dir", str(p), "--fmt", "json",
              "--sql", 'SELECT * FROM "_artifact_derivation"'])
    assert r.returncode == 0, r.stderr
    assert "cohort:annot@v1" in r.stdout


def test_cohort_artifacts_view_has_derived_stale(tmp_path):
    p = _proj(tmp_path)
    conn = casetrack.open_project_db(p / "casetrack.db")
    conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id='A2'")
    conn.commit(); conn.close()
    r = _run(["query", "--project-dir", str(p), "--fmt", "json", "--sql",
              'SELECT analysis, run_tag, stale, ref_stale, derived_stale '
              'FROM "_cohort_artifacts" ORDER BY analysis'])
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout) if r.stdout.strip().startswith("[") else None
    # annot is input-FRESH but derived_stale=TRUE (joint is input-stale)
    annot = next(x for x in rows if x["analysis"] == "annot")
    assert annot["derived_stale"] in (True, 1)
    assert annot["stale"] in (False, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -q`
Expected: FAIL — view `_artifact_derivation` not found / column `derived_stale` missing.

- [ ] **Step 3a: add the views to `casetrack_qc/reader.py`**

Add a shared closure CTE builder + the new installer. Append before `__all__`:

```python
def _derived_stale_cte() -> str:
    """A reusable WITH-body computing derived_stale per node (canonical string).

    edges  : up-edges from artifact_derivation UNION reference_usage (consumer->ref)
    direct : every node with its single-hop direct staleness (reuses _cohort_artifacts
             and _reference_usage, which are installed before this view)
    reach  : transitive closure (>=1 hop) of edges
    Result relation `derived` exposes (node, derived_stale BOOLEAN).
    """
    return """
    edges AS (
        SELECT down_node, up_node FROM proj.artifact_derivation
        UNION
        SELECT
          CASE ru.scope
            WHEN 'cohort'   THEN 'cohort:' || ca.analysis || '@' || ca.run_tag
            WHEN 'analysis' THEN 'analysis:' || ru.entity_level || '/' || ru.entity_id || '/' || ru.analysis
          END AS down_node,
          'reference:' || ru.ref_key AS up_node
        FROM proj.reference_usage ru
        LEFT JOIN proj.cohort_artifacts ca ON ca.artifact_id = ru.artifact_id
        WHERE ru.scope IN ('cohort','analysis')
    ),
    direct AS (
        SELECT 'cohort:' || analysis || '@' || run_tag AS node,
               (stale OR ref_stale) AS d
        FROM "_cohort_artifacts"
        UNION ALL
        SELECT 'reference:' || ref_key, FALSE FROM proj.reference_artifacts
        UNION ALL
        SELECT 'analysis:' || entity_level || '/' || entity_id || '/' || analysis,
               BOOL_OR(is_stale)
        FROM "_reference_usage" WHERE scope = 'analysis'
        GROUP BY 1
    ),
    reach(start, cur) AS (
        SELECT down_node, up_node FROM edges
        UNION
        SELECT r.start, e.up_node FROM reach r JOIN edges e ON e.down_node = r.cur
    ),
    derived AS (
        SELECT reach.start AS node, BOOL_OR(COALESCE(direct.d, FALSE)) AS derived_stale
        FROM reach LEFT JOIN direct ON direct.node = reach.cur
        GROUP BY reach.start
    )
    """


def install_artifact_derivation_view(duckdb_con) -> None:
    """Attach ``_artifact_derivation`` (edges + per-down_node derived_stale). 0011.

    Silent no-op on pre-0011 projects (no artifact_derivation table) or pre-0010
    projects (the CTE references _cohort_artifacts / _reference_usage).
    """
    sql = f"""
        CREATE VIEW "_artifact_derivation" AS
        WITH RECURSIVE {_derived_stale_cte()}
        SELECT e.down_node, e.up_node,
               COALESCE(d.derived_stale, FALSE) AS down_derived_stale
        FROM proj.artifact_derivation e
        LEFT JOIN derived d ON d.node = e.down_node
    """
    try:
        duckdb_con.execute(sql)
    except Exception:
        pass
```

- [ ] **Step 3b: extend `_cohort_artifacts` with `derived_stale`**

In `install_cohort_artifact_view`, change `sql_with_ref_stale` to a recursive CTE that joins `derived`. Replace the `sql_with_ref_stale` definition with:

```python
    sql_with_ref_stale = f"""
        CREATE VIEW "_cohort_artifacts" AS
        WITH RECURSIVE {_derived_stale_cte()}
        SELECT ca.artifact_id, ca.analysis, ca.run_tag, ca.path, ca.checksum,
               ca.n_inputs, ca.stats_json, ca.created_at,
               {censored_count} AS n_censored_inputs,
               ({censored_count} > 0) AS stale,
               {ref_stale_subquery} AS ref_stale,
               COALESCE(d.derived_stale, FALSE) AS derived_stale
        FROM proj.cohort_artifacts ca
        LEFT JOIN derived d
               ON d.node = 'cohort:' || ca.analysis || '@' || ca.run_tag
    """
```

**Important:** `_derived_stale_cte()` references `"_cohort_artifacts"` in its `direct` block, but here we are *defining* `_cohort_artifacts`. To avoid self-reference, the `direct` block must compute the cohort node's direct staleness inline rather than from the view. Use this alternate CTE inside the two cohort/derivation views — add a parameterized variant:

```python
def _derived_stale_cte(self_safe: bool = False) -> str:
    cohort_direct = (
        # inline (self-safe): recompute cohort direct staleness from base tables
        """
        SELECT 'cohort:' || ca.analysis || '@' || ca.run_tag AS node,
               ((SELECT COUNT(*) FROM proj.cohort_artifact_inputs ci
                  WHERE ci.artifact_id = ca.artifact_id
                    AND ci.assay_id NOT IN (%(active)s)) > 0
                OR EXISTS (SELECT 1 FROM proj.reference_usage ru2
                           LEFT JOIN proj.reference_artifacts rr2 ON rr2.ref_key = ru2.ref_key
                           WHERE ru2.scope='cohort' AND ru2.artifact_id = ca.artifact_id
                             AND (rr2.version IS NULL OR rr2.version <> ru2.version_used))
               ) AS d
        FROM proj.cohort_artifacts ca
        """ % {"active": _ACTIVE_ASSAY_SQL}
        if self_safe else
        "SELECT 'cohort:' || analysis || '@' || run_tag AS node, (stale OR ref_stale) AS d FROM \"_cohort_artifacts\""
    )
    analysis_direct = (
        """
        SELECT 'analysis:' || ru.entity_level || '/' || ru.entity_id || '/' || ru.analysis AS node,
               BOOL_OR(rr.version IS NULL OR rr.version <> ru.version_used) AS d
        FROM proj.reference_usage ru
        LEFT JOIN proj.reference_artifacts rr ON rr.ref_key = ru.ref_key
        WHERE ru.scope='analysis' GROUP BY 1
        """ if self_safe else
        "SELECT 'analysis:' || entity_level || '/' || entity_id || '/' || analysis, BOOL_OR(is_stale) FROM \"_reference_usage\" WHERE scope='analysis' GROUP BY 1"
    )
    return f"""
    edges AS (
        SELECT down_node, up_node FROM proj.artifact_derivation
        UNION
        SELECT CASE ru.scope
                 WHEN 'cohort'   THEN 'cohort:' || ca.analysis || '@' || ca.run_tag
                 WHEN 'analysis' THEN 'analysis:' || ru.entity_level || '/' || ru.entity_id || '/' || ru.analysis
               END,
               'reference:' || ru.ref_key
        FROM proj.reference_usage ru
        LEFT JOIN proj.cohort_artifacts ca ON ca.artifact_id = ru.artifact_id
        WHERE ru.scope IN ('cohort','analysis')
    ),
    direct AS (
        {cohort_direct}
        UNION ALL
        SELECT 'reference:' || ref_key, FALSE FROM proj.reference_artifacts
        UNION ALL
        {analysis_direct}
    ),
    reach(start, cur) AS (
        SELECT down_node, up_node FROM edges
        UNION
        SELECT r.start, e.up_node FROM reach r JOIN edges e ON e.down_node = r.cur
    ),
    derived AS (
        SELECT reach.start AS node, BOOL_OR(COALESCE(direct.d, FALSE)) AS derived_stale
        FROM reach LEFT JOIN direct ON direct.node = reach.cur
        GROUP BY reach.start
    )
    """
```

Define `_ACTIVE_ASSAY_SQL` near the top of `reader.py` (the proj-qualified active-assay subquery already used in `install_cohort_artifact_view`'s `active_sql` — extract it to a module constant and reuse). In `install_cohort_artifact_view` and `install_artifact_derivation_view`, call `_derived_stale_cte(self_safe=True)` (they touch base tables, so cannot depend on the views). For `_reference_usage`, no change needed (it stays single-hop; do not add derived_stale there in the first cut — sample-level analysis derived-staleness is surfaced via the `derivation` command and `_artifact_derivation` view).

- [ ] **Step 3c: call the installer in `casetrack.py`**

In `casetrack.py` (line ~6113), after `_install_reference_usage_view(con)`:

```python
    # Proposal 0011: `_artifact_derivation` view + derived_stale on _cohort_artifacts.
    from casetrack_qc.reader import (
        install_artifact_derivation_view as _install_artifact_derivation_view,
    )
    _install_artifact_derivation_view(con)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -q && python3 -m pytest tests/test_cohort_artifacts_readpaths.py tests/test_reference_artifacts_readpaths.py -q`
Expected: PASS, and the pre-existing 0009/0010 read-path tests still pass (the view extension is backward compatible).

- [ ] **Step 5: Commit**

```bash
git add casetrack_qc/reader.py casetrack.py tests/test_artifact_derivation_readpaths.py
git commit -m "feat(lineage): _artifact_derivation view + derived_stale on _cohort_artifacts (0011 §6.5)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: `status` section

**Files:**
- Modify: `casetrack.py:7039-7046` (status hook) + new `_emit_derivation_section`
- Test: `tests/test_artifact_derivation_readpaths.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_artifact_derivation_readpaths.py
def test_status_shows_derivation_section(tmp_path):
    p = _proj(tmp_path)
    conn = casetrack.open_project_db(p / "casetrack.db")
    conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id='A2'")
    conn.commit(); conn.close()
    r = _run(["status", "--project-dir", str(p)])
    assert r.returncode == 0, r.stderr
    assert "Derivation" in r.stdout
    assert "cohort:annot@v1" in r.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -k status_shows_derivation -q`
Expected: FAIL — "Derivation" not in output.

- [ ] **Step 3: Implementation**

In `casetrack.py`, after the references section block (line ~7045):

```python
    # Proposal 0011: surface derivation edges + derived-stale outputs.
    if args.fmt in (None, "table"):
        conn6 = open_project_db(project_dir / PROJECT_DB_NAME)
        try:
            _emit_derivation_section(conn6)
        finally:
            conn6.close()
```

Add the function after `_emit_references_section`:

```python
def _emit_derivation_section(conn: sqlite3.Connection) -> None:
    """Print a derivation summary with derived-staleness (proposal 0011 §6.5).

    Self-contained block appended to the human status view. No-ops on pre-0011
    projects.
    """
    from casetrack_qc.artifact_derivation import (
        derivation_schema_exists as _deriv_exists,
        list_edges as _list_edges,
        all_derived_stale as _all_derived_stale,
    )
    if not _deriv_exists(conn):
        return
    edges = _list_edges(conn)
    if not edges:
        return
    stale = [r for r in _all_derived_stale(conn) if r["state"] == "STALE"]
    print("\n=== Derivation (proposal 0011) ===")
    print(f"  {len(edges)} edge(s); {len(stale)} derived-stale output(s)")
    for r in stale:
        print(f"  [STALE] {r['node']}  ({'; '.join(r['reasons'])})")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add casetrack.py tests/test_artifact_derivation_readpaths.py
git commit -m "feat(lineage): status Derivation section (0011 §6.5)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: `export --include-derivation`

**Files:**
- Modify: `casetrack.py:6400-6418` (export block) + `:8170-8175` (export argparse)
- Test: `tests/test_artifact_derivation_readpaths.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_artifact_derivation_readpaths.py
def test_export_include_derivation(tmp_path):
    p = _proj(tmp_path)
    out = tmp_path / "exp"
    out.mkdir()
    r = _run(["export", "--project-dir", str(p), "--out", str(out),
              "--fmt", "tsv", "--include-derivation"])
    assert r.returncode == 0, r.stderr
    assert (out / "artifact_derivation.tsv").exists()
```

(Match the actual `export` argument names from `tests/test_reference_artifacts_readpaths.py` — e.g. it may be `--output`/`-o` not `--out`. Mirror that test's invocation exactly.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -k export_include_derivation -q`
Expected: FAIL — unrecognized `--include-derivation` / file absent.

- [ ] **Step 3a: argparse**

In `casetrack.py` `p_export` block (after `--include-references`, line ~8173):

```python
    p_export.add_argument(
        "--include-derivation", dest="include_derivation", action="store_true",
        help="[v0.9] Also export the artifact_derivation table (auto for XLSX)",
    )
```

- [ ] **Step 3b: export block**

In `cmd_export_project`, after the references export block (line ~6418):

```python
        # Proposal 0011: artifact_derivation table. Auto for XLSX.
        include_deriv = getattr(args, "include_derivation", False) or (
            ext == ".xlsx" and not args.sql and shape != "joined"
        )
        if include_deriv:
            from casetrack_qc.artifact_derivation import (
                derivation_schema_exists as _deriv_exists,
            )
            if _deriv_exists(conn):
                df_d = pd.read_sql_query("SELECT * FROM artifact_derivation", conn)
                if prefix_mode:
                    out_path = Path(f"{prefix_base}.artifact_derivation{ext}")
                else:
                    out_path = output / f"artifact_derivation{ext}"
                _write_df(df_d, out_path)
                written.append(("artifact_derivation", out_path, len(df_d)))
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add casetrack.py tests/test_artifact_derivation_readpaths.py
git commit -m "feat(lineage): export --include-derivation (0011 §6.5)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 11: `validate` — dangling edges + acyclicity

**Files:**
- Modify: `casetrack.py:7617-7632` (validate, add check #7)
- Test: `tests/test_artifact_derivation_readpaths.py` (extend)

A node-ref "resolves" when: cohort → a `cohort_artifacts(analysis, run_tag)` row exists; reference → a `reference_artifacts.ref_key` row exists; analysis → a `reference_usage` analysis row OR an entity row exists (best-effort: accept analysis nodes whose entity_id exists in the matching level table).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_artifact_derivation_readpaths.py
def test_validate_flags_dangling_edge(tmp_path):
    p = _proj(tmp_path)
    conn = casetrack.open_project_db(p / "casetrack.db")
    # edge to a cohort artifact that does not exist
    conn.execute("INSERT INTO artifact_derivation(down_node, up_node, recorded_at) "
                 "VALUES ('cohort:annot@v1','cohort:ghost@v9','2026-01-01T00:00:00')")
    conn.commit(); conn.close()
    r = _run(["validate", "--project-dir", str(p)])
    out = r.stdout + r.stderr
    assert "ghost@v9" in out or "dangling" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -k validate_flags_dangling -q`
Expected: FAIL — no mention of the dangling node.

- [ ] **Step 3: Implementation**

In `casetrack.py` validate (after the proposal-0010 orphan check, line ~7632):

```python
        # 7. Proposal 0011: dangling derivation edges + acyclicity.
        from casetrack_qc.artifact_derivation import (
            derivation_schema_exists as _deriv_exists,
            list_edges as _list_edges, upstream_nodes as _upstream_nodes,
            LineageNode as _LineageNode, DerivationError as _DerivErr,
        )
        if _deriv_exists(conn):
            def _resolves(node: str) -> bool:
                try:
                    n = _LineageNode.parse(node)
                except _DerivErr:
                    return False
                if n.scope == "cohort":
                    return conn.execute(
                        "SELECT 1 FROM cohort_artifacts WHERE analysis=? AND run_tag=?",
                        (n.analysis, n.run_tag)).fetchone() is not None
                if n.scope == "reference":
                    return conn.execute(
                        "SELECT 1 FROM reference_artifacts WHERE ref_key=?",
                        (n.ref_key,)).fetchone() is not None
                # analysis: accept if the entity row exists at its level
                tbl = {"patient": "patients", "specimen": "specimens",
                       "assay": "assays"}.get(n.entity_level)
                col = {"patient": "patient_id", "specimen": "specimen_id",
                       "assay": "assay_id"}.get(n.entity_level)
                if not tbl:
                    return False
                return conn.execute(
                    f"SELECT 1 FROM {tbl} WHERE {col}=?", (n.entity_id,)).fetchone() is not None

            seen_nodes: set = set()
            for e in _list_edges(conn):
                for node in (e["down_node"], e["up_node"]):
                    if node not in seen_nodes:
                        seen_nodes.add(node)
                        if not _resolves(node):
                            issues.append(
                                f"artifact_derivation: dangling node-ref {node!r} "
                                f"(no matching artifact/reference/entity)")
            # acyclicity: DFS from each node over up edges
            WHITE, GREY, BLACK = 0, 1, 2
            color: dict = {}

            def _dfs(node: str) -> bool:
                color[node] = GREY
                for up in _upstream_nodes(conn, node):
                    c = color.get(up, WHITE)
                    if c == GREY:
                        issues.append(
                            f"artifact_derivation: cycle through {node!r} -> {up!r}")
                        return True
                    if c == WHITE and _dfs(up):
                        return True
                color[node] = BLACK
                return False

            for (dn,) in conn.execute("SELECT DISTINCT down_node FROM artifact_derivation"):
                if color.get(dn, WHITE) == WHITE:
                    _dfs(dn)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add casetrack.py tests/test_artifact_derivation_readpaths.py
git commit -m "feat(lineage): validate dangling + acyclic invariants (0011 §6.5)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 12: Dashboard section

**Files:**
- Modify: `casetrack.py:5664-5675` (qc_info population), `:5915` (call site), `:5984` (new `_derivation_html`)
- Test: `tests/test_artifact_derivation_readpaths.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_artifact_derivation_readpaths.py
def test_dashboard_has_derivation_section(tmp_path):
    p = _proj(tmp_path)
    out = tmp_path / "dash.html"
    r = _run(["dashboard", "--project-dir", str(p), "--out", str(out)])
    assert r.returncode == 0, r.stderr
    html = out.read_text()
    assert "Derivation" in html
```

(Mirror the dashboard invocation/arg names from `tests/test_reference_artifacts_readpaths.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -k dashboard_has_derivation -q`
Expected: FAIL — "Derivation" not in HTML.

- [ ] **Step 3a: populate qc_info**

In `casetrack.py` near the references qc_info block (line ~5672):

```python
        from casetrack_qc.artifact_derivation import (
            derivation_schema_exists as _ad_exists,
            all_derived_stale as _ad_all,
            list_edges as _ad_edges,
        )
        if _ad_exists(conn):
            qc_info["derivation_edges"] = _ad_edges(conn)
            qc_info["derivation_stale"] = [r for r in _ad_all(conn) if r["state"] == "STALE"]
```

- [ ] **Step 3b: render call**

In the HTML f-string (line ~5915), after `{_references_html(qc_info)}`:

```python
  {_derivation_html(qc_info)}
```

- [ ] **Step 3c: the renderer**

After `_references_html` (line ~5984):

```python
def _derivation_html(qc_info: dict | None) -> str:
    """Render the derivation section (proposal 0011) with derived-stale badges.

    Returns "" when no derivation edges exist so pre-0011 projects render
    unchanged.
    """
    if not qc_info:
        return ""
    edges = qc_info.get("derivation_edges") or []
    if not edges:
        return ""
    stale = qc_info.get("derivation_stale") or []
    rows = "".join(
        f"<tr><td>{_html_escape(e['down_node'])}</td>"
        f"<td>{_html_escape(e['up_node'])}</td></tr>"
        for e in edges
    )
    stale_rows = "".join(
        f"<li><span class='badge stale'>DERIVED-STALE</span> "
        f"{_html_escape(r['node'])} <small>{_html_escape('; '.join(r['reasons']))}</small></li>"
        for r in stale
    )
    stale_block = f"<ul>{stale_rows}</ul>" if stale_rows else "<p>None derived-stale.</p>"
    return f"""
    <section>
      <h2>Derivation (lineage 0011)</h2>
      {stale_block}
      <table><thead><tr><th>derived</th><th>derives from</th></tr></thead>
      <tbody>{rows}</tbody></table>
    </section>
    """
```

(Use the existing HTML-escape helper in `casetrack.py`; if it is named differently than `_html_escape`, match the actual name — grep `def _html` / `escape`.)

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add casetrack.py tests/test_artifact_derivation_readpaths.py
git commit -m "feat(lineage): dashboard Derivation section (0011 §6.5)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 13: MCP `casetrack_derivation` tool

**Files:**
- Modify: `casetrack_mcp/tools.py` (add `derivation_tool` after `cohort_artifacts_tool`)
- Modify: `casetrack_mcp/server.py` (import + register + dispatch)
- Test: `tests/test_artifact_derivation_readpaths.py` (extend) — call the tool function directly

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_artifact_derivation_readpaths.py
def test_mcp_derivation_tool(tmp_path):
    p = _proj(tmp_path)
    conn = casetrack.open_project_db(p / "casetrack.db")
    conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id='A2'")
    conn.commit(); conn.close()
    # register the project so the MCP resolver finds it (mirror test_reference_artifacts MCP test)
    from casetrack_mcp import tools
    # the tool resolves project by id/path; reuse the registration helper the
    # 0010 MCP test uses (see tests/test_reference_artifacts_readpaths.py).
    res = tools.derivation_tool(project_id=str(p / "casetrack.db"), stale_only=True)
    nodes = {r["node"] for r in res["derived_stale_outputs"]}
    assert "cohort:annot@v1" in nodes
```

(Match the exact project-resolution mechanism used by the 0010 `references_tool` test — copy its setup. If the tool takes a registered `project_id`, register via the same fixture; if it accepts a path, pass the db path.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -k mcp_derivation -q`
Expected: FAIL — `AttributeError: module 'casetrack_mcp.tools' has no attribute 'derivation_tool'`.

- [ ] **Step 3a: the tool**

In `casetrack_mcp/tools.py`, after `cohort_artifacts_tool` (line ~298), mirroring its project-resolution preamble:

```python
def derivation_tool(project_id: str, *, stale_only: bool = False) -> dict:
    """Return artifact-to-artifact lineage edges + derived-staleness (proposal 0011).

    A node is ``derived_stale`` when any upstream artifact it derives from is
    stale by any cause (0009 input / 0010 ref / 0011 transitive). Agent-facing
    companion to the `casetrack derivation` CLI.

    Returns ``{project_id, project_path, edges:[...], derived_stale_outputs:[...]}``.
    On a pre-0011 project (no artifact_derivation table), lists are empty.
    """
    from casetrack_qc.artifact_derivation import (
        derivation_schema_exists as _deriv_exists,
        list_edges as _list_edges, all_derived_stale as _all_derived_stale,
    )
    # resolve project + open conn EXACTLY as references_tool does (copy that preamble)
    conn, project_path, err = _resolve_conn_for_tool(project_id)  # use the same helper references_tool uses
    if err:
        return err
    try:
        if not _deriv_exists(conn):
            edges, stale = [], []
        else:
            edges = _list_edges(conn)
            stale = [r for r in _all_derived_stale(conn) if r["state"] == "STALE"]
        if not stale_only:
            all_rows = _all_derived_stale(conn) if _deriv_exists(conn) else []
        else:
            all_rows = stale
        return {
            "project_id": project_id,
            "project_path": str(project_path),
            "edges": edges,
            "derived_stale_outputs": stale,
            "outputs": all_rows,
        }
    finally:
        conn.close()
```

**Note:** `references_tool` (line ~260) shows the exact project-resolution preamble (it does not use a `_resolve_conn_for_tool` helper literally — copy whatever lines it uses to get `conn` + `project_path` and the not-found error dict). Replace the placeholder line above with that real preamble.

- [ ] **Step 3b: register in `server.py`**

Import (line ~40):

```python
    derivation_tool,
```

In `_list_tools()` (after the `casetrack_references` Tool entry, ~line 185):

```python
            Tool(
                name="casetrack_derivation",
                description=(
                    "List artifact-to-artifact lineage edges (derived-from) and "
                    "outputs that are derived-stale because an upstream artifact "
                    "(cohort artifact, reference, or sample output) is stale — "
                    "transitive across the 0009/0010/0011 cascade."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "stale_only": {"type": "boolean"},
                    },
                    "required": ["project_id"],
                },
            ),
```

In the `@app.call_tool()` dispatch, add a branch mirroring `casetrack_references`:

```python
        if name == "casetrack_derivation":
            return _as_content(derivation_tool(
                project_id=arguments["project_id"],
                stale_only=arguments.get("stale_only", False)))
```

(Match the actual content-wrapping helper name used by the other branches.)

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_artifact_derivation_readpaths.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add casetrack_mcp/tools.py casetrack_mcp/server.py tests/test_artifact_derivation_readpaths.py
git commit -m "feat(lineage): casetrack_derivation MCP tool (0011 §6.5)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 14: Nextflow passthrough

**Files:**
- Modify: `examples/nextflow/casetrack.nf` (`casetrack_append_cohort` process: optional `derived_from` input; `CASETRACK_REGISTER`: `--derived-from` param)
- Test: `tests/` — extend the existing Nextflow shell-contract test (find it: `grep -rl "casetrack_append_cohort\|CASETRACK_REGISTER" tests/`)

- [ ] **Step 1: Inspect the existing process + its `uses_references` slot**

Run: `grep -n "uses_references\|casetrack_append_cohort\|process \|input:\|--uses-references\|\[\]" examples/nextflow/casetrack.nf | head -40`
Mirror the `uses_references` `[]`-means-none pattern exactly for `derived_from`.

- [ ] **Step 2: Write/extend the failing test**

In the existing Nextflow contract test, add an assertion that the rendered `casetrack append-cohort` command includes `--derived-from cohort:joint@v1` when `derived_from` is non-empty, and omits the flag when `derived_from = []`. (Mirror the assertion style already used for `--uses-references`/`--stats`.)

- [ ] **Step 3: Implementation**

In `casetrack_append_cohort`, add an optional `derived_from` input channel (val, default `[]`) and in the script block:

```groovy
    def derived_arg = derived_from ? "--derived-from ${derived_from.join(',')}" : ''
```

append `${derived_arg}` to the `casetrack append-cohort` command line. For `CASETRACK_REGISTER`, add an optional `derived_from` param threaded into the `casetrack append` invocation as `--derived-from ...` when set.

- [ ] **Step 4: Run the contract test**

Run: `python3 -m pytest tests/ -k nextflow -q` (or the specific test file found in Step 1)
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/nextflow/casetrack.nf tests/
git commit -m "feat(lineage): Nextflow --derived-from passthrough (0011 §6.6)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 15: Docs + version bump to v0.9.0

**Files:**
- Modify: `setup.py:5` (`version="0.9.0"`), `casetrack.py` (`__version__` / CLI `--version` string — grep `0.8.0`), `CHANGELOG.md`, `README.md` (command table), `CLAUDE.md` (Architecture + Commands table), the casetrack skill (`~/.claude/skills/.../casetrack` or the plugin skill — grep for the skill that documents references), `docs/proposals/0011-*.md` (flip Status to "accepted (implemented)").
- Update `docs/proposals/0010` and `MEMORY.md` pointers if they reference "0011 is next".

- [ ] **Step 1: Bump versions (grep first)**

Run: `grep -rn "0\.8\.0" setup.py casetrack.py README.md CHANGELOG.md` — change each authoritative occurrence to `0.9.0`. (See the memory note "version lives in 4 drifting places": setup.py + casetrack.py + CLI help + tags/CHANGELOG.)

- [ ] **Step 2: CHANGELOG entry**

Add a `## v0.9.0 — artifact-to-artifact lineage (0011)` section summarizing: new `artifact_derivation` table; `derived-from`/`derivation`/`migrate-derivation` commands; `--derived-from` on `append`/`append-cohort`; TOML `[references].derived_from`; `derived_stale` third orthogonal flag; `_artifact_derivation` view + `derived_stale` on `_cohort_artifacts`; `casetrack_derivation` MCP tool; Nextflow passthrough; `validate` dangling + acyclic invariants.

- [ ] **Step 3: README + CLAUDE.md command tables**

Add a "derivation / lineage (0011)" row to the README command table and the CLAUDE.md Commands table: `derived-from`, `derivation`, `migrate-derivation` (+ `append`/`append-cohort` `--derived-from`). Add an "Artifact-to-artifact lineage (proposal 0011)" paragraph to the CLAUDE.md Architecture section mirroring the 0009/0010 paragraphs, and update the "Current/Next release" lines (current → v0.9.0; next → v1.0 flat-mode removal).

- [ ] **Step 4: Run the full suite + a smoke check of `--version`**

Run: `python3 -m pytest tests/ -q && python3 -m casetrack --version`
Expected: all green; version prints `0.9.0`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs(lineage): v0.9.0 — document 0011 + bump version

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 16: Full verification + finish

- [ ] **Step 1: Full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS — all prior tests (893+ baseline) plus the ~30 new 0011 tests. Confirm the count went UP and nothing regressed.

- [ ] **Step 2: Targeted re-run of the cross-proposal orthogonality test**

Run: `python3 -m pytest tests/test_artifact_derivation.py -k "pon_as_reference or orthogonality" -v`
Expected: PASS — confirms the load-bearing PoN-as-ref cascade and 0009/0010/0011 flag independence.

- [ ] **Step 3: Lint the proposal Status**

Confirm `docs/proposals/0011-artifact-to-artifact-lineage.md` Status line reads `accepted (implemented)`.

- [ ] **Step 4: Push + open PR**

```bash
git push -u origin feature/artifact-lineage-0011
gh pr create --title "feat: artifact-to-artifact lineage (proposal 0011, v0.9.0)" \
  --body "$(cat <<'EOF'
Implements proposal 0011 — a generic derived-from edge between any two lineage
nodes (cohort artifact / reference / sample output), making staleness transitive
across a multi-hop DAG via a third `derived_stale` flag orthogonal to 0009 `stale`
and 0010 `ref_stale`. The walk traverses the 0010 reference_usage edge so a
PoN-as-reference cascades to its consumers with no version bump.

See docs/proposals/0011-artifact-to-artifact-lineage.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Report CI status**

Run: `gh pr checks --watch` (or report the run URL). Confirm the 3.10–3.13 matrix passes before declaring done.

---

## Self-Review notes (for the executor)

- **Spec coverage:** every §6/§9 item maps to a task — schema+node (1), edges+cycle (2), walk (3), CLI (4-5), init/TOML (6), append convenience (7), views (8), status (9), export (10), validate (11), dashboard (12), MCP (13), Nextflow (14), docs/version (15), verify (16).
- **The risk task is Task 8** (recursive DuckDB view). If the `WITH RECURSIVE` view proves brittle across the attached-sqlite catalog, the fallback is to drop `derived_stale` from `_cohort_artifacts`/`_artifact_derivation` and surface derived-staleness only through the `derivation` CLI + MCP tool (which use the authoritative Python walk in Task 3). Do NOT ship a single-hop approximation labelled `derived_stale` — that would be wrong.
- **Match-the-codebase placeholders:** Tasks 10/12/13 flag specific arg/helper names (`--out` vs `--output`, the HTML-escape helper, the MCP project-resolution preamble and content-wrapper) that must be copied from the 0010 equivalents rather than guessed. Grep the named reference file before writing.
- **Backward compatibility:** every new view/section/export/validate block must no-op on pre-0011 (and pre-0010/0009) projects — Tasks 8-13 each wrap their logic in a `*_schema_exists` guard or a try/except, mirroring the 0009/0010 installers.
