"""CLI commands for reference artifacts (proposal 0010 §6.3).

- ``migrate-references`` — additive: create the two tables on a pre-0010 project.
- ``references``         — list canonical references + ref-staleness.

Mirrors ``casetrack_qc.cohort_artifacts_cli.cmd_migrate_cohort`` /
``cmd_cohort_artifacts`` exactly: resolve project → open db → check schema →
read.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import json
import sys

import casetrack
from casetrack_qc import reference_artifacts as ra


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


def cmd_migrate_references(args) -> None:
    # Upgrade path — operates on pre-0010 projects, so bypass the legacy gate.
    project_dir, _ = casetrack._resolve_project(
        args.project_dir, bypass_legacy_gate=True
    )
    db_path = project_dir / casetrack.PROJECT_DB_NAME

    conn = casetrack.open_project_db(db_path)
    try:
        if ra.reference_schema_exists(conn):
            print("No migration needed — reference schema already in place.")
            return
        if getattr(args, "dry_run", False):
            print(
                "[dry-run] Would create reference_artifacts + "
                "reference_usage tables (+ indexes)."
            )
            return
        txn_id = casetrack._new_transaction_id()
        with casetrack.begin_immediate(conn):
            executed = ra.ensure_reference_schema(conn)
        casetrack.log_project_provenance(
            project_dir,
            {
                "action": "migrate_references",
                "executed_sql": executed,
                "transaction_id": txn_id,
            },
        )
        print(f"Created reference schema ({len(executed)} statements).")
    finally:
        conn.close()


def cmd_references(args) -> None:
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        if not ra.reference_schema_exists(conn):
            print(
                f"Error: project has no reference schema. Run "
                f"`casetrack migrate-references --project-dir {project_dir}`.",
                file=sys.stderr,
            )
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
                    who = (
                        f"{r['entity_level']}:{r['entity_id']}/{r['analysis']}"
                        if r["scope"] == "analysis"
                        else f"cohort_artifact:{r['artifact_id']}"
                    )
                    print(f"[STALE] {who}  ({'; '.join(r['reasons'])})")
            return
        # default: list the canonical reference set
        refs = ra.list_references(conn)
        out = [
            {"ref_key": ref.ref_key, "version": ref.version,
             "kind": ref.kind, "path": ref.path}
            for ref in refs
        ]
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
    finally:
        conn.close()


__all__ = ["cmd_migrate_references", "capture_reference_usage", "cmd_references"]
