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
    """Create the artifact_derivation table + indexes. Idempotent. Returns executed SQL."""
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
            if len(parts) != 3 or not all(p.strip() for p in parts):
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


def downstream_nodes(conn: sqlite3.Connection, node: str) -> list[str]:
    """Direct artifact_derivation downstreams of *node* (one hop, 0011 edges only).

    The mirror of :func:`upstream_nodes`; uses the ``idx_deriv_up`` index rather
    than a full-table scan + Python filter.
    """
    return [d for (d,) in conn.execute(
        "SELECT down_node FROM artifact_derivation WHERE up_node = ?", (node,)
    ).fetchall()]


def _reaches(conn: sqlite3.Connection, start: str, target: str) -> bool:
    """True if *target* is reachable walking upstream from *start*.

    Walks the SAME combined edge set the staleness walk uses — both 0011
    ``artifact_derivation`` edges and the 0010 ``reference_usage`` edges
    (``_all_upstreams``). This is what lets ``record_edge``'s cycle guard reject
    a cross-table cycle: e.g. ``cohort:C uses ref R`` (a reference_usage row)
    plus an attempted ``reference:R -> cohort:C`` artifact_derivation edge would
    close a real C→R→C loop in the graph the staleness walk traverses
    (proposal 0011 §6.4). ``_all_upstreams`` is defined later in this module;
    the forward reference resolves at call time.
    """
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(_all_upstreams(conn, cur))
    return False


def record_edge(conn: sqlite3.Connection, *, down: str, up: str,
                transaction_id: str, recorded_at: str | None = None) -> None:
    """Record one derived-from edge (down derives from up). Idempotent.

    Validates both node-refs and refuses an edge that would create a cycle in
    the artifact_derivation graph (0011 §6.4).

    The cycle check (read) and the insert (write) are not atomic. Callers MUST
    wrap this in ``casetrack.begin_immediate(conn)`` so two concurrent callers
    cannot both pass the check and together close a cycle. Module functions do
    not own transactions — that is the CLI layer's job (proposal 0001).
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


# ── transitive staleness walk ─────────────────────────────────────────────────

def _resolve_artifact_id(conn: sqlite3.Connection, analysis: str, run_tag: str) -> int | None:
    """Look up cohort_artifacts.artifact_id by (analysis, run_tag). Returns None if absent."""
    row = conn.execute(
        "SELECT artifact_id FROM cohort_artifacts WHERE analysis=? AND run_tag=?",
        (analysis, run_tag),
    ).fetchone()
    return row[0] if row else None


def _reference_usage_upstreams(conn: sqlite3.Connection, node: str) -> list[str]:
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


def _all_upstreams(conn: sqlite3.Connection, node: str) -> list[str]:
    """Union of 0011 artifact_derivation edges and the 0010 reference_usage edges.

    Deduped (order-preserving): the same ``reference:<ref_key>`` can appear as
    both an artifact_derivation edge and a reference_usage edge, and walking it
    twice would otherwise produce duplicate reason strings downstream.
    """
    return list(dict.fromkeys(
        upstream_nodes(conn, node) + _reference_usage_upstreams(conn, node)))


def _direct_stale(conn: sqlite3.Connection, node: str,
                  cohort_stale: dict[int, list[str]] | None = None) -> list[str]:
    """A node's OWN direct staleness reasons (0009 input + 0010 ref), no recursion.

    reference nodes have no intrinsic direct staleness (only derived).
    analysis nodes have only 0010 ref-staleness (no 0009 inputs).

    *cohort_stale* is the pre-computed ``cohort_artifacts.artifact_staleness``
    map. Passing it in lets callers compute the whole-cohort scan ONCE and reuse
    it across every visited node, instead of rescanning per cohort node (which
    made ``all_derived_stale`` roughly O(k²)). If omitted, it is computed here.
    """
    n = LineageNode.parse(node)
    reasons: list[str] = []
    if n.scope == "cohort":
        from casetrack_qc import cohort_artifacts as _ca
        aid = _resolve_artifact_id(conn, n.analysis, n.run_tag)
        if aid is not None:
            if cohort_stale is None:
                cohort_stale = _ca.artifact_staleness(conn)
            censored = cohort_stale.get(aid, [])
            if censored:
                reasons.append(f"inputs censored: {', '.join(sorted(censored))}")
            ref = _ref_state(conn, scope="cohort", artifact_id=aid)
            reasons += [f"ref {r}" for r in ref]
    elif n.scope == "analysis":
        ref = _ref_state(conn, scope="analysis", entity_level=n.entity_level,
                         entity_id=n.entity_id, analysis=n.analysis)
        reasons += [f"ref {r}" for r in ref]
    # reference scope: no intrinsic direct staleness
    return reasons


def _ref_state(conn: sqlite3.Connection, **kw) -> list[str]:
    """Return reference staleness reason strings for one output, or [] if fresh/untracked."""
    if not _table_exists(conn, "reference_usage"):
        return []
    from casetrack_qc import reference_artifacts as _ra
    s = _ra.output_staleness(conn, **kw)
    return s["reasons"] if s["state"] == "STALE" else []


def _upstream_reachable(conn: sqlite3.Connection, node: str) -> list[str]:
    """All nodes reachable UPSTREAM from *node* (>=1 hop) over ``_all_upstreams``.

    Plain visited-set traversal — inherently cycle-safe and order-independent.
    A visited set never re-enters a node, so back-edges in a cyclic graph simply
    terminate that branch; there is no memo to poison and no path to taint
    (this is what dissolves the memo-poisoning failure mode of the old
    recursion). The starting *node* itself is excluded from the result so we
    roll up only its upstreams, never its own direct causes (proposal 0011 §6.2).
    """
    seen: set[str] = set()
    out: list[str] = []
    stack = list(_all_upstreams(conn, node))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        out.append(cur)
        stack.extend(_all_upstreams(conn, cur))
    return out


def _staleness_for(conn: sqlite3.Connection, node: str,
                   cohort_stale: dict[int, list[str]] | None = None) -> dict:
    """Core of ``derived_staleness`` with a hoistable cohort-staleness map."""
    if cohort_stale is None:
        from casetrack_qc import cohort_artifacts as _ca
        cohort_stale = (_ca.artifact_staleness(conn)
                        if _table_exists(conn, "cohort_artifacts") else {})
    reasons: list[str] = []
    for up in _upstream_reachable(conn, node):
        direct = _direct_stale(conn, up, cohort_stale=cohort_stale)
        if direct:
            reasons.append(
                f"upstream {up} is STALE ({'; '.join(sorted(direct))})")
    reasons = sorted(set(reasons))
    return {"state": "STALE" if reasons else "fresh", "reasons": reasons}


def derived_staleness(conn: sqlite3.Connection, node: str) -> dict:
    """{'state': 'fresh'|'STALE', 'reasons': [...]} for *node*.

    derived_stale = any node reachable UPSTREAM from *node* is itself directly
    stale (0009 input-censored / 0010 ref-stale). Excludes *node*'s OWN direct
    causes — those are surfaced as 0009 ``stale`` / 0010 ``ref_stale``. Boolean
    two-state rollup: a node with no upstream-derivation edges is cleanly
    ``fresh`` (NOT ``untracked`` — proposal 0011 §6.2).

    Cycle-safe: computed by visited-set upstream reachability, so a cyclic
    derivation graph terminates and reports staleness losslessly (no false
    fresh, no dropped reasons). The expensive whole-cohort
    ``artifact_staleness`` scan runs once per call.
    """
    return _staleness_for(conn, node)


def all_derived_stale(conn: sqlite3.Connection) -> list[dict]:
    """Every down_node with >=1 derivation edge, annotated with derived staleness.

    The whole-cohort ``artifact_staleness`` scan is computed ONCE and reused
    across all nodes, avoiding the per-node rescan that made this O(k²).
    """
    from casetrack_qc import cohort_artifacts as _ca
    cohort_stale = (_ca.artifact_staleness(conn)
                    if _table_exists(conn, "cohort_artifacts") else {})
    out: list[dict] = []
    for (node,) in conn.execute(
        "SELECT DISTINCT down_node FROM artifact_derivation"
    ).fetchall():
        out.append({"node": node,
                    **_staleness_for(conn, node, cohort_stale=cohort_stale)})
    return out


__all__ = [
    "TIMESTAMP_FMT", "NODE_SCOPES", "DerivationError",
    "artifact_derivation_ddl", "artifact_derivation_indexes",
    "derivation_schema_exists", "ensure_derivation_schema",
    "LineageNode",
    "list_edges", "upstream_nodes", "downstream_nodes", "record_edge",
    "derived_staleness", "all_derived_stale",
]
