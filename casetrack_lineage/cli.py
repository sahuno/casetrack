"""Argparse wiring for the lineage subsystem — called by ``casetrack.main()``.

Mirrors the pattern in ``casetrack_qc.cli``.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

from casetrack_lineage.batch_censor import cmd_censor_batch, cmd_uncensor_batch
from casetrack_lineage.lineage import cmd_add_batch, cmd_link_sources
from casetrack_lineage.migrate import cmd_migrate_lineage


def build_lineage_subparsers(subparsers) -> None:
    """Add ``migrate-lineage``, ``add-batch``, and ``link-sources`` to *subparsers*."""

    # ── migrate-lineage ──
    p_ml = subparsers.add_parser(
        "migrate-lineage",
        help="[v0.6] Add assay lineage + batch tables to an existing project",
    )
    p_ml.add_argument("--project-dir", required=True,
                      help="Casetrack project directory")
    p_ml.add_argument(
        "--map-flowcell-to-batch", action="store_true", dest="map_flowcell_to_batch",
        help="Copy flowcell_id → batch_id for assays that have flowcell_id set",
    )

    # ── add-batch ──
    p_ab = subparsers.add_parser(
        "add-batch",
        help="[v0.6] Register a sequencing/library-prep batch",
    )
    p_ab.add_argument("--project-dir", required=True)
    p_ab.add_argument("--batch-id", dest="batch_id",
                      help="Unique batch identifier")
    p_ab.add_argument(
        "--meta",
        help="key=val,key2=val2 — recognised keys: prep_date, reagent_lot, "
             "operator, notes",
    )
    p_ab.add_argument(
        "--from-tsv", dest="from_tsv",
        help="CSV/TSV with columns batch_id[,prep_date,reagent_lot,operator,notes]",
    )

    # ── link-sources ──
    p_ls = subparsers.add_parser(
        "link-sources",
        help="[v0.6] Record which run assays fed a specimen or merged assay",
    )
    p_ls.add_argument("--project-dir", required=True)
    p_ls.add_argument(
        "--sources",
        help="Comma-separated source assay_ids",
    )
    p_ls.add_argument(
        "--specimen",
        help="consumer_specimen_id (Mode B: run assays → specimen)",
    )
    p_ls.add_argument(
        "--merged-id", dest="merged_id",
        help="merged_assay_id (Mode A: run assays → merged assay)",
    )
    p_ls.add_argument(
        "--from-tsv", dest="from_tsv",
        help="CSV/TSV with columns source_assay_id + merged_assay_id OR "
             "consumer_specimen_id",
    )


def lineage_command_dispatch() -> dict:
    """Command-name → function map that ``casetrack.main()`` merges into its own."""
    return {
        "migrate-lineage": cmd_migrate_lineage,
        "add-batch": cmd_add_batch,
        "link-sources": cmd_link_sources,
        # Batch-level censor/uncensor are injected into the existing 'censor' /
        # 'uncensor' dispatch inside casetrack.py via the --batch flag routing.
    }


__all__ = [
    "build_lineage_subparsers",
    "lineage_command_dispatch",
    "cmd_censor_batch",
    "cmd_uncensor_batch",
]
