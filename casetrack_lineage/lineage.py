"""`casetrack add-batch` and `casetrack link-sources` commands (proposal 0006 §2).

``add-batch``   — upsert a library-prep / sequencing batch into ``batches``.
``link-sources`` — record which source assays fed a merged assay (Mode A) or
                   a downstream specimen (Mode B).

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import casetrack
from casetrack_lineage.schema import lineage_schema_exists


# ── shared helpers ─────────────────────────────────────────────────────────────


def _error(msg: str, exit_code: int = 1) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(exit_code)


def _require_lineage_schema(conn) -> None:
    if not lineage_schema_exists(conn):
        _error(
            "project has no lineage schema. "
            "Run `casetrack migrate-lineage --project-dir <DIR>`.",
            exit_code=1,
        )


def _parse_meta(meta_str: str) -> dict:
    """Parse ``'key=val,key2=val2'`` into a dict.

    Recognised keys: prep_date, reagent_lot, operator, notes.

    Examples
    --------
    >>> _parse_meta('prep_date=2026-01-01,operator=jdoe')
    {'prep_date': '2026-01-01', 'operator': 'jdoe'}
    """
    allowed = {"prep_date", "reagent_lot", "operator", "notes"}
    result: dict = {}
    for token in meta_str.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            _error(
                f"--meta token {token!r} is not in key=value format",
                exit_code=1,
            )
        k, _, v = token.partition("=")
        k = k.strip()
        if k not in allowed:
            _error(
                f"--meta key {k!r} not recognised; allowed: {sorted(allowed)}",
                exit_code=1,
            )
        result[k] = v.strip()
    return result


# ── add-batch ─────────────────────────────────────────────────────────────────


def _upsert_batch(conn, batch_id: str, fields: dict) -> None:
    """INSERT OR REPLACE a single batch row."""
    cols = ["batch_id"] + list(fields.keys())
    vals = [batch_id] + list(fields.values())
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT OR REPLACE INTO batches ({col_names}) VALUES ({placeholders})",
        vals,
    )


def cmd_add_batch(args) -> None:
    """Upsert one or more batch rows into ``batches``.

    Modes:

    - Single batch: ``--batch-id ID [--meta 'key=val,...']``
    - Bulk import:  ``--from-tsv FILE`` (columns: batch_id, prep_date,
                    reagent_lot, operator, notes)
    """
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        _require_lineage_schema(conn)

        upserted: list[str] = []

        if args.from_tsv:
            src = Path(args.from_tsv)
            if not src.exists():
                _error(f"--from-tsv file not found: {src}", exit_code=1)
            with open(src, newline="") as f:
                # Accept both comma and tab separators.
                sample = f.read(1024)
                f.seek(0)
                dialect = "excel" if "," in sample else "excel-tab"
                reader = csv.DictReader(f, dialect=dialect)
                rows = list(reader)
            if not rows:
                print(f"No rows in {src}", file=sys.stderr)
                return
            if "batch_id" not in (rows[0].keys() or {}):
                _error(
                    "--from-tsv file must have a 'batch_id' column", exit_code=1
                )
            allowed_fields = {"prep_date", "reagent_lot", "operator", "notes"}
            with casetrack.begin_immediate(conn):
                for row in rows:
                    bid = (row.get("batch_id") or "").strip()
                    if not bid:
                        _error(
                            "--from-tsv row has empty batch_id", exit_code=1
                        )
                    fields = {
                        k: v.strip()
                        for k, v in row.items()
                        if k in allowed_fields and v and v.strip()
                    }
                    _upsert_batch(conn, bid, fields)
                    upserted.append(bid)

            entry = {
                "action": "add_batch",
                "source": "tsv",
                "batch_ids": upserted,
                "from_tsv": src.name,
            }
            casetrack.log_project_provenance(project_dir, entry)
            print(f"Registered {len(upserted)} batch(es) from {src.name}.")

        else:
            # Single-batch path.
            if not args.batch_id:
                _error(
                    "either --batch-id or --from-tsv is required", exit_code=1
                )
            fields = _parse_meta(args.meta or "")
            with casetrack.begin_immediate(conn):
                _upsert_batch(conn, args.batch_id, fields)

            entry = {
                "action": "add_batch",
                "source": "manual",
                "batch_id": args.batch_id,
                "fields": fields,
            }
            casetrack.log_project_provenance(project_dir, entry)
            print(f"Registered batch {args.batch_id!r}.")

    finally:
        conn.close()


# ── link-sources ──────────────────────────────────────────────────────────────


def _assay_exists(conn, assay_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM assays WHERE assay_id=?", (assay_id,)
    ).fetchone() is not None


def _specimen_exists(conn, specimen_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM specimens WHERE specimen_id=?", (specimen_id,)
    ).fetchone() is not None


def _insert_link(conn, source_assay_id: str, merged_assay_id=None,
                 consumer_specimen_id=None) -> bool:
    """INSERT OR IGNORE a single edge.  Returns True if a new row was inserted."""
    conn.execute(
        """
        INSERT OR IGNORE INTO assay_sources
            (source_assay_id, merged_assay_id, consumer_specimen_id)
        VALUES (?, ?, ?)
        """,
        (source_assay_id, merged_assay_id, consumer_specimen_id),
    )
    return conn.execute("SELECT changes()").fetchone()[0] == 1


def cmd_link_sources(args) -> None:
    """Record assay-lineage edges in ``assay_sources``.

    Mode A (``--merged-id``): source assays were merged/combined into a
    derived assay.
    Mode B (``--specimen``): source assays are run-level inputs that belong to
    a downstream specimen.

    Bulk path: ``--from-tsv FILE`` with columns
    ``source_assay_id`` + one of ``merged_assay_id`` / ``consumer_specimen_id``.
    """
    project_dir, _ = casetrack._resolve_project(args.project_dir)
    db_path = project_dir / casetrack.PROJECT_DB_NAME
    conn = casetrack.open_project_db(db_path)
    try:
        _require_lineage_schema(conn)

        inserted = 0
        skipped = 0

        if args.from_tsv:
            src = Path(args.from_tsv)
            if not src.exists():
                _error(f"--from-tsv file not found: {src}", exit_code=1)
            with open(src, newline="") as f:
                sample = f.read(1024)
                f.seek(0)
                dialect = "excel" if "," in sample else "excel-tab"
                reader = csv.DictReader(f, dialect=dialect)
                rows = list(reader)
            if not rows:
                print(f"No rows in {src}", file=sys.stderr)
                return
            headers = set(rows[0].keys() or {})
            if "source_assay_id" not in headers:
                _error(
                    "--from-tsv file must have 'source_assay_id' column",
                    exit_code=1,
                )
            if "merged_assay_id" not in headers and "consumer_specimen_id" not in headers:
                _error(
                    "--from-tsv file must have 'merged_assay_id' or "
                    "'consumer_specimen_id' column",
                    exit_code=1,
                )

            with casetrack.begin_immediate(conn):
                for idx, row in enumerate(rows, start=1):
                    src_id = (row.get("source_assay_id") or "").strip()
                    merged = (row.get("merged_assay_id") or "").strip() or None
                    specimen = (row.get("consumer_specimen_id") or "").strip() or None
                    if not src_id:
                        _error(
                            f"row {idx}: empty source_assay_id", exit_code=1
                        )
                    if (merged is not None) == (specimen is not None):
                        _error(
                            f"row {idx}: exactly one of merged_assay_id / "
                            "consumer_specimen_id must be set",
                            exit_code=1,
                        )
                    if not _assay_exists(conn, src_id):
                        _error(
                            f"row {idx}: source assay {src_id!r} not found",
                            exit_code=2,
                        )
                    if merged is not None and not _assay_exists(conn, merged):
                        _error(
                            f"row {idx}: merged assay {merged!r} not found",
                            exit_code=2,
                        )
                    if specimen is not None and not _specimen_exists(conn, specimen):
                        _error(
                            f"row {idx}: specimen {specimen!r} not found",
                            exit_code=2,
                        )
                    new = _insert_link(conn, src_id, merged, specimen)
                    if new:
                        inserted += 1
                    else:
                        skipped += 1

            entry = {
                "action": "link_sources",
                "from_tsv": src.name,
                "inserted": inserted,
                "skipped_duplicates": skipped,
            }
            casetrack.log_project_provenance(project_dir, entry)
            print(
                f"Linked {inserted} source assay(s) from {src.name} "
                f"({skipped} duplicate(s) skipped)."
            )

        else:
            # CLI single-invocation path.
            if not args.sources:
                _error("--sources is required (or use --from-tsv)", exit_code=1)
            merged_id = getattr(args, "merged_id", None)
            specimen_id = getattr(args, "specimen", None)
            if (merged_id is not None) == (specimen_id is not None):
                _error(
                    "exactly one of --merged-id or --specimen is required",
                    exit_code=1,
                )

            source_ids = [s.strip() for s in args.sources.split(",") if s.strip()]
            if not source_ids:
                _error("--sources produced an empty list", exit_code=1)

            # Validate targets first.
            if merged_id is not None and not _assay_exists(conn, merged_id):
                _error(f"merged assay {merged_id!r} not found", exit_code=2)
            if specimen_id is not None and not _specimen_exists(conn, specimen_id):
                _error(f"specimen {specimen_id!r} not found", exit_code=2)

            with casetrack.begin_immediate(conn):
                for src_id in source_ids:
                    if not _assay_exists(conn, src_id):
                        _error(
                            f"source assay {src_id!r} not found", exit_code=2
                        )
                    new = _insert_link(conn, src_id, merged_id, specimen_id)
                    if new:
                        inserted += 1
                    else:
                        skipped += 1

            target = merged_id or specimen_id
            entry = {
                "action": "link_sources",
                "source_assay_ids": source_ids,
                "merged_assay_id": merged_id,
                "consumer_specimen_id": specimen_id,
                "inserted": inserted,
                "skipped_duplicates": skipped,
            }
            casetrack.log_project_provenance(project_dir, entry)
            mode = "merged assay" if merged_id else "specimen"
            print(
                f"Linked {inserted} source assay(s) → {mode} {target!r} "
                f"({skipped} duplicate(s) skipped)."
            )

    finally:
        conn.close()


__all__ = ["cmd_add_batch", "cmd_link_sources"]
