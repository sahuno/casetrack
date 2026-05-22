"""CLI commands for cohort-level artifacts (proposal 0009 §6.3).

- ``migrate-cohort``    — additive: create the two tables on a pre-0009 project.
- ``append-cohort``     — register one cohort artifact + its assay lineage.
- ``cohort-artifacts``  — list artifacts with read-time staleness.

Mirrors the ``casetrack_qc.migrate`` / ``casetrack_qc.cohort`` command style:
resolve project → open db → one ``begin_immediate`` write → provenance entry.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import casetrack
from casetrack_qc import artifact_derivation as ad
from casetrack_qc import cohort_artifacts as ca


def _read_inputs(args) -> list[str]:
    """Collect contributing assay_ids from ``--inputs`` or ``--inputs-from``.

    ``--inputs`` is a comma-separated list. ``--inputs-from`` is a file with one
    assay_id per line; a leading ``assay_id`` header line is skipped and only the
    first tab-separated column is read (so a wider TSV works).
    """
    inputs = getattr(args, "inputs", None)
    if inputs:
        return [s.strip() for s in inputs.split(",") if s.strip()]
    inputs_from = getattr(args, "inputs_from", None)
    if inputs_from:
        ids: list[str] = []
        for line in Path(inputs_from).read_text().splitlines():
            s = line.strip()
            if not s or s.lower() == "assay_id":
                continue
            ids.append(s.split("\t")[0].strip())
        return ids
    return []


# ── append-cohort ─────────────────────────────────────────────────────────────


def cmd_append_cohort(args) -> None:
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME

    inputs = _read_inputs(args)
    if not inputs:
        print(
            "Error: no contributing assays — pass --inputs a,b,c or "
            "--inputs-from FILE.",
            file=sys.stderr,
        )
        sys.exit(2)

    stats_json = None
    if getattr(args, "stats", None):
        stats_json = Path(args.stats).read_text()

    checksum = getattr(args, "checksum", None)
    created_by = getattr(args, "created_by", None) or (
        f"manual:{os.environ.get('USER', 'unknown')}"
    )
    txn_id = casetrack._new_transaction_id()

    conn = casetrack.open_project_db(db_path)
    art_id: int | None = None
    try:
        try:
            with casetrack.begin_immediate(conn):
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
                    created_by=created_by,
                )
                ca.add_artifact_inputs(conn, art_id, inputs)
                refs = getattr(args, "uses_references", None)
                if refs:
                    from casetrack_qc import reference_artifacts as _ra
                    _ra.ensure_reference_schema(conn)
                    current = {r.ref_key: r.version
                               for r in _ra.list_references(conn)}
                    for ref_key in [s.strip() for s in refs.split(",") if s.strip()]:
                        if ref_key in current:
                            _ra.record_usage(
                                conn, scope="cohort", artifact_id=art_id,
                                ref_key=ref_key, version_used=current[ref_key],
                                transaction_id=txn_id)
                derived_from = getattr(args, "derived_from", None)
                if derived_from:
                    from casetrack_qc.artifact_derivation_cli import record_derivation_edges
                    ups = [s.strip() for s in derived_from.split(",") if s.strip()]
                    record_derivation_edges(
                        conn, down=f"cohort:{args.analysis}@{args.run_tag}",
                        ups=ups, transaction_id=txn_id)
        except (ca.CohortArtifactError, ad.DerivationError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)

        casetrack.log_project_provenance(
            project_dir,
            {
                "action": "append_cohort",
                "analysis": args.analysis,
                "run_tag": args.run_tag,
                "path": args.path,
                "checksum": checksum,
                "n_inputs": len(inputs),
                "inputs": inputs,
                "artifact_id": art_id,
                "has_stats": stats_json is not None,
                "transaction_id": txn_id,
                "derived_from": [
                    s.strip() for s in (derived_from or "").split(",") if s.strip()
                ],
            },
        )
        print(
            f"Appended cohort artifact id={art_id}: "
            f"{args.analysis}/{args.run_tag} ({len(inputs)} inputs)"
        )
    finally:
        conn.close()


# ── migrate-cohort ────────────────────────────────────────────────────────────


def cmd_migrate_cohort(args) -> None:
    # Upgrade path — operates on pre-0009 projects, so bypass the legacy gate.
    project_dir, _ = casetrack._resolve_project(
        args.project_dir, bypass_legacy_gate=True
    )
    db_path = project_dir / casetrack.PROJECT_DB_NAME

    conn = casetrack.open_project_db(db_path)
    try:
        # NOTE: this guard only checks table presence. A post-0009/pre-0013 project
        # (tables exist but lack region_scope/role) is upgraded via `migrate-region-scope`,
        # not here.
        if ca.cohort_artifacts_schema_exists(conn):
            print("No migration needed — cohort-artifact schema already in place.")
            return
        if getattr(args, "dry_run", False):
            print(
                "[dry-run] Would create cohort_artifacts + "
                "cohort_artifact_inputs tables (+ indexes)."
            )
            return
        txn_id = casetrack._new_transaction_id()
        with casetrack.begin_immediate(conn):
            executed = ca.ensure_cohort_artifacts_schema(conn)
        casetrack.log_project_provenance(
            project_dir,
            {
                "action": "migrate_cohort",
                "executed_sql": executed,
                "transaction_id": txn_id,
            },
        )
        print(f"Created cohort-artifact schema ({len(executed)} statements).")
    finally:
        conn.close()


# ── cohort-artifacts (list + staleness) ──────────────────────────────────────


def _artifact_rows(conn) -> list[dict]:
    stale_map = ca.artifact_staleness(conn)
    rows: list[dict] = []
    for art in ca.list_artifacts(conn):
        censored = stale_map.get(art.artifact_id, [])
        rows.append(
            {
                "artifact_id": art.artifact_id,
                "analysis": art.analysis,
                "run_tag": art.run_tag,
                "path": art.path,
                "n_inputs": art.n_inputs,
                "stale": len(censored) > 0,
                "n_censored_inputs": len(censored),
                "censored_inputs": censored,
            }
        )
    return rows


def cmd_cohort_artifacts(args) -> None:
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME

    conn = casetrack.open_project_db(db_path)
    try:
        if not ca.cohort_artifacts_schema_exists(conn):
            print(
                "Error: project has no cohort-artifact schema. Run "
                f"`casetrack migrate-cohort --project-dir {project_dir}`.",
                file=sys.stderr,
            )
            sys.exit(1)

        rows = _artifact_rows(conn)
        if getattr(args, "stale_only", False):
            rows = [r for r in rows if r["stale"]]

        fmt = getattr(args, "fmt", None) or "table"
        if fmt == "json":
            print(json.dumps(rows, indent=2))
        elif fmt == "tsv":
            cols = ["artifact_id", "analysis", "run_tag", "n_inputs",
                    "stale", "n_censored_inputs", "path"]
            print("#" + "\t".join(cols))
            for r in rows:
                print("\t".join(str(r[c]) for c in cols))
        else:
            if not rows:
                print("No cohort artifacts.")
                return
            for r in rows:
                flag = "STALE" if r["stale"] else "fresh"
                line = (
                    f"[{flag}] {r['analysis']}/{r['run_tag']}  "
                    f"id={r['artifact_id']}  inputs={r['n_inputs']}"
                )
                if r["stale"]:
                    line += (
                        f"  censored={r['n_censored_inputs']} "
                        f"({', '.join(r['censored_inputs'])})"
                    )
                print(line)
    finally:
        conn.close()


__all__ = ["cmd_append_cohort", "cmd_migrate_cohort", "cmd_cohort_artifacts"]
