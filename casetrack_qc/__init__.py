"""casetrack_qc — QC events, censoring, and consent tracking (v0.4).

Implements proposal 0002. Lives alongside the v0.3 `casetrack.py` monolith;
integration happens at the argparse-dispatch level in `casetrack.main()` and
via minimal hooks in existing commands (append, rerun, status, export,
validate, recover, dashboard).

Public API:
- ``cmd_censor`` / ``cmd_uncensor`` / ``cmd_qc_history`` — manual QC CLI.
- ``cmd_migrate_qc`` — one-shot v0.3 → v0.4 upgrade.
- ``ensure_qc_schema`` — idempotent DDL (``qc_events`` + ``qc_status`` + consent
  columns). Called by ``casetrack init`` and ``casetrack migrate-qc``.
- ``build_qc_subparsers`` / ``qc_command_dispatch`` — argparse wiring helpers
  consumed by ``casetrack.main()``.
- ``recover_qc_action`` — replay handler for ``censor`` / ``uncensor`` /
  ``ethics_override`` / ``migrate_qc`` provenance entries.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

from casetrack_qc.censor import cmd_censor, cmd_qc_history, cmd_uncensor
from casetrack_qc.migrate import cmd_migrate_qc
from casetrack_qc.recover import recover_qc_action
from casetrack_qc.schema import (
    DEFAULT_CONSENT_ENUM,
    DEFAULT_QC_KIND_SCOPES,
    DEFAULT_QC_KINDS,
    DEFAULT_EXCLUDE_STATUSES,
    ensure_qc_schema,
    parse_qc_config,
    qc_schema_exists,
    write_qc_toml_block,
)
from casetrack_qc.cli import build_qc_subparsers, qc_command_dispatch

__all__ = [
    "cmd_censor",
    "cmd_migrate_qc",
    "cmd_qc_history",
    "cmd_uncensor",
    "DEFAULT_CONSENT_ENUM",
    "DEFAULT_EXCLUDE_STATUSES",
    "DEFAULT_QC_KIND_SCOPES",
    "DEFAULT_QC_KINDS",
    "build_qc_subparsers",
    "ensure_qc_schema",
    "parse_qc_config",
    "qc_command_dispatch",
    "qc_schema_exists",
    "recover_qc_action",
    "write_qc_toml_block",
]
