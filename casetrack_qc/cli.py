"""Argparse wiring for the QC subsystem — called by ``casetrack.main()``.

Kept separate from the command functions so ``casetrack.py`` can integrate
without pulling transitive imports in at every call site.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse

from casetrack_qc.censor import cmd_censor, cmd_qc_history, cmd_uncensor
from casetrack_qc.cohort import cmd_cohort
from casetrack_qc.cohort_artifacts_cli import (
    cmd_append_cohort,
    cmd_cohort_artifacts,
    cmd_migrate_cohort,
)
from casetrack_qc.migrate import cmd_migrate_qc
from casetrack_qc.reference_artifacts_cli import cmd_migrate_references, cmd_references
from casetrack_qc.artifact_derivation_cli import (
    cmd_derived_from,
    cmd_derivation,
    cmd_migrate_derivation,
)


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
    p_censor.add_argument(
        "--batch",
        help="[v0.6] Batch ID: censor all assays in this batch and cascade "
             "downstream (proposal 0006 §3). When set, --level/--id/--kind are "
             "ignored; --reason is used as the censor reason.",
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
    p_uncensor.add_argument(
        "--batch",
        help="[v0.6] Batch ID: reverse the batch censor for all assays in this "
             "batch (proposal 0006 §3). When set, --event-id/--level/--id are "
             "ignored.",
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

    # ── cohort ──
    p_cohort = subparsers.add_parser(
        "cohort",
        help="[v0.4] Cohort readiness summary + paired-design view",
    )
    p_cohort.add_argument("--project-dir", required=True)
    p_cohort.add_argument("--pair-by",
                          help="Column on specimens to partition by (e.g. tissue_site)")
    p_cohort.add_argument("--assay-type",
                          help="Scope pair-by readiness to one assay_type")
    p_cohort.add_argument("--partition-order",
                          help="Comma-separated canonical order (e.g. 'tumor,normal')")
    p_cohort.add_argument("--require", type=int,
                          help="Report patients satisfying >=N passing partitions")
    p_cohort.add_argument("--complete-only", action="store_true")
    p_cohort.add_argument("--broken-only", action="store_true")
    p_cohort.add_argument("--incomplete-only", action="store_true")
    p_cohort.add_argument("--singleton-only", action="store_true")
    p_cohort.add_argument("--fmt", choices=["table", "tsv", "json", "md"],
                          default="table")

    # ── append-cohort ── (proposal 0009)
    p_appc = subparsers.add_parser(
        "append-cohort",
        help="[v0.7] Register a cohort-level artifact (joint VCF, PoN, matrix) "
             "+ its assay lineage",
    )
    p_appc.add_argument("--project-dir", required=True)
    p_appc.add_argument("--analysis", required=True,
                        help="Analysis name, e.g. joint_genotype")
    p_appc.add_argument("--run-tag", dest="run_tag", required=True,
                        help="Run identifier; (analysis, run_tag) is the unique key")
    p_appc.add_argument("--path", required=True, help="Path to the artifact on disk")
    p_appc.add_argument("--inputs",
                        help="Comma-separated contributing assay_ids")
    p_appc.add_argument("--inputs-from", dest="inputs_from",
                        help="File of assay_ids (one per line; 'assay_id' header "
                             "and extra TSV columns tolerated)")
    p_appc.add_argument("--stats", help="JSON file of cohort-level summary stats")
    p_appc.add_argument("--checksum", help="Artifact checksum (e.g. sha256)")
    p_appc.add_argument("--created-by", dest="created_by",
                        help="Override the recorded actor (default: manual:$USER)")
    p_appc.add_argument("--uses-references", dest="uses_references", default=None,
                        help="[v0.8] Comma-separated reference keys this cohort "
                             "output consumed (e.g. genome,dbsnp)")

    # ── migrate-cohort ──
    p_migc = subparsers.add_parser(
        "migrate-cohort",
        help="[v0.7] Additive: create cohort-artifact tables on a pre-0009 project",
    )
    p_migc.add_argument("--project-dir", required=True)
    p_migc.add_argument("--dry-run", action="store_true",
                        help="Print the plan, make no changes")

    # ── cohort-artifacts ──
    p_calist = subparsers.add_parser(
        "cohort-artifacts",
        help="[v0.7] List cohort-level artifacts with read-time staleness",
    )
    p_calist.add_argument("--project-dir", required=True)
    p_calist.add_argument("--fmt", choices=["table", "tsv", "json"],
                         default="table")
    p_calist.add_argument("--stale-only", dest="stale_only", action="store_true",
                         help="Show only artifacts with one or more censored inputs")

    # ── migrate-references ── (proposal 0010)
    p_migr = subparsers.add_parser(
        "migrate-references",
        help="[v0.8] Additive: create reference-artifact tables on a pre-0010 project",
    )
    p_migr.add_argument("--project-dir", required=True)
    p_migr.add_argument("--dry-run", action="store_true",
                        help="Print the plan, make no changes")

    # ── references ── (proposal 0010)
    p_refs = subparsers.add_parser(
        "references",
        help="[v0.8] List reference artifacts + ref-staleness",
    )
    p_refs.add_argument("--project-dir", required=True)
    p_refs.add_argument("--fmt", choices=["table", "tsv", "json"], default="table")
    p_refs.add_argument("--stale-only", dest="stale_only", action="store_true",
                        help="Show only outputs whose used reference version is stale")

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


def qc_command_dispatch() -> dict:
    """Command-name → function map that ``casetrack.main()`` merges into its own."""
    return {
        "censor": cmd_censor,
        "uncensor": cmd_uncensor,
        "qc-history": cmd_qc_history,
        "migrate-qc": cmd_migrate_qc,
        "cohort": cmd_cohort,
        "append-cohort": cmd_append_cohort,
        "migrate-cohort": cmd_migrate_cohort,
        "cohort-artifacts": cmd_cohort_artifacts,
        "migrate-references": cmd_migrate_references,
        "references": cmd_references,
        "migrate-derivation": cmd_migrate_derivation,
        "derived-from": cmd_derived_from,
        "derivation": cmd_derivation,
    }
