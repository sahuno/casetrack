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

    Used by ``derived-from`` and by the ``--derived-from`` convenience on
    ``append`` / ``append-cohort``. Cycle-checked per edge.

    Parameters
    ----------
    conn:
        Open SQLite connection. Caller is responsible for the transaction boundary.
    down:
        Canonical node-ref of the derived output.
    ups:
        Canonical node-refs of the source artifacts.
    transaction_id:
        Provenance transaction ID (caller's ``_new_transaction_id()``).

    Returns
    -------
    int
        Number of edges recorded (duplicates are ignored silently).

    Example
    -------
    >>> n = record_derivation_edges(conn, down="cohort:annot@v1",
    ...                             ups=["cohort:joint@v1"], transaction_id="t1")
    """
    ad.ensure_derivation_schema(conn)
    n = 0
    for up in ups:
        ad.record_edge(conn, down=down, up=up, transaction_id=transaction_id)
        n += 1
    return n


def cmd_derived_from(args) -> None:
    """Record one or more derived-from edges for the given downstream node."""
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
                "down_node": args.downstream,
                "up_node": up,
                "transaction_id": txn_id,
            })
        print(f"Recorded {len(ups)} derived-from edge(s) for {args.downstream}.")
    finally:
        conn.close()


def cmd_migrate_derivation(args) -> None:
    """Additive migration: create the artifact_derivation table on a pre-0011 project."""
    project_dir, _ = casetrack._resolve_project(
        args.project_dir, bypass_legacy_gate=True
    )
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
        casetrack.log_project_provenance(
            project_dir,
            {
                "action": "migrate_derivation",
                "executed_sql": executed,
                "transaction_id": txn_id,
            },
        )
        print(f"Created derivation schema ({len(executed)} statements).")
    finally:
        conn.close()


def cmd_derivation(args) -> None:
    """List derivation edges and per-node derived-staleness."""
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        if not ad.derivation_schema_exists(conn):
            print(
                f"Error: project has no derivation schema. Run "
                f"`casetrack migrate-derivation --project-dir {project_dir}`.",
                file=sys.stderr,
            )
            sys.exit(1)
        fmt = getattr(args, "fmt", None) or "table"
        node = getattr(args, "node", None)
        stale_only = getattr(args, "stale_only", False)
        if node:
            rows = [
                {"node": node, "direction": "upstream", "other": u}
                for u in ad.upstream_nodes(conn, node)
            ]
            rows += [
                {"node": node, "direction": "downstream", "other": e["down_node"]}
                for e in ad.list_edges(conn)
                if e["up_node"] == node
            ]
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
                print(
                    "No derivation edges."
                    if not stale_only
                    else "No derived-stale outputs."
                )
                return
            for r in rows:
                line = f"[{r['state']}] {r['node']}"
                if r["reasons"]:
                    line += f"  ({'; '.join(r['reasons'])})"
                print(line)
    finally:
        conn.close()


__all__ = [
    "record_derivation_edges",
    "cmd_derived_from",
    "cmd_migrate_derivation",
    "cmd_derivation",
]
