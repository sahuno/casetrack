"""Argparse wiring for the QC subsystem — called by ``casetrack.main()``.

Kept separate from the command functions so ``casetrack.py`` can integrate
without pulling transitive imports in at every call site.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse

from casetrack_qc.censor import cmd_censor, cmd_qc_history, cmd_uncensor
from casetrack_qc.migrate import cmd_migrate_qc


def build_qc_subparsers(subparsers) -> None:
    """Add ``censor``, ``uncensor``, ``qc-history``, ``migrate-qc`` to the
    parent ``subparsers`` returned by ``argparse.ArgumentParser.add_subparsers``.
    """
    # ── censor ──
    p_censor = subparsers.add_parser(
        "censor",
        help="[v0.4] Record a QC failure / consent revocation (project mode)",
    )
    p_censor.add_argument("--project-dir", required=True, help="Casetrack project directory")
    p_censor.add_argument("--level", choices=["patient", "specimen", "assay"],
                          help="Entity level (required unless --from)")
    p_censor.add_argument("--id", help="Entity ID at --level (required unless --from)")
    p_censor.add_argument("--kind", help="QC kind (e.g. qc_fail, library_prep_failed)")
    p_censor.add_argument("--reason", help="Human-readable reason; stored verbatim")
    p_censor.add_argument("--source", choices=["manual", "slurm", "import"],
                          help="Event source (default: manual)")
    p_censor.add_argument(
        "--withdrawal-date",
        help="Only with --kind consent_revoked at --level patient; "
             "ISO date (YYYY-MM-DD). Defaults to today if omitted.",
    )
    p_censor.add_argument(
        "--from", dest="from_file",
        help="Bulk import from a TSV (columns: level, entity_id, kind, reason)",
    )

    # ── uncensor ──
    p_uncensor = subparsers.add_parser(
        "uncensor",
        help="[v0.4] Resolve an active qc_events row",
    )
    p_uncensor.add_argument("--project-dir", required=True)
    p_uncensor.add_argument("--event-id", type=int, help="qc_events.id to resolve")
    p_uncensor.add_argument("--level", choices=["patient", "specimen", "assay"],
                            help="(sugar) Entity level — use with --id when there's "
                                 "exactly one active event on the entity")
    p_uncensor.add_argument("--id", help="(sugar) Entity ID at --level")
    p_uncensor.add_argument("--reason", required=True, help="Why it's being resolved")
    p_uncensor.add_argument(
        "--ethics-override", action="store_true",
        help="Required for resolving a consent_revoked event",
    )
    p_uncensor.add_argument(
        "--yes", action="store_true",
        help="Confirm --ethics-override non-interactively",
    )

    # ── qc-history ──
    p_hist = subparsers.add_parser(
        "qc-history",
        help="[v0.4] Show all QC events for an entity (or all active if no --id)",
    )
    p_hist.add_argument("--project-dir", required=True)
    p_hist.add_argument("--level", choices=["patient", "specimen", "assay"])
    p_hist.add_argument("--id")
    p_hist.add_argument(
        "--include-cascaded", action="store_true",
        help="(reserved for a future phase; currently accepted but no-op)",
    )
    p_hist.add_argument("--fmt", choices=["table", "tsv", "json"], default="table")

    # ── migrate-qc ──
    p_mig = subparsers.add_parser(
        "migrate-qc",
        help="[v0.4] One-shot: add QC schema + port legacy qc_pass column",
    )
    p_mig.add_argument("--project-dir", required=True)
    p_mig.add_argument(
        "--qc-pass-column", default="qc_pass",
        help="Legacy column name on assays to migrate (default: qc_pass)",
    )
    p_mig.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan, make no changes",
    )


def qc_command_dispatch() -> dict:
    """Command-name → function map that ``casetrack.main()`` merges into its own."""
    return {
        "censor": cmd_censor,
        "uncensor": cmd_uncensor,
        "qc-history": cmd_qc_history,
        "migrate-qc": cmd_migrate_qc,
    }
