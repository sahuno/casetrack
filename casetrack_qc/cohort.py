"""`casetrack cohort` — readiness summary and paired-design view.

Proposal 0002 §8.2 and §8.3. Two shapes:

1. **Base mode** — one table summarizing patient / specimen / assay counts and
   completion-by-analysis for the usable subset.
2. **Pair-by mode** — per-patient partition completeness across any
   ``specimens`` column (tumor/normal via ``tissue_site``, longitudinal via
   ``timepoint``, etc.). Handles N partitions (not just 2).

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import casetrack
from casetrack_qc.reader import active_assay_ids, active_patient_ids


LEVEL_KEYS = {"patient": "patient_id", "specimen": "specimen_id", "assay": "assay_id"}


# ── base mode (§8.2) ────────────────────────────────────────────────────────────


def _patient_consent_counts(conn) -> dict:
    """Return consent-status buckets for the cohort."""
    out = defaultdict(int)
    for (status,) in conn.execute(
        "SELECT COALESCE(consent_status, 'consented') FROM patients"
    ).fetchall():
        out[status] += 1
    return dict(out)


def _specimen_qc_counts(conn) -> dict:
    out = defaultdict(int)
    for (status,) in conn.execute(
        "SELECT qc_status FROM specimens"
    ).fetchall():
        out[status or "pass"] += 1
    return dict(out)


def _assay_qc_counts(conn) -> dict:
    out = defaultdict(int)
    for (status,) in conn.execute(
        "SELECT qc_status FROM assays"
    ).fetchall():
        out[status or "pass"] += 1
    return dict(out)


def _completion_by_analysis(conn, active_ids: set[str]) -> list[dict]:
    from casetrack import _get_table_columns, DONE_COLUMN_SUFFIX
    done_cols = [
        c for c in _get_table_columns(conn, "assays")
        if c.endswith(DONE_COLUMN_SUFFIX)
    ]
    rows: list[dict] = []
    if not active_ids or not done_cols:
        return rows
    placeholders = ", ".join("?" * len(active_ids))
    for dc in done_cols:
        (done,) = conn.execute(
            f'SELECT COUNT(*) FROM assays WHERE "{dc}" IS NOT NULL '
            f"AND assay_id IN ({placeholders})",
            list(active_ids),
        ).fetchone()
        analysis = dc[: -len(DONE_COLUMN_SUFFIX)]
        rows.append({
            "analysis": analysis,
            "done": done,
            "pending": len(active_ids) - done,
            "total_usable": len(active_ids),
        })
    rows.sort(key=lambda r: r["analysis"])
    return rows


def cohort_summary(conn) -> dict:
    """Gather all counts needed for the §8.2 display. Dict-of-dicts so the
    different --fmt outputs share one shape."""
    (n_pat,) = conn.execute("SELECT COUNT(*) FROM patients").fetchone()
    (n_spec,) = conn.execute("SELECT COUNT(*) FROM specimens").fetchone()
    (n_assay,) = conn.execute("SELECT COUNT(*) FROM assays").fetchone()
    active = active_assay_ids(conn)
    consent_buckets = _patient_consent_counts(conn)
    spec_buckets = _specimen_qc_counts(conn)
    assay_buckets = _assay_qc_counts(conn)
    return {
        "patients": {
            "total": n_pat,
            "by_consent": consent_buckets,
        },
        "specimens": {
            "total": n_spec,
            "by_qc_status": spec_buckets,
        },
        "assays": {
            "total": n_assay,
            "by_qc_status": assay_buckets,
            "usable": len(active),
            "excluded": n_assay - len(active),
        },
        "completion": _completion_by_analysis(conn, active),
    }


# ── pair-by mode (§8.3) ────────────────────────────────────────────────────────


def _specimen_columns(conn) -> set[str]:
    return {r[1] for r in conn.execute('PRAGMA table_info("specimens")').fetchall()}


def build_partition_table(
    conn,
    pair_by: str,
    *,
    assay_type: str | None = None,
    partition_order: list[str] | None = None,
    require: int | None = None,
) -> dict:
    """Return the pair-by readiness table: per-patient rows + summary counts.

    ``pair_by`` must be a column on ``specimens``. Each specimen contributes
    one partition value; each patient's specimens are grouped, and — if
    ``assay_type`` is given — only assays matching that type are considered
    for readiness status.

    Returned shape::

        {
          "pair_by": "tissue_site",
          "partitions": ["tumor", "normal"],
          "assay_type": "ONT-RNA-Seq",
          "rows": [
              {"patient_id": "HGSOC002", "values": {"tumor": "pass", "normal": "FAIL"}, "status": "broken"},
              ...
          ],
          "summary": {"complete": 1, "broken": 1, "incomplete": 0, "singleton": 1},
          "require_satisfied": 2  # only present when --require was passed
        }
    """
    spec_cols = _specimen_columns(conn)
    if pair_by not in spec_cols:
        raise ValueError(
            f"--pair-by column {pair_by!r} not found on specimens "
            f"(available: {sorted(spec_cols)})"
        )

    # Gather per-patient specimen rows with their partition value + qc_status.
    # We pull all specimens (including ones with qc_status != pass) so the
    # "broken" category picks up failed halves.
    spec_sql = f'SELECT patient_id, specimen_id, "{pair_by}", qc_status FROM specimens'
    spec_rows = conn.execute(spec_sql).fetchall()

    # For each specimen, compute an effective "pass/fail" value that accounts
    # for both specimen qc_status and (if assay_type given) any matching
    # assay's qc_status under the cascade.
    per_patient_partitions: dict[str, dict[str, str]] = defaultdict(dict)

    if assay_type is not None:
        # One assay per (specimen, assay_type) is assumed by the proposal §8.3
        # example. When there are multiple, we take the worst qc_status.
        assay_rows = conn.execute(
            "SELECT specimen_id, assay_id, qc_status FROM assays "
            "WHERE assay_type = ?",
            (assay_type,),
        ).fetchall()
        # Also need the patient consent cascade to mark consent_revoked.
        from casetrack_qc.reader import active_patient_ids as _active_pids
        consent_ok_pids = _active_pids(conn)
        worst_by_spec: dict[str, str] = {}
        for spec_id, assay_id, qc in assay_rows:
            qc = qc or "pass"
            cur = worst_by_spec.get(spec_id)
            if cur is None or _severity(qc) > _severity(cur):
                worst_by_spec[spec_id] = qc

    for pid, sid, pval, spec_qc in spec_rows:
        if pval is None or pval == "":
            continue
        # Start with specimen's own qc_status.
        effective = spec_qc or "pass"
        if assay_type is not None:
            # No matching assay at all → "(none)" sentinel so it shows up as
            # incomplete rather than silently counting the specimen as present.
            if sid not in worst_by_spec:
                effective = "(none)"
            else:
                assay_qc = worst_by_spec[sid]
                if _severity(assay_qc) > _severity(effective):
                    effective = assay_qc
            # Cascade consent from patient.
            if pid not in consent_ok_pids and effective != "(none)":
                effective = "consent_revoked"
        else:
            if effective == "pass":
                pass
        # For display: uppercase FAIL/CENSORED so they stand out in tables.
        label = effective
        if label in ("fail", "censored", "consent_revoked"):
            label = label.upper()
        per_patient_partitions[pid][pval] = label

    # Determine the canonical partition order.
    all_parts: set[str] = set()
    for parts in per_patient_partitions.values():
        all_parts.update(parts.keys())
    if partition_order:
        ordered = [p for p in partition_order if p in all_parts] + sorted(
            all_parts - set(partition_order)
        )
    else:
        ordered = sorted(all_parts)

    rows: list[dict] = []
    complete = broken = incomplete = singleton = 0
    require_count = 0
    for pid in sorted(per_patient_partitions.keys()):
        parts = per_patient_partitions[pid]
        present = [p for p in ordered if p in parts]
        missing = [p for p in ordered if p not in parts]
        label_by_part = {
            p: parts.get(p, "(none)") for p in ordered
        }
        if not present:
            continue
        if len(present) == 1 and len(ordered) > 1:
            status = "singleton"
            singleton += 1
        elif missing:
            status = "incomplete"
            incomplete += 1
        else:
            # All partitions present — check for failures.
            bad = [p for p in present if parts[p] in ("FAIL", "CENSORED", "CONSENT_REVOKED", "(none)")]
            if bad:
                status = "broken"
                broken += 1
            else:
                status = "complete"
                complete += 1

        passing = sum(1 for p in present if parts[p] not in ("FAIL", "CENSORED", "CONSENT_REVOKED", "(none)"))
        if require is not None and passing >= require:
            require_count += 1

        rows.append({
            "patient_id": pid,
            "values": label_by_part,
            "status": status,
            "present": present,
            "missing": missing,
            "passing": passing,
        })

    result = {
        "pair_by": pair_by,
        "partitions": ordered,
        "assay_type": assay_type,
        "rows": rows,
        "summary": {
            "complete": complete,
            "broken": broken,
            "incomplete": incomplete,
            "singleton": singleton,
        },
    }
    if require is not None:
        result["require"] = require
        result["require_satisfied"] = require_count
    return result


_QC_SEVERITY = {
    "pass": 0, "warn": 1, "censored": 2, "fail": 3,
    "consent_revoked": 4, "(none)": 2,
}


def _severity(status: str) -> int:
    return _QC_SEVERITY.get(status.lower(), 0)


# ── CLI entry point ────────────────────────────────────────────────────────────


def cmd_cohort(args) -> None:
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        from casetrack_qc.schema import qc_schema_exists
        if not qc_schema_exists(conn):
            print(
                "Error: project has no QC schema. Run `casetrack migrate-qc "
                f"--project-dir {project_dir}`.",
                file=sys.stderr,
            )
            sys.exit(1)

        fmt = args.fmt or "table"

        if args.pair_by:
            partition_order = None
            if args.partition_order:
                partition_order = [
                    s.strip() for s in args.partition_order.split(",") if s.strip()
                ]
            try:
                table = build_partition_table(
                    conn,
                    args.pair_by,
                    assay_type=args.assay_type,
                    partition_order=partition_order,
                    require=args.require,
                )
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

            _filter_by_status(table, args)

            if fmt == "json":
                print(json.dumps(table, indent=2))
            elif fmt == "tsv":
                _emit_pair_tsv(table)
            else:
                _emit_pair_table(table, project_dir)
        else:
            summary = cohort_summary(conn)
            if fmt == "json":
                print(json.dumps(summary, indent=2))
            elif fmt == "tsv":
                _emit_summary_tsv(summary)
            elif fmt == "md":
                _emit_summary_md(summary, project_dir)
            else:
                _emit_summary_table(summary, project_dir)
    finally:
        conn.close()


def _filter_by_status(table: dict, args) -> None:
    if args.complete_only:
        table["rows"] = [r for r in table["rows"] if r["status"] == "complete"]
    elif args.broken_only:
        table["rows"] = [r for r in table["rows"] if r["status"] == "broken"]
    elif args.incomplete_only:
        table["rows"] = [r for r in table["rows"] if r["status"] == "incomplete"]
    elif args.singleton_only:
        table["rows"] = [r for r in table["rows"] if r["status"] == "singleton"]


# ── formatters ────────────────────────────────────────────────────────────────


def _emit_summary_table(s: dict, project_dir: Path) -> None:
    print(f"Cohort: {project_dir.name}")
    print()
    print(f"  Patients:  {s['patients']['total']} total")
    for status, count in sorted(s['patients']['by_consent'].items()):
        print(f"    {count:>4} {status}")
    print()
    print(f"  Specimens: {s['specimens']['total']} total")
    for status, count in sorted(s['specimens']['by_qc_status'].items()):
        print(f"    {count:>4} {status}")
    print()
    print(
        f"  Assays:   {s['assays']['total']} total / "
        f"{s['assays']['usable']} usable / {s['assays']['excluded']} excluded"
    )
    for status, count in sorted(s['assays']['by_qc_status'].items()):
        print(f"    {count:>4} {status}")
    if s["completion"]:
        print()
        print("  Completion by analysis (usable assays only):")
        print(f"    {'ANALYSIS':<28}{'COMPLETE':>10}{'PENDING':>10}{'TOTAL-USABLE':>14}")
        for row in s["completion"]:
            print(
                f"    {row['analysis']:<28}{row['done']:>10}"
                f"{row['pending']:>10}{row['total_usable']:>14}"
            )


def _emit_summary_tsv(s: dict) -> None:
    print("\t".join(["kind", "key", "count"]))
    print(f"patients_total\t-\t{s['patients']['total']}")
    for k, v in sorted(s['patients']['by_consent'].items()):
        print(f"patients_by_consent\t{k}\t{v}")
    print(f"specimens_total\t-\t{s['specimens']['total']}")
    for k, v in sorted(s['specimens']['by_qc_status'].items()):
        print(f"specimens_by_qc\t{k}\t{v}")
    print(f"assays_total\t-\t{s['assays']['total']}")
    print(f"assays_usable\t-\t{s['assays']['usable']}")
    print(f"assays_excluded\t-\t{s['assays']['excluded']}")
    for k, v in sorted(s['assays']['by_qc_status'].items()):
        print(f"assays_by_qc\t{k}\t{v}")
    for row in s["completion"]:
        print(
            f"completion\t{row['analysis']}\t"
            f"{row['done']}/{row['total_usable']}"
        )


def _emit_summary_md(s: dict, project_dir: Path) -> None:
    print(f"# Cohort: {project_dir.name}")
    print()
    print(f"- **Patients**: {s['patients']['total']}")
    for k, v in sorted(s['patients']['by_consent'].items()):
        print(f"  - {v} {k}")
    print(f"- **Specimens**: {s['specimens']['total']}")
    for k, v in sorted(s['specimens']['by_qc_status'].items()):
        print(f"  - {v} {k}")
    print(
        f"- **Assays**: {s['assays']['total']} total / "
        f"{s['assays']['usable']} usable / {s['assays']['excluded']} excluded"
    )
    print()
    if s["completion"]:
        print("| Analysis | Complete | Pending | Total-usable |")
        print("|---|---:|---:|---:|")
        for row in s["completion"]:
            print(
                f"| {row['analysis']} | {row['done']} | "
                f"{row['pending']} | {row['total_usable']} |"
            )


def _emit_pair_table(table: dict, project_dir: Path) -> None:
    parts = table["partitions"]
    print(
        f"Assay type: {table.get('assay_type') or '(any)'}   "
        f"(partition: {table['pair_by']} = {{{', '.join(parts)}}})"
    )
    print()
    # Header
    header = f"  {'PATIENT':<12}"
    for p in parts:
        header += f"{p[:12]:<14}"
    header += "GROUP STATUS"
    print(header)
    for row in table["rows"]:
        line = f"  {row['patient_id']:<12}"
        for p in parts:
            val = row["values"].get(p, "(none)")
            line += f"{val[:12]:<14}"
        line += row["status"]
        print(line)
    print()
    s = table["summary"]
    print(f"Summary:")
    print(f"  Complete groups:  {s['complete']}")
    print(f"  Broken groups:    {s['broken']}")
    print(f"  Incomplete:       {s['incomplete']}")
    print(f"  Singletons:       {s['singleton']}")
    if "require" in table:
        print(
            f"  With --require {table['require']} of {len(parts)}: "
            f"{table['require_satisfied']} patient(s) satisfy"
        )


def _emit_pair_tsv(table: dict) -> None:
    parts = table["partitions"]
    header = ["patient_id"] + list(parts) + ["status"]
    print("\t".join(header))
    for row in table["rows"]:
        out = [row["patient_id"]]
        for p in parts:
            out.append(row["values"].get(p, "(none)"))
        out.append(row["status"])
        print("\t".join(out))


__all__ = [
    "build_partition_table",
    "cohort_summary",
    "cmd_cohort",
]
