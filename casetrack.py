#!/usr/bin/env python3
"""
casetrack - Manifest-centric case management for bioinformatics pipelines.

Every analysis appends columns to a single manifest TSV, creating a growing
record of what's been computed for each sample. Built for HPC/SLURM environments
with file locking for concurrent job safety.

Usage:
    casetrack init      --manifest manifest.tsv --samples samples.txt [--cols col1,col2]
    casetrack append    --manifest manifest.tsv --results result.tsv --key sample_id --analysis modkit
    casetrack status    --manifest manifest.tsv [--analysis modkit] [--fmt table|tsv|json]
    casetrack validate  --manifest manifest.tsv --key sample_id
    casetrack log       --manifest manifest.tsv [--last N]
    casetrack schema    --manifest manifest.tsv [--fmt table|json]
    casetrack rerun     --manifest manifest.tsv --analysis tldr --script run_tldr.sh [--submit]
    casetrack dashboard    --manifest manifest.tsv --output dashboard.html
    casetrack add-metadata --manifest manifest.tsv --metadata clinical.tsv --key sample_id
    casetrack projects     --root ~/projects/
    casetrack query        --manifest manifest.tsv "SELECT sample_id FROM _ WHERE qc_pass"
    casetrack export       --manifest manifest.tsv --output out.xlsx

Author: Samuel Ahuno (sahuno)
"""

import argparse
import contextlib
import datetime
import fcntl
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import hashlib
import uuid
from pathlib import Path

try:
    import tomllib as _tomllib  # Python 3.11+
except ImportError:  # pragma: no cover — 3.10 path
    try:
        import tomli as _tomllib
    except ImportError:
        print(
            "Error: tomllib/tomli is required. On Python <3.11 install tomli: pip install tomli",
            file=sys.stderr,
        )
        sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("Error: pandas is required. Install with: pip install pandas", file=sys.stderr)
    sys.exit(1)


# ── Constants ──────────────────────────────────────────────────────────────────

PROVENANCE_SUFFIX = ".provenance.jsonl"
LOCK_SUFFIX = ".lock"
SCHEMA_SUFFIX = ".schema.json"

DONE_COLUMN_SUFFIX = "_done"
TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S"

# ── v0.3 project-mode constants ────────────────────────────────────────────────

PROJECT_DB_NAME = "casetrack.db"
PROJECT_TOML_NAME = "casetrack.toml"
PROJECT_PROVENANCE_NAME = "provenance.jsonl"
PROJECT_GITIGNORE_NAME = ".gitignore"

# Column types allowed in casetrack.toml schema.
VALID_COLUMN_TYPES = {"TEXT", "INTEGER", "REAL", "BOOLEAN", "DATE"}

# Hardcoded hierarchy for v0.3 (Q1 from proposal 0001 §19): patient → specimen → assay.
LEVEL_ORDER = ("patient", "specimen", "assay")

# Placeholders allowed in [layout.path_templates.<level>] entries.
# {tool}    — the analysis tool (matches an [analyses.<tool>] section name)
# {run_tag} — the run identifier (date_genome_description convention)
# {patient_id}, {specimen_id}, {assay_id} — level keys
LAYOUT_PLACEHOLDER_NAMES = {
    "tool", "run_tag", "patient_id", "specimen_id", "assay_id",
}

# Default pragma values for every SQLite connection casetrack opens.
SQLITE_BUSY_TIMEOUT_MS = 30000

# Casetrack version stamped into project_meta.casetrack_version on init
# (proposal 0005 Part B). Reflects the release that created the row, so
# future schema migrations can decide whether the row needs touch-ups.
_CASETRACK_VERSION = "0.6.0a1"

# project_meta DDL — proposal 0005 §6.1. CHECK constraint mirrors the
# Python-side _PROJECT_ID_PATTERN so a hand-edit of the SQLite file can't
# land a malformed slug. One row per database, written at init, never
# updated, never deleted.
_PROJECT_META_DDL = """
CREATE TABLE IF NOT EXISTS project_meta (
    project_id         TEXT NOT NULL PRIMARY KEY,
    name               TEXT NOT NULL,
    schema_v           INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    casetrack_version  TEXT NOT NULL,
    CHECK (
        length(project_id) BETWEEN 3 AND 64
        AND project_id GLOB '[a-z0-9]*'
        AND project_id NOT GLOB '*[^a-z0-9-]*'
    )
)
""".strip()

# Emitted once per invocation; silenced by CASETRACK_NO_DEPRECATION=1.
_DEPRECATION_EMITTED = False


def _warn_flat_deprecation():
    """Warn loudly (but once) that --manifest flat mode is on its way out.

    Gated by the env var so CI and batch scripts can silence cleanly.
    """
    global _DEPRECATION_EMITTED
    if _DEPRECATION_EMITTED:
        return
    if os.environ.get("CASETRACK_NO_DEPRECATION"):
        return
    _DEPRECATION_EMITTED = True
    print(
        "DeprecationWarning: flat-manifest mode (--manifest) is deprecated in "
        "v0.3 and will be removed in v1.0.\n"
        "  Migrate with: casetrack migrate --flat M.tsv --patient-col ... "
        "--specimen-col ... --assay-col ... --out-dir D/\n"
        "  Then switch to --project-dir. Silence this warning with "
        "CASETRACK_NO_DEPRECATION=1.",
        file=sys.stderr,
    )


# ── v0.3 TOML schema ───────────────────────────────────────────────────────────
#
# `casetrack.toml` declares the three-level schema (patient/specimen/assay).
# The DB is regenerable from TOML + provenance.jsonl (see §9.4 of proposal 0001),
# so TOML is the git-tracked source of schema truth.


class SchemaError(ValueError):
    """Raised when a casetrack.toml schema is malformed or internally inconsistent."""


def _blank_toml_template(project_name: str, project_id: str = "") -> str:
    """Minimal schema with just the primary keys and enforced parent FKs."""
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    pid_line = f'project_id = "{project_id}"\n' if project_id else ""
    return f"""[project]
{pid_line}name     = "{project_name}"
schema_v = 1
created  = "{now}"

[levels.patient]
key = "patient_id"

[levels.patient.columns]
patient_id = {{ type = "TEXT", required = true, unique = true }}

[levels.specimen]
key        = "specimen_id"
parent     = "patient"
parent_key = "patient_id"

[levels.specimen.columns]
specimen_id = {{ type = "TEXT", required = true, unique = true }}
patient_id  = {{ type = "TEXT", required = true }}

[levels.assay]
key        = "assay_id"
parent     = "specimen"
parent_key = "specimen_id"

[levels.assay.columns]
assay_id    = {{ type = "TEXT", required = true, unique = true }}
specimen_id = {{ type = "TEXT", required = true }}
assay_type  = {{ type = "TEXT", required = true }}

[analysis_defaults]
default_level = "assay"

[engine]
wal             = true
busy_timeout_ms = {SQLITE_BUSY_TIMEOUT_MS}

# Tool-first results directory convention — consumed by
# `casetrack append --infer-from-path`. Re-running a tool keeps outputs
# grouped under the same tool/, with separate {{run_tag}} subtrees per run.
[layout]
results_dir = "results"

[layout.path_templates]
patient  = "{{tool}}/{{run_tag}}/{{patient_id}}"
specimen = "{{tool}}/{{run_tag}}/{{patient_id}}/{{specimen_id}}"
assay    = "{{tool}}/{{run_tag}}/{{patient_id}}/{{specimen_id}}/{{assay_id}}"

# Declare each tool that writes into results/ so inference can reject typos
# and apply the right --column-prefix / summary-file convention per analysis.
# Example:
#
# [analyses.modkit_pileup]
# level         = "assay"
# column_prefix = "modkit"
# summary_tsv   = "modkit_summary.tsv"
"""


def _hgsoc_toml_template(project_name: str, project_id: str = "") -> str:
    """Template matching the example in proposal 0001 §6."""
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    pid_line = f'project_id = "{project_id}"\n' if project_id else ""
    return f"""[project]
{pid_line}name     = "{project_name}"
schema_v = 1
created  = "{now}"

[levels.patient]
key = "patient_id"

[levels.patient.columns]
patient_id  = {{ type = "TEXT", required = true, unique = true }}
age         = {{ type = "INTEGER" }}
sex         = {{ type = "TEXT", enum = ["F", "M", "intersex", "unknown"] }}
diagnosis   = {{ type = "TEXT" }}
brca_status = {{ type = "TEXT", enum = ["brca1", "brca2", "wt", "vus"] }}
neoadjuvant = {{ type = "BOOLEAN" }}
pfs_months  = {{ type = "REAL" }}
os_months   = {{ type = "REAL" }}

[levels.specimen]
key        = "specimen_id"
parent     = "patient"
parent_key = "patient_id"

[levels.specimen.columns]
specimen_id     = {{ type = "TEXT", required = true, unique = true }}
patient_id      = {{ type = "TEXT", required = true }}
tissue_site     = {{ type = "TEXT", required = true }}
timepoint       = {{ type = "TEXT" }}
collection_date = {{ type = "DATE" }}
tumor_purity    = {{ type = "REAL" }}

[levels.assay]
key        = "assay_id"
parent     = "specimen"
parent_key = "specimen_id"

[levels.assay.columns]
assay_id    = {{ type = "TEXT", required = true, unique = true }}
specimen_id = {{ type = "TEXT", required = true }}
assay_type  = {{ type = "TEXT", required = true, enum = ["scRNA", "ATAC", "WGS", "WES", "ONT", "Visium"] }}
replicate   = {{ type = "INTEGER", default = 1 }}
qc_pass     = {{ type = "BOOLEAN" }}

[analysis_defaults]
default_level = "assay"

[engine]
wal             = true
busy_timeout_ms = {SQLITE_BUSY_TIMEOUT_MS}

# Tool-first results directory convention — see the blank template comment.
[layout]
results_dir = "results"

[layout.path_templates]
patient  = "{{tool}}/{{run_tag}}/{{patient_id}}"
specimen = "{{tool}}/{{run_tag}}/{{patient_id}}/{{specimen_id}}"
assay    = "{{tool}}/{{run_tag}}/{{patient_id}}/{{specimen_id}}/{{assay_id}}"

# Declare one [analyses.<tool>] table per tool your pipeline runs.
# Example seeded for HGSOC ONT cohorts:
#
# [analyses.modkit_pileup]
# level         = "assay"
# column_prefix = "modkit"
# summary_tsv   = "modkit_summary.tsv"
#
# [analyses.cohort_dmr]
# level         = "patient"
# column_prefix = "dmr"
# summary_tsv   = "dmr_cohort_summary.tsv"
"""


def _giab_ont_toml_template(project_name: str, project_id: str = "") -> str:
    """Template for Oxford Nanopore WGS cohorts — e.g. Genome-in-a-Bottle
    reference samples. patient = biological sample (HG002, HG006, ...);
    specimen = one DNA extraction; assay = one flowcell run.
    """
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    pid_line = f'project_id = "{project_id}"\n' if project_id else ""
    return f"""[project]
{pid_line}name     = "{project_name}"
schema_v = 1
created  = "{now}"

[levels.patient]
key = "patient_id"

[levels.patient.columns]
patient_id       = {{ type = "TEXT", required = true, unique = true }}
sex              = {{ type = "TEXT", enum = ["F", "M", "intersex", "unknown"] }}
reference_source = {{ type = "TEXT" }}
trio_role        = {{ type = "TEXT", enum = ["proband", "father", "mother", "sibling", "unrelated"] }}
cohort           = {{ type = "TEXT" }}

[levels.specimen]
key        = "specimen_id"
parent     = "patient"
parent_key = "patient_id"

[levels.specimen.columns]
specimen_id   = {{ type = "TEXT", required = true, unique = true }}
patient_id    = {{ type = "TEXT", required = true }}
specimen_type = {{ type = "TEXT", enum = ["lymphoblastoid_dna", "whole_blood", "buccal", "whole_genome_dna"] }}
cell_line     = {{ type = "TEXT" }}
source        = {{ type = "TEXT" }}

[levels.assay]
key        = "assay_id"
parent     = "specimen"
parent_key = "specimen_id"

[levels.assay.columns]
assay_id         = {{ type = "TEXT", required = true, unique = true }}
specimen_id      = {{ type = "TEXT", required = true }}
assay_type       = {{ type = "TEXT", required = true, enum = ["ONT_WGS", "ONT_target", "ONT_cDNA", "ONT_direct_RNA"] }}
flowcell_id      = {{ type = "TEXT" }}
chemistry        = {{ type = "TEXT", enum = ["R9.4.1", "R10.4.1", "R10.4.1_dorado"] }}
basecaller_model = {{ type = "TEXT" }}
bam_path         = {{ type = "TEXT" }}
condition        = {{ type = "TEXT" }}
qc_pass          = {{ type = "BOOLEAN" }}

[analysis_defaults]
default_level = "assay"

[engine]
wal             = true
busy_timeout_ms = {SQLITE_BUSY_TIMEOUT_MS}

# Tool-first results directory convention — see the blank template comment.
[layout]
results_dir = "results"

[layout.path_templates]
patient  = "{{tool}}/{{run_tag}}/{{patient_id}}"
specimen = "{{tool}}/{{run_tag}}/{{patient_id}}/{{specimen_id}}"
assay    = "{{tool}}/{{run_tag}}/{{patient_id}}/{{specimen_id}}/{{assay_id}}"

# Example tool entries for GIAB ONT cohorts (extend as needed):
#
# [analyses.dorado_basecaller]
# level         = "assay"
# column_prefix = "dorado"
# summary_tsv   = "dorado_summary.tsv"
#
# [analyses.clair3]
# level         = "assay"
# column_prefix = "clair3"
# summary_tsv   = "clair3_summary.tsv"
"""


TEMPLATES = {
    "blank": _blank_toml_template,
    "hgsoc": _hgsoc_toml_template,
    "giab_ont": _giab_ont_toml_template,
}


def load_schema(toml_path: str | Path) -> dict:
    """Parse `casetrack.toml` into a validated schema dict.

    Returns the parsed TOML (as a plain dict) after validating structure.
    Raises SchemaError with a clear message on any violation.
    """
    toml_path = Path(toml_path)
    if not toml_path.exists():
        raise SchemaError(f"schema file not found: {toml_path}")
    with open(toml_path, "rb") as f:
        try:
            raw = _tomllib.load(f)
        except Exception as e:
            raise SchemaError(f"failed to parse {toml_path}: {e}") from e
    validate_schema(raw)
    return raw


def validate_schema(schema: dict) -> None:
    """Validate a parsed casetrack schema dict. Raises SchemaError on failure."""
    if "project" not in schema or not isinstance(schema["project"], dict):
        raise SchemaError("missing [project] section")
    proj = schema["project"]
    for key in ("name", "schema_v"):
        if key not in proj:
            raise SchemaError(f"[project] missing required key: {key}")

    # v0.6: [project] allow_unicode_ids (proposal 0005 Part A) — opt in to
    # relaxed hierarchy ID validation when non-ASCII characters are required.
    allow_unicode_ids = proj.get("allow_unicode_ids")
    if allow_unicode_ids is not None and not isinstance(allow_unicode_ids, bool):
        raise SchemaError(
            f"[project] allow_unicode_ids must be a boolean, "
            f"got {type(allow_unicode_ids).__name__}"
        )

    # v0.6: [project] project_id (proposal 0005 Part B). Optional in TOML
    # (legacy v0.5 projects don't have one); when present, must be a valid
    # DNS-label slug. Required at `casetrack init` (added by templates) but
    # not required to load an existing schema.
    project_id_value = proj.get("project_id")
    if project_id_value is not None:
        try:
            validate_project_id(project_id_value)
        except ValueError as e:
            raise SchemaError(f"[project] {e}") from e

    if "levels" not in schema or not isinstance(schema["levels"], dict):
        raise SchemaError("missing [levels.*] sections")

    for level in LEVEL_ORDER:
        if level not in schema["levels"]:
            raise SchemaError(f"missing [levels.{level}] section")
        _validate_level(level, schema["levels"][level])

    # Enforce hierarchy: specimen.parent == patient, assay.parent == specimen.
    expected_parents = {"patient": None, "specimen": "patient", "assay": "specimen"}
    for level, expected in expected_parents.items():
        actual = schema["levels"][level].get("parent")
        if actual != expected:
            raise SchemaError(
                f"[levels.{level}] parent mismatch: expected {expected!r}, got {actual!r}"
            )

    # [layout] and [analyses] are optional, additive across any schema_v. They
    # describe a tool-first results directory convention and the known tools
    # that write into it — consumed by `casetrack append --infer-from-path`.
    if "layout" in schema:
        _validate_layout(schema["layout"])
    if "analyses" in schema:
        _validate_analyses(schema["analyses"])


def _validate_layout(layout: dict) -> None:
    if not isinstance(layout, dict):
        raise SchemaError("[layout] must be a table")
    results_dir = layout.get("results_dir", "results")
    if not isinstance(results_dir, str) or not results_dir:
        raise SchemaError("[layout] results_dir must be a non-empty string")
    templates = layout.get("path_templates")
    if templates is None:
        raise SchemaError("[layout] missing [layout.path_templates]")
    if not isinstance(templates, dict) or not templates:
        raise SchemaError("[layout.path_templates] must be a non-empty table")
    import re as _re
    placeholder_re = _re.compile(r"\{(\w+)\}")
    for level, tmpl in templates.items():
        if level not in LEVEL_ORDER:
            raise SchemaError(
                f"[layout.path_templates.{level}] unknown level; must be one of "
                f"{list(LEVEL_ORDER)}"
            )
        if not isinstance(tmpl, str) or not tmpl:
            raise SchemaError(
                f"[layout.path_templates.{level}] must be a non-empty string"
            )
        for ph in placeholder_re.findall(tmpl):
            if ph not in LAYOUT_PLACEHOLDER_NAMES:
                raise SchemaError(
                    f"[layout.path_templates.{level}] unknown placeholder "
                    f"{{{ph}}}; allowed: {sorted(LAYOUT_PLACEHOLDER_NAMES)}"
                )
        # Every template must carry {tool} so the tool-first layout is enforceable.
        if "{tool}" not in tmpl:
            raise SchemaError(
                f"[layout.path_templates.{level}] must contain the {{tool}} placeholder"
            )
        # Level-appropriate key must be the deepest placeholder so inference
        # can map a path unambiguously to a row.
        required_key = {"patient": "patient_id",
                        "specimen": "specimen_id",
                        "assay": "assay_id"}[level]
        if f"{{{required_key}}}" not in tmpl:
            raise SchemaError(
                f"[layout.path_templates.{level}] must contain {{{required_key}}}"
            )


def _validate_analyses(analyses: dict) -> None:
    if not isinstance(analyses, dict):
        raise SchemaError("[analyses] must be a table")
    import re as _re
    tool_re = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    prefix_re = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for tool, spec in analyses.items():
        if not tool_re.match(tool):
            raise SchemaError(
                f"[analyses.{tool}] tool name must be a valid identifier "
                f"(letters/digits/underscores, not starting with a digit)"
            )
        if not isinstance(spec, dict):
            raise SchemaError(f"[analyses.{tool}] must be an inline table")
        level = spec.get("level")
        if level is None:
            raise SchemaError(f"[analyses.{tool}] missing required key: level")
        if level not in LEVEL_ORDER:
            raise SchemaError(
                f"[analyses.{tool}] level={level!r} must be one of {list(LEVEL_ORDER)}"
            )
        prefix = spec.get("column_prefix")
        if prefix is not None:
            if not isinstance(prefix, str) or not prefix_re.match(prefix):
                raise SchemaError(
                    f"[analyses.{tool}] column_prefix must be a valid identifier; "
                    f"got {prefix!r}"
                )
        summary_tsv = spec.get("summary_tsv")
        if summary_tsv is not None:
            if not isinstance(summary_tsv, str) or not summary_tsv:
                raise SchemaError(
                    f"[analyses.{tool}] summary_tsv must be a non-empty string"
                )


def _validate_level(level: str, spec: dict) -> None:
    if not isinstance(spec, dict):
        raise SchemaError(f"[levels.{level}] must be a table")
    if "key" not in spec:
        raise SchemaError(f"[levels.{level}] missing required key: key")
    if "columns" not in spec or not isinstance(spec["columns"], dict):
        raise SchemaError(f"[levels.{level}] missing [levels.{level}.columns] table")
    key = spec["key"]
    if key not in spec["columns"]:
        raise SchemaError(
            f"[levels.{level}] declared key {key!r} is not in [levels.{level}.columns]"
        )
    for colname, coldef in spec["columns"].items():
        _validate_column(level, colname, coldef)

    # Specimen and assay must also declare their parent foreign-key column.
    parent_key = spec.get("parent_key")
    if parent_key and parent_key not in spec["columns"]:
        raise SchemaError(
            f"[levels.{level}] parent_key {parent_key!r} not in [levels.{level}.columns]"
        )

    # v0.6: optional id_pattern override + allow_case_variants flag
    # (proposal 0005 Part A). Both are cached on the level_spec dict by
    # validate_hierarchy_id / check_id_case_unique on first use.
    id_pattern = spec.get("id_pattern")
    if id_pattern is not None:
        if not isinstance(id_pattern, str) or not id_pattern:
            raise SchemaError(
                f"[levels.{level}] id_pattern must be a non-empty string"
            )
        if not (id_pattern.startswith("^") and id_pattern.endswith("$")):
            raise SchemaError(
                f"[levels.{level}] id_pattern {id_pattern!r} must anchor "
                f"with ^ and $"
            )
        try:
            re.compile(id_pattern)
        except re.error as e:
            raise SchemaError(
                f"[levels.{level}] id_pattern {id_pattern!r} is not a valid "
                f"regex: {e}"
            )
    acv = spec.get("allow_case_variants")
    if acv is not None and not isinstance(acv, bool):
        raise SchemaError(
            f"[levels.{level}] allow_case_variants must be a boolean, "
            f"got {type(acv).__name__}"
        )


def _validate_column(level: str, name: str, spec) -> None:
    if not isinstance(spec, dict):
        raise SchemaError(f"[levels.{level}.columns.{name}] must be an inline table")
    ctype = spec.get("type")
    if ctype not in VALID_COLUMN_TYPES:
        raise SchemaError(
            f"[levels.{level}.columns.{name}] invalid type {ctype!r}; "
            f"must be one of {sorted(VALID_COLUMN_TYPES)}"
        )
    if "enum" in spec:
        enum = spec["enum"]
        if not isinstance(enum, list) or not all(isinstance(v, str) for v in enum):
            raise SchemaError(
                f"[levels.{level}.columns.{name}] enum must be a list of strings"
            )


# ── v0.6 hierarchy ID format enforcement (proposal 0005 Part A) ────────────────
#
# Validator runs only on INSERT paths (register, migrate flat→project,
# add-metadata --allow-new). Read paths (query, export, dashboard, recover)
# tolerate pre-existing malformed IDs unchanged. Opt-in loosening per level
# via [levels.<level>] id_pattern and allow_case_variants in casetrack.toml.
# Project-wide unicode opt-in via [project] allow_unicode_ids = true.

# Default ASCII regex: alnum start, then alnum / underscore / hyphen / dot, 1-64 chars.
# Use \A...\Z (not ^...$) because $ matches before a trailing \n in Python's
# default re flags — we want strict end-of-string anchoring.
_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")

# Characters forbidden in IDs in both ASCII and unicode modes — whitespace,
# path separators, shell metacharacters, control chars, null byte.
_ID_FORBIDDEN_CHARS = re.compile(
    r"[\s/\\:;|&<>'\"`$*?!()\x00-\x1f\x7f]"
)

# Reserved literals that would poison path joins even when otherwise valid.
_RESERVED_ID_LITERALS = frozenset({".", ".."})


def _get_level_id_pattern(schema: dict, level: str):
    """Return compiled custom id_pattern for a level, or None for default rules."""
    level_spec = schema["levels"][level]
    if "_compiled_id_pattern" in level_spec:
        return level_spec["_compiled_id_pattern"]
    custom = level_spec.get("id_pattern")
    compiled = re.compile(custom) if custom else None
    level_spec["_compiled_id_pattern"] = compiled
    return compiled


def validate_hierarchy_id(value, schema: dict, level: str) -> None:
    """Validate a hierarchy key value against the schema's format rules.

    Runs in order: null/empty check, reserved-literal rejection, then either
    the configured id_pattern (if set), the unicode-relaxed rules (if the
    project opted in), or the default ASCII regex.

    Raises ValueError with a message naming the offending value and rule.
    """
    if value is None:
        raise ValueError(f"{level}_id cannot be null")
    # pd.isna() raises TypeError for unhashable / array-like values — ignore
    # that path. Never swallow our own ValueError raised below on NA match.
    try:
        is_na = pd.isna(value)
    except (TypeError, ValueError):
        is_na = False
    if is_na is True:
        raise ValueError(f"{level}_id cannot be null")
    s = str(value)
    if not s or not s.strip():
        raise ValueError(
            f"{level}_id cannot be empty or whitespace-only (got {value!r})"
        )
    if s.strip() in _RESERVED_ID_LITERALS:
        raise ValueError(
            f"{level}_id cannot be {s!r} (reserved literal; path-traversal hazard)"
        )

    custom = _get_level_id_pattern(schema, level)
    if custom is not None:
        # fullmatch() avoids Python's default "$ matches before trailing \n"
        # semantics, which would otherwise let "P01\n" sneak past a user's
        # ^...$-anchored override regex.
        if not custom.fullmatch(s):
            raise ValueError(
                f"{level}_id {value!r} does not match configured "
                f"[levels.{level}] id_pattern {custom.pattern!r}"
            )
        return

    allow_unicode = bool(schema.get("project", {}).get("allow_unicode_ids", False))
    if allow_unicode:
        if len(s) < 1 or len(s) > 64:
            raise ValueError(
                f"{level}_id {value!r} length must be 1-64 chars (got {len(s)})"
            )
        if _ID_FORBIDDEN_CHARS.search(s):
            raise ValueError(
                f"{level}_id {value!r} contains a forbidden character "
                f"(whitespace, shell metacharacter, path separator, or control)"
            )
        if s[0] in "-.":
            raise ValueError(
                f"{level}_id {value!r} cannot start with '-' or '.'"
            )
        return

    if not _ID_PATTERN.fullmatch(s):
        raise ValueError(
            f"{level}_id {value!r} is not a valid identifier. "
            f"Must match {_ID_PATTERN.pattern!r} "
            f"(ASCII alnum start, then alnum/underscore/hyphen/dot, 1-64 chars). "
            f"To loosen, set [levels.{level}] id_pattern = \"...\" or "
            f"[project] allow_unicode_ids = true in casetrack.toml."
        )


def check_id_case_unique(
    conn: sqlite3.Connection,
    schema: dict,
    level: str,
    value: str,
    folded_existing: set | None = None,
) -> None:
    """Reject the insert if a case-variant of `value` already exists.

    `folded_existing` is an optional pre-computed casefold()-set of IDs
    already in the table; pass it for batch inserts to avoid an O(N) query
    per row. If omitted, a single SELECT runs per call.
    """
    level_spec = schema["levels"][level]
    if level_spec.get("allow_case_variants"):
        return
    key_col = level_spec["key"]
    table = f"{level}s"
    folded = str(value).casefold()
    if folded_existing is not None:
        if folded not in folded_existing:
            return
        # Case-variant found — fetch the actual value for the error message.
        row = conn.execute(
            f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)} "
            f"WHERE lower({_quote_ident(key_col)}) = lower(?) LIMIT 1",
            (str(value),),
        ).fetchone()
        existing = row[0] if row else "?"
    else:
        row = conn.execute(
            f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)} "
            f"WHERE lower({_quote_ident(key_col)}) = lower(?) LIMIT 1",
            (str(value),),
        ).fetchone()
        if not row or row[0] == str(value):
            return
        existing = row[0]
    raise ValueError(
        f"{level}_id {value!r} conflicts with existing case-variant {existing!r}. "
        f"Set [levels.{level}] allow_case_variants = true in casetrack.toml to allow."
    )


def _preload_folded_ids(
    conn: sqlite3.Connection, schema: dict, level: str
) -> set:
    """Return the casefold()-set of IDs currently in `{level}s` — for batch inserts."""
    level_spec = schema["levels"][level]
    key_col = level_spec["key"]
    table = f"{level}s"
    return {
        str(r[0]).casefold()
        for r in conn.execute(
            f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)}"
        ).fetchall()
    }


# ── v0.6 project identity (proposal 0005 Part B) ──────────────────────────────
#
# `project_id` is a DNS-label-shaped slug that uniquely identifies a casetrack
# project on this machine. Stored in three places at init time and cross-checked
# at every command:
#   1. casetrack.toml [project] project_id  — human-editable source of truth
#   2. project_meta SQLite table             — DB self-describes
#   3. ~/.casetrack/registry.json            — single-user registry for
#                                              `casetrack --project <id>` lookup
#
# Stricter than hierarchy IDs: lowercase only, hyphens (not underscores or dots),
# 3–64 chars. Matches Docker repo / Kubernetes namespace naming so it's safe in
# URLs, CLI flags, and shell pipelines.

_PROJECT_ID_PATTERN = re.compile(r"\A[a-z0-9][a-z0-9-]{2,63}\Z")


def validate_project_id(value) -> None:
    """Raise ValueError if `value` is not a valid project_id slug.

    Format: ^[a-z0-9][a-z0-9-]{2,63}$ (DNS-label shape, 3-64 chars,
    lowercase-only, hyphens allowed).
    """
    if value is None:
        raise ValueError("project_id cannot be null")
    if not isinstance(value, str):
        raise ValueError(
            f"project_id must be a string; got {type(value).__name__}: {value!r}"
        )
    if not _PROJECT_ID_PATTERN.fullmatch(value):
        raise ValueError(
            f"project_id {value!r} is not a valid identifier. "
            f"Must match {_PROJECT_ID_PATTERN.pattern!r} "
            f"(DNS-label shape: lowercase, hyphens, 3-64 chars). "
            f"Examples: hgsoc-2026, giab-pilot, methylation-cohort-spring-2026."
        )


def suggest_project_id(name: str) -> str | None:
    """Derive a valid project_id slug from a free-form name or directory.

    Heuristic: lowercase, replace runs of non-alnum with single hyphen,
    strip leading/trailing hyphens, truncate to 64 chars. Returns the
    cleaned slug iff it matches _PROJECT_ID_PATTERN, else None.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    s = name.lower()
    # Collapse runs of non-(a-z0-9) into a single hyphen.
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    s = s[:64]
    return s if s and _PROJECT_ID_PATTERN.fullmatch(s) else None


def write_project_meta(
    conn: sqlite3.Connection,
    project_id: str,
    name: str,
    schema_v: int,
) -> None:
    """Insert the single project_meta row at init. Idempotent on existing rows."""
    validate_project_id(project_id)
    conn.execute(_PROJECT_META_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO project_meta "
        "(project_id, name, schema_v, created_at, casetrack_version) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            project_id,
            name,
            int(schema_v),
            datetime.datetime.now().strftime(TIMESTAMP_FMT),
            _CASETRACK_VERSION,
        ),
    )


def read_project_meta(conn: sqlite3.Connection) -> dict | None:
    """Return the project_meta row as a dict, or None if the table doesn't
    exist (legacy v0.5 project) or is empty.
    """
    try:
        row = conn.execute(
            "SELECT project_id, name, schema_v, created_at, casetrack_version "
            "FROM project_meta LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # table doesn't exist — legacy project
    if row is None:
        return None
    return {
        "project_id": row[0],
        "name": row[1],
        "schema_v": row[2],
        "created_at": row[3],
        "casetrack_version": row[4],
    }


# ── v0.6 registry: ~/.casetrack/registry.json (proposal 0005 §6.3) ────────────
#
# Single-user JSON registry mapping project_id → {path, name, created, last_seen}.
# Enables `casetrack --project hgsoc-2026 query "..."` without remembering paths.
# fcntl.flock guards concurrent writes from parallel casetrack invocations.
# Team-shared / globally-unique-UUID variants are deferred to a later proposal
# (§8 Q1 / Q2).

_REGISTRY_SCHEMA_V = 1


def _registry_path() -> Path:
    """Return ~/.casetrack/registry.json — env override via CASETRACK_REGISTRY."""
    override = os.environ.get("CASETRACK_REGISTRY")
    if override:
        return Path(override)
    return Path.home() / ".casetrack" / "registry.json"


def _registry_load(path: Path | None = None) -> dict:
    """Read the registry, returning {schema_v, projects} (empty if missing)."""
    p = path or _registry_path()
    if not p.exists():
        return {"schema_v": _REGISTRY_SCHEMA_V, "projects": {}}
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"registry at {p} is corrupt JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"registry at {p} is not a JSON object")
    data.setdefault("schema_v", _REGISTRY_SCHEMA_V)
    data.setdefault("projects", {})
    return data


@contextlib.contextmanager
def _registry_locked(path: Path | None = None):
    """Yield (registry_dict, lock_fd). Saves + releases on normal exit."""
    p = path or _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Open in 'a+' so the file is created if missing without truncating.
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.lseek(fd, 0, 0)
            raw = os.read(fd, 1 << 24).decode("utf-8") or "{}"
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"registry at {p} is corrupt JSON: {e}") from e
            data.setdefault("schema_v", _REGISTRY_SCHEMA_V)
            data.setdefault("projects", {})
            yield data
            # Write back atomically: truncate + write under the same lock.
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, 0)
            payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
            os.write(fd, payload)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def registry_register(
    project_id: str,
    project_dir: Path | str,
    name: str,
    *,
    registry_path: Path | None = None,
) -> None:
    """Add or refresh a registry entry. Idempotent on existing project_id."""
    validate_project_id(project_id)
    abs_path = str(Path(project_dir).resolve())
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    with _registry_locked(registry_path) as reg:
        existing = reg["projects"].get(project_id)
        if existing and existing.get("path") != abs_path:
            raise ValueError(
                f"registry conflict: project_id {project_id!r} already maps "
                f"to {existing['path']!r}, refusing to clobber with "
                f"{abs_path!r}. Use `casetrack projects deregister "
                f"{project_id}` first if you really want to retarget."
            )
        if existing:
            # Just bump last_seen + name; keep created.
            existing["name"] = name
            existing["last_seen"] = now
        else:
            reg["projects"][project_id] = {
                "path": abs_path,
                "name": name,
                "created": now,
                "last_seen": now,
            }


def registry_deregister(
    project_id: str, *, registry_path: Path | None = None
) -> bool:
    """Remove an entry. Returns True if removed, False if not present."""
    with _registry_locked(registry_path) as reg:
        return reg["projects"].pop(project_id, None) is not None


def registry_resolve(
    project_id: str, *, registry_path: Path | None = None
) -> Path | None:
    """Return the project directory path for `project_id`, or None if unknown.

    Read-only — does not bump last_seen. (last_seen is bumped via
    `registry_touch` once a command has confirmed the project loads.)
    """
    reg = _registry_load(registry_path)
    entry = reg["projects"].get(project_id)
    return Path(entry["path"]) if entry else None


def registry_touch(
    project_id: str, *, registry_path: Path | None = None
) -> None:
    """Bump last_seen for `project_id`. Silent no-op if not registered."""
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    try:
        with _registry_locked(registry_path) as reg:
            entry = reg["projects"].get(project_id)
            if entry is not None:
                entry["last_seen"] = now
    except (OSError, ValueError):
        # Don't fail commands over a registry hiccup — purely cosmetic.
        pass


# Env-var bypass for the v0.6 final hard-error gate. Set to a truthy value
# (1, true, yes — case-insensitive) to allow operations on legacy projects
# that haven't been migrated yet. Intended for batch scripts auditing many
# projects at once and for short-lived inspection of inherited cohorts.
_LEGACY_BYPASS_ENV = "CASETRACK_ALLOW_LEGACY"


def _legacy_bypass_enabled() -> bool:
    return os.environ.get(_LEGACY_BYPASS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def require_project_identity_or_fail(
    conn: sqlite3.Connection, schema: dict, project_dir: Path
) -> None:
    """Refuse to proceed when a project has no v0.6 identity wiring.

    Triggered from `_resolve_project` for both read and write commands.
    Bypassable via CASETRACK_ALLOW_LEGACY=1 for batch audits / inspection.
    Suggestion-only — never auto-runs migrate-project-id, since that's a
    destructive write the user should opt into explicitly.
    """
    toml_pid = schema.get("project", {}).get("project_id")
    meta = read_project_meta(conn)
    if toml_pid and meta:
        return  # fully migrated — no gate needed
    if _legacy_bypass_enabled():
        return  # explicit opt-out

    missing: list[str] = []
    if not toml_pid:
        missing.append("[project] project_id in casetrack.toml")
    if meta is None:
        missing.append("project_meta row in casetrack.db")
    raise ValueError(
        f"This project is missing v0.6 identity wiring "
        f"({', '.join(missing)}). Run:\n"
        f"    casetrack migrate-project-id --project-dir {project_dir}\n"
        f"To bypass for a one-off read or batch audit, set "
        f"{_LEGACY_BYPASS_ENV}=1."
    )


def check_project_identity_consistency(
    conn: sqlite3.Connection, schema: dict, project_dir: Path | None = None
) -> None:
    """Compare TOML's [project] project_id to the project_meta row.

    Hard error on mismatch — someone copied casetrack.db into the wrong
    project directory or hand-edited project_id in TOML after init. Either
    way we refuse to proceed because subsequent operations would corrupt
    the registry's path↔project_id mapping.

    Skipped silently when:
      - project_meta row is absent (legacy v0.5 project)
      - TOML has no project_id (legacy or partially-migrated project)

    Both paths are tolerated for the alpha rollout — the v0.6.0 final
    release will tighten this to a hard requirement.
    """
    toml_project_id = schema.get("project", {}).get("project_id")
    if not toml_project_id:
        return  # legacy TOML — skip
    meta = read_project_meta(conn)
    if meta is None:
        return  # legacy DB — skip
    if meta["project_id"] != toml_project_id:
        loc = f" at {project_dir}" if project_dir else ""
        raise ValueError(
            f"project_id mismatch{loc}: casetrack.toml says "
            f"{toml_project_id!r}, but casetrack.db's project_meta row says "
            f"{meta['project_id']!r}. This usually means the DB was copied "
            f"into a different project directory, or [project] project_id "
            f"in casetrack.toml was edited after init. "
            f"To recover: restore the original casetrack.toml, OR re-run "
            f"`casetrack init --force` in this directory with the correct "
            f"--project-id."
        )


# ── v0.3 SQLite engine ─────────────────────────────────────────────────────────


def open_project_db(db_path: str | Path) -> sqlite3.Connection:
    """Open (or create) a casetrack SQLite DB with WAL + busy_timeout + FKs on.

    Every casetrack connection should go through this factory so concurrency
    pragmas are consistent across commands (proposal 0001 §9.1).
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextlib.contextmanager
def begin_immediate(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE … COMMIT/ROLLBACK context (proposal 0001 §9.1).

    `IMMEDIATE` acquires a reserved lock up front so two concurrent writers
    don't both enter the transaction optimistically and collide at commit.
    Any exception inside the block triggers ROLLBACK.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def schema_to_ddl(schema: dict) -> list[str]:
    """Render a validated schema dict into CREATE TABLE statements.

    Order matters — parents first so child FKs resolve during CREATE.
    """
    statements = []
    for level in LEVEL_ORDER:
        spec = schema["levels"][level]
        statements.append(_create_table_ddl(level, spec))
    return statements


def _create_table_ddl(level: str, spec: dict) -> str:
    table = f"{level}s"  # patients, specimens, assays
    cols = []
    for name, col in spec["columns"].items():
        parts = [_quote_ident(name), col["type"]]
        if col.get("required"):
            parts.append("NOT NULL")
        if col.get("unique") and name != spec["key"]:
            parts.append("UNIQUE")
        if "default" in col:
            parts.append(f"DEFAULT {_quote_literal(col['default'])}")
        if "enum" in col:
            values = ", ".join(_quote_literal(v) for v in col["enum"])
            parts.append(f"CHECK ({_quote_ident(name)} IN ({values}))")
        cols.append(" ".join(parts))

    cols.append(f"PRIMARY KEY ({_quote_ident(spec['key'])})")

    parent = spec.get("parent")
    if parent:
        parent_table = f"{parent}s"
        parent_key = spec["parent_key"]
        cols.append(
            f"FOREIGN KEY ({_quote_ident(parent_key)}) "
            f"REFERENCES {_quote_ident(parent_table)}({_quote_ident(parent_key)}) "
            f"ON DELETE RESTRICT"
        )

    body = ",\n    ".join(cols)
    return f"CREATE TABLE {_quote_ident(table)} (\n    {body}\n)"


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier (table or column). Rejects embedded double-quotes
    to avoid breaking identifier quoting — schema identifiers come from TOML,
    a trusted source, so a reject-on-violation policy is appropriate."""
    if '"' in name:
        raise SchemaError(f"identifier contains embedded double-quote: {name!r}")
    return f'"{name}"'


def _quote_literal(value) -> str:
    """Quote a SQL literal (used for CHECK enums and DEFAULTs)."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def apply_schema(conn: sqlite3.Connection, schema: dict) -> None:
    """Run every CREATE TABLE from `schema` inside one transaction."""
    with begin_immediate(conn):
        for ddl in schema_to_ddl(schema):
            conn.execute(ddl)


# ── v0.3 project-mode provenance ───────────────────────────────────────────────


def log_project_provenance(project_dir: str | Path, entry: dict) -> None:
    """Append a provenance record to the project's provenance.jsonl.

    Parallel to `log_provenance` (flat-mode) but targets `<project>/provenance.jsonl`.
    Populates timestamp / user / host / SLURM ids / git state automatically.
    """
    log_path = Path(project_dir) / PROJECT_PROVENANCE_NAME
    entry["timestamp"] = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    entry["user"] = os.environ.get("USER", "unknown")
    entry["hostname"] = os.environ.get("HOSTNAME", "unknown")
    entry["slurm_job_id"] = os.environ.get("SLURM_JOB_ID", None)
    entry["slurm_array_task_id"] = os.environ.get("SLURM_ARRAY_TASK_ID", None)
    entry["git"] = _git_state()
    with open(log_path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _new_transaction_id() -> str:
    return "txn_" + datetime.datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]


# ── File Locking ───────────────────────────────────────────────────────────────

class ManifestLock:
    """Context manager for file-level locking. Safe for concurrent SLURM jobs."""

    def __init__(self, manifest_path: str, timeout: int = 300):
        self.lock_path = manifest_path + LOCK_SUFFIX
        self.timeout = timeout
        self._lockfile = None

    def __enter__(self):
        self._lockfile = open(self.lock_path, "w")
        try:
            fcntl.flock(self._lockfile, fcntl.LOCK_EX)
        except OSError as e:
            self._lockfile.close()
            raise RuntimeError(
                f"Could not acquire lock on {self.lock_path}: {e}\n"
                f"If a previous job crashed, remove the lock file manually."
            ) from e
        return self

    def __exit__(self, *args):
        if self._lockfile:
            fcntl.flock(self._lockfile, fcntl.LOCK_UN)
            self._lockfile.close()


# ── Provenance Logging ─────────────────────────────────────────────────────────

_GIT_STATE_CACHE = {}


def _git_state(cwd: str = None, use_cache: bool = True) -> dict:
    """Capture minimal git state of `cwd` (defaults to process CWD).

    Returns {commit, branch, dirty, toplevel} or None if not in a repo, if
    git is unavailable, if the calls time out, or if the caller has opted
    out via CASETRACK_NO_GIT=1. Never raises — provenance must not break
    because of a missing git binary.

    Results are memoized per-process on `(cwd, CASETRACK_NO_GIT)` so a CLI
    invocation that writes many provenance entries (e.g. a parallel rerun)
    only pays for git once.
    """
    no_git = os.environ.get("CASETRACK_NO_GIT") or ""
    cache_key = (cwd or os.getcwd(), no_git)
    if use_cache and cache_key in _GIT_STATE_CACHE:
        return _GIT_STATE_CACHE[cache_key]

    if no_git:
        _GIT_STATE_CACHE[cache_key] = None
        return None

    import subprocess

    def _run(*args):
        try:
            return subprocess.run(
                ["git", *args], cwd=cwd,
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    top = _run("rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0 or not top.stdout.strip():
        _GIT_STATE_CACHE[cache_key] = None
        return None

    commit = _run("rev-parse", "HEAD")
    branch = _run("rev-parse", "--abbrev-ref", "HEAD")
    status = _run("status", "--porcelain")

    result = {
        "commit": (commit.stdout.strip() if commit and commit.returncode == 0 else None),
        "branch": (branch.stdout.strip() if branch and branch.returncode == 0 else None),
        "dirty": bool(status.stdout.strip()) if (status and status.returncode == 0) else None,
        "toplevel": top.stdout.strip(),
    }
    _GIT_STATE_CACHE[cache_key] = result
    return result


def log_provenance(manifest_path: str, entry: dict):
    """Append a provenance record as a JSONL line."""
    log_path = manifest_path + PROVENANCE_SUFFIX
    entry["timestamp"] = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    entry["user"] = os.environ.get("USER", "unknown")
    entry["hostname"] = os.environ.get("HOSTNAME", "unknown")
    entry["slurm_job_id"] = os.environ.get("SLURM_JOB_ID", None)
    entry["slurm_array_task_id"] = os.environ.get("SLURM_ARRAY_TASK_ID", None)
    entry["git"] = _git_state()

    with open(log_path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _checksum(filepath: str) -> str:
    """Quick MD5 checksum of a file for provenance tracking."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Schema Tracking ────────────────────────────────────────────────────────────

def update_schema(manifest_path: str, analysis: str, new_columns: list):
    """Track which analysis added which columns."""
    schema_path = manifest_path + SCHEMA_SUFFIX
    schema = {}
    if os.path.exists(schema_path):
        with open(schema_path) as f:
            schema = json.load(f)

    schema[analysis] = {
        "columns": new_columns,
        "added": datetime.datetime.now().strftime(TIMESTAMP_FMT),
        "added_by": os.environ.get("USER", "unknown"),
    }

    with open(schema_path, "w") as f:
        json.dump(schema, f, indent=2)


# ── Smart Merge ────────────────────────────────────────────────────────────────

def fill_nan_cells(manifest: "pd.DataFrame", results: "pd.DataFrame",
                   key_col: str, cols: list) -> "pd.DataFrame":
    """Fill NaN cells in `manifest[cols]` from `results[cols]` joined on `key_col`.

    Vectorized replacement for a row-wise iterrows() loop. Existing non-NaN
    values in the manifest are preserved (smart-merge / fill-only semantics).
    Keys are compared as strings to match the prior behavior.
    """
    merged = manifest.copy()
    if not cols:
        return merged

    results_by_key = results.copy()
    results_by_key[key_col] = results_by_key[key_col].astype(str)
    results_by_key = results_by_key.set_index(key_col)
    merged_keys = merged[key_col].astype(str)

    for col in cols:
        if col not in results_by_key.columns:
            continue
        rcol = results_by_key[col]
        if not rcol.index.is_unique:
            rcol = rcol[~rcol.index.duplicated(keep="first")]
        # Project results values onto the merged row order via the key.
        new_values = merged_keys.map(rcol)
        mask = merged[col].isna() & new_values.notna()
        if mask.any():
            # Promote target to object when dtypes differ — otherwise pandas 3.0
            # raises on e.g. assigning timestamp strings into an all-NaN float64
            # column pre-created by `init --cols`.
            if merged[col].dtype != new_values.dtype:
                merged[col] = merged[col].astype(object)
            merged.loc[mask, col] = new_values.loc[mask]
    return merged


# ── Core Commands ──────────────────────────────────────────────────────────────

# Proposal 0003: `casetrack init` scaffolds a full project tree by default.
# Leaves listed here get `mkdir -p` + a `.gitkeep` file so the layout survives
# git clone. `results/<analysis>/<assay_id>/` is intentionally NOT here —
# pipelines create those on first append, and the analysis mix varies per
# project. `--bare` on init skips this scaffold.
SCAFFOLD_LEAVES: tuple[str, ...] = (
    "data/raw",
    "data/ref",
    "data/validation",
    "results",
    "scripts",
    "docs/research",
    "docs/hypothesis",
    "manuscript/figures/scripts/png",
    "manuscript/figures/scripts/pdf",
    "manuscript/figures/scripts/svg",
    "manuscript/draft",
    "manuscript/proofs",
    "manuscript/references",
    "logs",
    "containers",
    "sandbox",
)


def _scaffold_project_tree(project_dir: Path) -> list[str]:
    """Create SCAFFOLD_LEAVES under `project_dir` with `.gitkeep` in each leaf.

    Idempotent: existing dirs and .gitkeep files are left alone (no mtime
    change). Returns the list of leaf paths (relative) that now exist.
    """
    created: list[str] = []
    for leaf in SCAFFOLD_LEAVES:
        d = project_dir / leaf
        d.mkdir(parents=True, exist_ok=True)
        keeper = d / ".gitkeep"
        if not keeper.exists():
            keeper.touch()
        created.append(leaf)
    return created


def cmd_init(args):
    """Dispatch `casetrack init` to flat (--manifest) or project (--project-dir) mode."""
    if getattr(args, "project_dir", None):
        return cmd_init_project(args)
    return cmd_init_flat(args)


def cmd_init_project(args):
    """Initialize a v0.3 casetrack project directory.

    Creates: <dir>/casetrack.toml, <dir>/casetrack.db (with the three-level
    schema applied), <dir>/provenance.jsonl, <dir>/.gitignore.
    """
    project_dir = Path(args.project_dir)
    template = getattr(args, "from_template", None) or "blank"
    if template not in TEMPLATES:
        print(
            f"Error: unknown template {template!r}. Known: {sorted(TEMPLATES)}",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = project_dir / PROJECT_DB_NAME
    toml_path = project_dir / PROJECT_TOML_NAME
    prov_path = project_dir / PROJECT_PROVENANCE_NAME
    gitignore_path = project_dir / PROJECT_GITIGNORE_NAME

    if db_path.exists() and not args.force:
        print(
            f"Error: {db_path} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    project_dir.mkdir(parents=True, exist_ok=True)

    project_name = args.project_name or project_dir.resolve().name

    # v0.6: derive/validate project_id (proposal 0005 Part B). Resolution
    # order: explicit --project-id > slug derived from --project-name >
    # slug derived from directory basename. Fail loudly if none of the
    # three produce a valid DNS-label slug.
    explicit_pid = getattr(args, "project_id", None)
    if explicit_pid:
        try:
            validate_project_id(explicit_pid)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        project_id = explicit_pid
    else:
        project_id = (
            suggest_project_id(project_name)
            or suggest_project_id(project_dir.resolve().name)
        )
        if not project_id:
            print(
                "Error: could not derive a valid project_id from "
                f"--project-name {project_name!r} or directory name "
                f"{project_dir.resolve().name!r}. Pass --project-id explicitly "
                "(format: lowercase alnum + hyphen, 3-64 chars; e.g. "
                "'hgsoc-2026').",
                file=sys.stderr,
            )
            sys.exit(1)

    toml_text = TEMPLATES[template](project_name, project_id)
    toml_path.write_text(toml_text)

    try:
        schema = load_schema(toml_path)
    except SchemaError as e:
        print(f"Error: generated schema failed validation: {e}", file=sys.stderr)
        sys.exit(1)

    if db_path.exists():
        db_path.unlink()
        # Clean up WAL/SHM leftovers from the old DB so they don't get reused.
        for suffix in ("-wal", "-shm"):
            leftover = Path(str(db_path) + suffix)
            if leftover.exists():
                leftover.unlink()

    # v0.4 QC schema is bolted on after the three-level core — keeps the QC
    # subsystem self-contained so v0.3 projects can upgrade via `migrate-qc`
    # and new projects get it by default.
    from casetrack_qc.schema import (
        DEFAULT_QC_KINDS as _QC_DEFAULT_KINDS,
        ensure_qc_schema as _ensure_qc_schema,
        write_qc_toml_block as _write_qc_toml_block,
    )

    conn = open_project_db(db_path)
    qc_ddl: list[str] = []
    try:
        apply_schema(conn, schema)
        with begin_immediate(conn):
            qc_ddl = _ensure_qc_schema(conn, kinds=_QC_DEFAULT_KINDS)
            # v0.6 Part B: write the project_meta row in the same
            # transaction as the QC schema so init is atomic.
            write_project_meta(
                conn, project_id, project_name, schema["project"]["schema_v"]
            )
    finally:
        conn.close()

    # v0.6 Part B: register in the user's local registry. Failure here is
    # surfaced but doesn't roll back the project — the user can manually
    # `casetrack projects register --project-dir <dir>` later.
    try:
        registry_register(project_id, project_dir, project_name)
    except (OSError, ValueError) as e:
        print(
            f"Warning: project initialised but registry update failed: {e}\n"
            f"  Re-run `casetrack projects register --project-dir "
            f"{project_dir}` to retry.",
            file=sys.stderr,
        )

    # Append the [qc] TOML block so teams can extend kinds / scopes.
    _write_qc_toml_block(toml_path)

    # Empty provenance log — touch to create.
    if not prov_path.exists():
        prov_path.touch()

    if not gitignore_path.exists():
        gitignore_path.write_text(_project_gitignore_contents())

    # Proposal 0003: scaffold the full project tree unless the caller opted
    # out with --bare. Idempotent so re-running init on an existing project
    # only fills in missing leaves.
    bare = bool(getattr(args, "bare", False))
    scaffold_leaves: list[str] = []
    if not bare:
        scaffold_leaves = _scaffold_project_tree(project_dir)

    log_project_provenance(project_dir, {
        "action": "init_project",
        "transaction_id": _new_transaction_id(),
        "template": template,
        "project_name": project_name,
        "schema_v_before": 0,
        "schema_v_after": schema["project"]["schema_v"],
        "sql": schema_to_ddl(schema) + qc_ddl,
        "qc_schema_v": 1,
        "scaffold": "full" if not bare else "bare",
        "scaffold_leaves": scaffold_leaves,
    })

    lines = [
        f"Initialized casetrack project at {project_dir}/",
        f"  - project_id:   {project_id!r}",
        f"  - {PROJECT_TOML_NAME}  ({template} template)",
        f"  - {PROJECT_DB_NAME}    (three levels: {', '.join(LEVEL_ORDER)})",
        f"  - {PROJECT_PROVENANCE_NAME}",
        f"  - {PROJECT_GITIGNORE_NAME}",
    ]
    if not bare:
        lines.append(f"  - scaffold: {len(scaffold_leaves)} leaf directories (.gitkeep in each)")
    lines.append(
        f"  - registered in {_registry_path()} (use `casetrack --project "
        f"{project_id} ...` from anywhere)"
    )
    print("\n".join(lines))


def _project_gitignore_contents() -> str:
    """Contents of `.gitignore` for a fresh project.

    Extended in proposal 0003 to cover large analysis artifacts that belong
    in the manifest (via bam_path columns etc.) rather than in git: raw
    inputs under data/raw/, Apptainer SIFs, merged BAMs, bedMethyl.gz, VCF
    bundles. `.gitkeep` files created by the scaffold are explicitly
    re-included with `!` negations so empty leaves survive commit.
    """
    return (
        "# casetrack project .gitignore — proposals 0001 §5 + 0003\n"
        "# DB is binary and regenerable from casetrack.toml + provenance.jsonl.\n"
        f"{PROJECT_DB_NAME}\n"
        f"{PROJECT_DB_NAME}-wal\n"
        f"{PROJECT_DB_NAME}-shm\n"
        "\n"
        "# Large artifacts — tracked in the manifest, not in git.\n"
        "data/raw/*\n"
        "!data/raw/.gitkeep\n"
        "containers/*.sif\n"
        "!containers/.gitkeep\n"
        "results/**/*.bam\n"
        "results/**/*.bam.bai\n"
        "results/**/*.cram\n"
        "results/**/*.cram.crai\n"
        "results/**/*.bedMethyl.gz\n"
        "results/**/*.tbi\n"
        "results/**/*.vcf.gz\n"
        "results/**/*.fastq.gz\n"
        "\n"
        "# Exports and working artifacts.\n"
        "exports/\n"
        "sandbox/*\n"
        "!sandbox/.gitkeep\n"
    )


def cmd_init_flat(args):
    """Initialize a new flat-manifest (v0.2 style)."""
    _warn_flat_deprecation()
    manifest_path = args.manifest

    if not getattr(args, "samples", None):
        print(
            "Error: --samples is required with --manifest (flat mode).",
            file=sys.stderr,
        )
        sys.exit(1)

    if os.path.exists(manifest_path) and not args.force:
        print(f"Error: {manifest_path} already exists. Use --force to overwrite.", file=sys.stderr)
        sys.exit(1)

    # Read sample IDs
    samples_path = args.samples
    if not os.path.exists(samples_path):
        print(f"Error: samples file not found: {samples_path}", file=sys.stderr)
        sys.exit(1)

    with open(samples_path) as f:
        sample_ids = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not sample_ids:
        print("Error: no sample IDs found in samples file.", file=sys.stderr)
        sys.exit(1)

    # Build initial manifest
    key_col = args.key
    df = pd.DataFrame({key_col: sample_ids})

    # Add optional extra columns from a metadata TSV
    if args.metadata:
        if not os.path.exists(args.metadata):
            print(f"Error: metadata file not found: {args.metadata}", file=sys.stderr)
            sys.exit(1)
        meta = pd.read_csv(args.metadata, sep="\t")
        if key_col not in meta.columns:
            print(f"Error: key column '{key_col}' not found in metadata file.", file=sys.stderr)
            sys.exit(1)
        df = df.merge(meta, on=key_col, how="left")

    # Add optional bare columns
    if args.cols:
        for col in args.cols.split(","):
            col = col.strip()
            if col and col not in df.columns:
                df[col] = pd.NA

    df.to_csv(manifest_path, sep="\t", index=False)

    log_provenance(manifest_path, {
        "action": "init",
        "samples_file": str(samples_path),
        "n_samples": len(sample_ids),
        "columns": list(df.columns),
    })

    print(f"Initialized {manifest_path} with {len(sample_ids)} samples, {len(df.columns)} columns.")


def cmd_append(args):
    """Dispatch `casetrack append` to flat (--manifest) or project (--project-dir) mode.

    When ``--infer-from-path`` is given, resolve the project and fill missing
    args (project_dir, level, analysis, column_prefix, results, inferred
    run_tag) before dispatching to project-mode.
    """
    if getattr(args, "infer_from_path", None) is not None:
        _apply_path_inference(args)

    if not getattr(args, "project_dir", None) and not getattr(args, "manifest", None):
        print(
            "Error: one of --project-dir, --manifest, or --infer-from-path is required.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not getattr(args, "results", None):
        print("Error: --results is required (or infer it with --infer-from-path)",
              file=sys.stderr)
        sys.exit(1)

    if not getattr(args, "analysis", None):
        print("Error: --analysis is required (or infer it with --infer-from-path)",
              file=sys.stderr)
        sys.exit(1)

    if getattr(args, "project_dir", None):
        return cmd_append_project(args)
    return cmd_append_flat(args)


def _apply_path_inference(args) -> None:
    """Populate ``args`` in place from the path-inference result.

    The CLI flag ``--infer-from-path`` may come in as ``""`` (bare flag, use
    ``$PWD``), an absolute path, or a relative path. Explicit values on
    ``args`` (e.g. ``--level assay``) always win over inferred ones.
    """
    from casetrack_qc.path_infer import (
        InferenceError,
        find_project_root,
        infer_from_path,
    )

    raw = args.infer_from_path
    start = Path(raw) if raw else Path.cwd()
    try:
        project_dir = find_project_root(start)
    except InferenceError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        schema = load_schema(project_dir / PROJECT_TOML_NAME)
    except SchemaError as e:
        print(f"Error: invalid schema in {project_dir / PROJECT_TOML_NAME}: {e}",
              file=sys.stderr)
        sys.exit(1)

    try:
        inferred = infer_from_path(project_dir, start, schema)
    except InferenceError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Fill args that the user didn't supply explicitly.
    if not getattr(args, "project_dir", None):
        args.project_dir = str(inferred["project_dir"])
    if not getattr(args, "level", None):
        args.level = inferred["level"]
    if not getattr(args, "analysis", None):
        args.analysis = inferred["tool"]
    if not getattr(args, "column_prefix", None) and inferred.get("column_prefix"):
        args.column_prefix = inferred["column_prefix"]
    if not getattr(args, "results", None):
        summary = inferred.get("summary_tsv") or "summary.tsv"
        candidate = inferred["leaf_dir"] / summary
        if not candidate.exists():
            print(
                f"Error: expected summary TSV not found: {candidate}\n"
                f"  [analyses.{inferred['tool']}].summary_tsv = {summary!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        args.results = str(candidate)

    # Stash run_tag so cmd_append_project can inject it as a column.
    args._inferred_run_tag = inferred["run_tag"]


def cmd_append_flat(args):
    """Append analysis results as new columns to the manifest."""
    _warn_flat_deprecation()
    manifest_path = args.manifest
    results_path = args.results
    key_col = args.key
    analysis = args.analysis

    if not os.path.exists(manifest_path):
        print(f"Error: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(results_path):
        print(f"Error: results file not found: {results_path}", file=sys.stderr)
        sys.exit(1)

    # Read results
    results = pd.read_csv(results_path, sep="\t")
    if key_col not in results.columns:
        print(f"Error: key column '{key_col}' not in results file. Columns: {list(results.columns)}", file=sys.stderr)
        sys.exit(1)

    # Determine new columns (everything except the key)
    new_cols = [c for c in results.columns if c != key_col]
    if not new_cols:
        print("Error: results file has no columns besides the key.", file=sys.stderr)
        sys.exit(1)

    # Add a _done timestamp column if not already present
    done_col = f"{analysis}{DONE_COLUMN_SUFFIX}"
    if done_col not in results.columns:
        results[done_col] = datetime.datetime.now().strftime("%Y-%m-%d")
        new_cols.append(done_col)

    # Lock and merge
    with ManifestLock(manifest_path):
        manifest = pd.read_csv(manifest_path, sep="\t")

        if key_col not in manifest.columns:
            print(f"Error: key column '{key_col}' not in manifest. Columns: {list(manifest.columns)}", file=sys.stderr)
            sys.exit(1)

        # Check for unrecognized sample IDs
        result_keys = set(results[key_col].astype(str))
        manifest_keys = set(manifest[key_col].astype(str))
        unknown = result_keys - manifest_keys
        if unknown and not args.allow_new:
            print(
                f"Warning: {len(unknown)} sample(s) in results not in manifest: {sorted(unknown)[:5]}...\n"
                f"Use --allow-new --yes to add them as new rows.",
                file=sys.stderr,
            )
        elif unknown and args.allow_new and not args.yes:
            preview = sorted(unknown)[:20]
            suffix = (f"\n  ... and {len(unknown) - 20} more"
                      if len(unknown) > 20 else "")
            print(
                f"Refusing to add {len(unknown)} new sample(s) without --yes.\n"
                f"New IDs that would be added:\n  "
                + "\n  ".join(preview) + suffix + "\n"
                f"Re-run with --allow-new --yes to commit.",
                file=sys.stderr,
            )
            sys.exit(2)
        elif unknown and args.allow_new and args.yes:
            preview = sorted(unknown)[:5]
            more = f" (+ {len(unknown) - 5} more)" if len(unknown) > 5 else ""
            print(
                f"Adding {len(unknown)} new sample(s): "
                f"{', '.join(preview)}{more}",
                file=sys.stderr,
            )

        # Check for column collisions
        existing_cols = set(manifest.columns)
        collisions = [c for c in new_cols if c in existing_cols]
        if collisions:
            # Smart merge: if the columns already exist, update only the NaN cells
            # This is the common case for per-sample SLURM array jobs where
            # the first job creates the columns and subsequent jobs fill in rows.
            if args.overwrite:
                manifest = manifest.drop(columns=collisions)
                print(f"Overwriting existing columns: {collisions}", file=sys.stderr)
                how = "outer" if args.allow_new else "left"
                merged = manifest.merge(results, on=key_col, how=how)
            else:
                # Update-in-place (vectorized): fill NaN cells with new values.
                merged = fill_nan_cells(manifest, results, key_col, collisions)
                # Add any truly new columns
                new_only = [c for c in new_cols if c not in existing_cols]
                if new_only:
                    results_new = results[[key_col] + new_only]
                    how = "outer" if args.allow_new else "left"
                    merged = merged.merge(results_new, on=key_col, how=how)
        else:
            # No collisions — simple merge
            how = "outer" if args.allow_new else "left"
            merged = manifest.merge(results, on=key_col, how=how)

        # Write back
        merged.to_csv(manifest_path, sep="\t", index=False)

    # Update schema and provenance
    update_schema(manifest_path, analysis, new_cols)

    samples_updated = len(result_keys & manifest_keys)
    log_provenance(manifest_path, {
        "action": "append",
        "analysis": analysis,
        "results_file": str(results_path),
        "results_checksum": _checksum(results_path),
        "columns_added": new_cols,
        "samples_updated": samples_updated,
        "samples_new": len(unknown) if args.allow_new else 0,
    })

    print(f"Appended {len(new_cols)} columns from '{analysis}' for {samples_updated} samples.")


def cmd_status(args):
    """Dispatch `casetrack status` to flat or project mode."""
    if getattr(args, "project_dir", None):
        return cmd_status_project(args)
    return cmd_status_flat(args)


def cmd_status_flat(args):
    """Show completion status across analyses."""
    _warn_flat_deprecation()
    manifest_path = args.manifest

    if not os.path.exists(manifest_path):
        print(f"Error: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = pd.read_csv(manifest_path, sep="\t")

    # Find all _done columns
    done_cols = [c for c in manifest.columns if c.endswith(DONE_COLUMN_SUFFIX)]

    if args.analysis:
        target = f"{args.analysis}{DONE_COLUMN_SUFFIX}"
        if target in done_cols:
            done_cols = [target]
        else:
            print(f"No _done column found for analysis '{args.analysis}'.", file=sys.stderr)
            sys.exit(1)

    key_col = args.key
    if key_col not in manifest.columns:
        # Try to guess the key column (first column)
        key_col = manifest.columns[0]

    n_samples = len(manifest)

    if args.fmt == "json":
        status = {}
        for dc in done_cols:
            analysis_name = dc.replace(DONE_COLUMN_SUFFIX, "")
            completed = int(manifest[dc].notna().sum())
            status[analysis_name] = {
                "completed": completed,
                "total": n_samples,
                "pct": round(100 * completed / n_samples, 1) if n_samples > 0 else 0,
                "missing": sorted(manifest.loc[manifest[dc].isna(), key_col].tolist()),
            }
        print(json.dumps(status, indent=2, default=str))
    elif args.fmt == "tsv":
        print("analysis\tcompleted\ttotal\tpct")
        for dc in done_cols:
            analysis_name = dc.replace(DONE_COLUMN_SUFFIX, "")
            completed = int(manifest[dc].notna().sum())
            pct = round(100 * completed / n_samples, 1) if n_samples > 0 else 0
            print(f"{analysis_name}\t{completed}\t{n_samples}\t{pct}")
    else:
        # Table format
        print(f"\nManifest: {manifest_path}")
        print(f"Samples:  {n_samples}")
        print(f"Columns:  {len(manifest.columns)}")
        print(f"{'─' * 55}")
        print(f"{'Analysis':<30} {'Done':>6} {'Total':>6} {'%':>7}")
        print(f"{'─' * 55}")
        for dc in done_cols:
            analysis_name = dc.replace(DONE_COLUMN_SUFFIX, "")
            completed = int(manifest[dc].notna().sum())
            pct = round(100 * completed / n_samples, 1) if n_samples > 0 else 0
            bar_len = 10
            filled = int(bar_len * pct / 100)
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"{analysis_name:<30} {completed:>6} {n_samples:>6} {pct:>6.1f}% {bar}")
        print(f"{'─' * 55}")

        # Show incomplete samples if few
        for dc in done_cols:
            missing = manifest.loc[manifest[dc].isna(), key_col].tolist()
            if 0 < len(missing) <= 5:
                analysis_name = dc.replace(DONE_COLUMN_SUFFIX, "")
                print(f"\n  Missing for {analysis_name}: {', '.join(str(s) for s in missing)}")


def cmd_validate(args):
    """Dispatch `casetrack validate` to flat or project mode."""
    if getattr(args, "project_dir", None):
        return cmd_validate_project(args)
    return cmd_validate_flat(args)


def cmd_validate_flat(args):
    """Validate manifest integrity."""
    _warn_flat_deprecation()
    manifest_path = args.manifest
    key_col = args.key

    if not os.path.exists(manifest_path):
        print(f"Error: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = pd.read_csv(manifest_path, sep="\t")
    issues = []

    # Check key column exists
    if key_col not in manifest.columns:
        issues.append(f"Key column '{key_col}' not found. Columns: {list(manifest.columns)}")
    else:
        # Check for duplicate keys
        dupes = manifest[manifest[key_col].duplicated(keep=False)]
        if not dupes.empty:
            dupe_ids = sorted(dupes[key_col].unique().tolist())
            issues.append(f"Duplicate sample IDs: {dupe_ids[:10]}")

        # Check for null keys
        null_keys = manifest[key_col].isna().sum()
        if null_keys > 0:
            issues.append(f"{null_keys} rows with null sample IDs")

    # Check for completely empty columns
    empty_cols = [c for c in manifest.columns if manifest[c].isna().all()]
    if empty_cols:
        issues.append(f"Completely empty columns: {empty_cols}")

    # Check schema consistency and _done column integrity
    schema_path = manifest_path + SCHEMA_SUFFIX
    schema = {}
    if os.path.exists(schema_path):
        with open(schema_path) as f:
            schema = json.load(f)
        for analysis, info in schema.items():
            for col in info.get("columns", []):
                if col not in manifest.columns:
                    issues.append(f"Schema says '{col}' should exist (from '{analysis}') but it's missing")

    done_cols = [c for c in manifest.columns if c.endswith(DONE_COLUMN_SUFFIX)]
    for dc in done_cols:
        analysis_name = dc.replace(DONE_COLUMN_SUFFIX, "")
        # Use schema to find related columns if available
        if analysis_name in schema:
            data_cols = [c for c in schema[analysis_name].get("columns", []) if c != dc]
            if not data_cols:
                issues.append(f"Done column '{dc}' has no data columns in schema")
        else:
            # Fallback: any non-key, non-done column with same prefix
            related = [c for c in manifest.columns if c.startswith(analysis_name) and c != dc]
            if not related:
                issues.append(f"Done column '{dc}' has no corresponding data columns and no schema entry")

    if issues:
        print(f"Validation found {len(issues)} issue(s):", file=sys.stderr)
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Manifest OK: {len(manifest)} samples, {len(manifest.columns)} columns, no issues.")


def cmd_log(args):
    """Dispatch `casetrack log` to flat or project mode."""
    if getattr(args, "project_dir", None):
        return cmd_log_project(args)
    return cmd_log_flat(args)


def cmd_log_flat(args):
    """Show provenance log entries."""
    _warn_flat_deprecation()
    manifest_path = args.manifest
    log_path = manifest_path + PROVENANCE_SUFFIX

    if not os.path.exists(log_path):
        print("No provenance log found.", file=sys.stderr)
        sys.exit(1)

    with open(log_path) as f:
        lines = f.readlines()

    if args.last:
        lines = lines[-args.last:]

    for line in lines:
        entry = json.loads(line.strip())
        ts = entry.get("timestamp", "?")
        action = entry.get("action", "?")
        user = entry.get("user", "?")
        job = entry.get("slurm_job_id", "")
        analysis = entry.get("analysis", "")

        job_str = f" [SLURM {job}]" if job else ""
        analysis_str = f" ({analysis})" if analysis else ""

        if action == "init":
            n = entry.get("n_samples", "?")
            print(f"  {ts}  INIT  {user}{job_str} — {n} samples")
        elif action == "append":
            cols = entry.get("columns_added", [])
            n_updated = entry.get("samples_updated", "?")
            print(f"  {ts}  APPEND{analysis_str}  {user}{job_str} — {len(cols)} cols, {n_updated} samples")
        else:
            print(f"  {ts}  {action.upper()}  {user}{job_str}{analysis_str}")


def cmd_schema(args):
    """Dispatch `casetrack schema` to flat or project mode."""
    if getattr(args, "project_dir", None):
        return cmd_schema_project(args)
    return cmd_schema_flat(args)


def cmd_schema_flat(args):
    """Show which analysis added which columns."""
    _warn_flat_deprecation()
    manifest_path = args.manifest
    schema_path = manifest_path + SCHEMA_SUFFIX

    if not os.path.exists(schema_path):
        print("No schema file found. Run 'casetrack append' to build one.", file=sys.stderr)
        sys.exit(1)

    with open(schema_path) as f:
        schema = json.load(f)

    if args.fmt == "json":
        print(json.dumps(schema, indent=2))
    else:
        print(f"\n{'Analysis':<25} {'Columns':<40} {'Added by':<12} {'Date'}")
        print(f"{'─' * 90}")
        for analysis, info in schema.items():
            cols = ", ".join(info.get("columns", []))
            added_by = info.get("added_by", "?")
            added = info.get("added", "?")
            print(f"{analysis:<25} {cols:<40} {added_by:<12} {added}")


def _find_project_manifests(root: Path, pattern: str, max_depth: int) -> list:
    """Yield manifest paths under `root` up to `max_depth` directories deep.

    Hidden directories (dot-prefixed) and any directory literally named
    "sandbox" are skipped.
    """
    root = root.resolve()
    matches = []
    for p in root.rglob(pattern):
        if not p.is_file():
            continue
        try:
            rel_parts = p.resolve().relative_to(root).parts
        except ValueError:
            continue
        # Depth = number of path parts above the file itself.
        depth = len(rel_parts) - 1
        if depth > max_depth:
            continue
        # Skip hidden + sandbox anywhere along the path (but not in the filename).
        if any(part.startswith(".") or part == "sandbox" for part in rel_parts[:-1]):
            continue
        matches.append(p)
    return matches


def _summarize_project(manifest_path: Path, key_col: str) -> dict:
    """Compute the cross-project stats for a single manifest."""
    df = pd.read_csv(manifest_path, sep="\t")
    n_samples = len(df)
    done_cols = [c for c in df.columns if c.endswith(DONE_COLUMN_SUFFIX)]
    total_cells = n_samples * len(done_cols)
    completed = int(sum(df[dc].notna().sum() for dc in done_cols))
    pct = round(100.0 * completed / total_cells, 1) if total_cells else 0.0
    return {
        "kind": "v0.2",
        "name": manifest_path.parent.name,
        "path": str(manifest_path),
        "samples": n_samples,
        "analyses": len(done_cols),
        "completed_cells": completed,
        "total_cells": total_cells,
        "pct": pct,
    }


def cmd_projects(args):
    """Dispatch `casetrack projects <action>` to the right handler.

    Subactions: scan (v0.5 filesystem walk) | list / register / deregister
    (v0.6 registry operations). Defaults to printing help when no
    subaction is given.

    Backward compat: a v0.5 `casetrack projects --root <path>` invocation
    (no subaction, but with `--root` set) is silently routed to `scan`.
    """
    action = getattr(args, "projects_action", None)
    if action is None and getattr(args, "root", None) is not None:
        action = "scan"
    if action == "scan":
        return cmd_projects_scan(args)
    if action == "list":
        return cmd_projects_list(args)
    if action == "register":
        return cmd_projects_register_in_registry(args)
    if action == "deregister":
        return cmd_projects_deregister_from_registry(args)
    print(
        "Error: `casetrack projects` requires a subaction.\n"
        "  casetrack projects list                       — list registered projects\n"
        "  casetrack projects register --project-dir ... — add to registry\n"
        "  casetrack projects deregister <project_id>    — remove from registry\n"
        "  casetrack projects scan --root <path>         — walk a filesystem tree",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_projects_list(args):
    """`casetrack projects list [--fmt table|tsv|json] [--status ...]` — read registry."""
    fmt = getattr(args, "fmt", None) or "table"
    status_filter_raw = getattr(args, "status", "active") or "active"

    from casetrack_lifecycle.schema import get_status as _get_lifecycle_status

    reg = _registry_load()
    all_entries = sorted(
        (
            {"project_id": pid, **info}
            for pid, info in reg.get("projects", {}).items()
        ),
        key=lambda e: e["project_id"],
    )

    # Enrich each entry with its lifecycle status from the project DB.
    for e in all_entries:
        path = e.get("path")
        db_path = Path(path) / PROJECT_DB_NAME if path else None
        if db_path and db_path.exists():
            try:
                _conn = open_project_db(db_path)
                e["status"] = _get_lifecycle_status(_conn)
                _conn.close()
            except Exception:
                e["status"] = "active"
        else:
            e["status"] = "active"

    # Apply status filter.
    if status_filter_raw == "all":
        entries = all_entries
    else:
        wanted = {s.strip() for s in status_filter_raw.split(",")}
        entries = [e for e in all_entries if e.get("status", "active") in wanted]

    if fmt == "json":
        print(json.dumps({
            "registry": str(_registry_path()),
            "schema_v": reg.get("schema_v"),
            "status_filter": status_filter_raw,
            "projects": entries,
        }, indent=2, default=str))
        return

    if fmt == "tsv":
        print("project_id\tname\tstatus\tpath\tlast_seen")
        for e in entries:
            print(
                f"{e['project_id']}\t{e.get('name','')}\t{e.get('status','active')}\t"
                f"{e.get('path','')}\t{e.get('last_seen','')}"
            )
        return

    # table
    if not all_entries:
        print(
            f"No projects registered in {_registry_path()}.\n"
            "  Run `casetrack init --project-dir <path>` to create one,\n"
            "  or `casetrack projects register --project-dir <path>` to add an existing project."
        )
        return
    if not entries:
        print(
            f"No projects with status={status_filter_raw!r}. "
            f"Use --status all to see all {len(all_entries)} project(s)."
        )
        return
    pid_w   = max(10, max(len(e["project_id"]) for e in entries))
    name_w  = max(8, min(40, max(len(e.get("name", "")) for e in entries)))
    stat_w  = 8
    print(f"{'project_id':<{pid_w}}  {'name':<{name_w}}  {'status':<{stat_w}}  last_seen          path")
    print("─" * (pid_w + name_w + stat_w + 65))
    for e in entries:
        name = e.get("name", "")[:name_w]
        print(
            f"{e['project_id']:<{pid_w}}  {name:<{name_w}}  "
            f"{e.get('status','active'):<{stat_w}}  "
            f"{e.get('last_seen', ''):<19}  {e.get('path', '')}"
        )
    status_note = "" if status_filter_raw == "all" else f" (status={status_filter_raw!r})"
    print(f"\n{len(entries)} project(s){status_note} in {_registry_path()}")


def cmd_projects_register_in_registry(args):
    """`casetrack projects register --project-dir <path>` — add to registry.

    Reads project_id from the project's project_meta row (or TOML if no DB
    yet). Refuses to register a project_id that's already mapped to a
    different path; pass `casetrack projects deregister <id>` first.
    """
    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"Error: project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)
    toml_path = project_dir / PROJECT_TOML_NAME
    if not toml_path.exists():
        print(
            f"Error: {PROJECT_TOML_NAME} not found in {project_dir}",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        schema = load_schema(toml_path)
    except SchemaError as e:
        print(f"Error: invalid schema in {toml_path}: {e}", file=sys.stderr)
        sys.exit(1)

    project_id = schema.get("project", {}).get("project_id")
    project_name = schema.get("project", {}).get("name") or project_dir.name

    # Fall back to project_meta row if TOML lacks project_id (legacy or
    # hand-edited).
    if not project_id:
        db_path = project_dir / PROJECT_DB_NAME
        if db_path.exists():
            conn = open_project_db(db_path)
            try:
                meta = read_project_meta(conn)
                if meta:
                    project_id = meta["project_id"]
                    project_name = meta.get("name") or project_name
            finally:
                conn.close()

    if not project_id:
        print(
            f"Error: {project_dir} has no project_id (in TOML or project_meta). "
            f"Run `casetrack init --force --project-id <slug> --project-dir "
            f"{project_dir}` to assign one.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        registry_register(project_id, project_dir, project_name)
    except (OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(
        f"Registered {project_id!r} → {project_dir} in {_registry_path()}"
    )


def cmd_projects_deregister_from_registry(args):
    """`casetrack projects deregister <project_id>` — remove a registry entry."""
    pid = args.project_id
    try:
        validate_project_id(pid)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        removed = registry_deregister(pid)
    except (OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if removed:
        print(f"Deregistered {pid!r} from {_registry_path()}")
    else:
        print(
            f"No entry for {pid!r} in {_registry_path()} — nothing to do.",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_projects_scan(args):
    """Scan `--root` for projects (both v0.2 flat manifests and v0.3
    casetrack.toml projects) and summarize cross-project status."""
    root = Path(args.root)
    if not root.exists() or not root.is_dir():
        print(f"Error: root not found or not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    projects: list[dict] = []

    # v0.3 projects: casetrack.toml with sibling casetrack.db.
    for toml_path in _find_v03_projects(root, args.max_depth):
        try:
            projects.append(_summarize_v03_project(toml_path))
        except Exception as e:  # noqa: BLE001
            print(
                f"Error: failed to summarize {toml_path}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            sys.exit(1)

    # v0.2 flat manifests — skip any dir that already reports as v0.3 so we
    # don't double-count the sandbox/source_manifest.tsv left by `migrate`.
    v03_dirs = {Path(p["path"]).parent.resolve() for p in projects}
    manifests = _find_project_manifests(root, args.pattern, args.max_depth)
    for mpath in manifests:
        if any(mpath.resolve().is_relative_to(d) for d in v03_dirs):
            continue
        try:
            projects.append(_summarize_project(mpath, args.key))
        except Exception as e:  # noqa: BLE001 — surface the offending path before exiting.
            print(
                f"Error: failed to summarize {mpath}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            sys.exit(1)

    projects.sort(key=lambda p: p["name"])

    if not projects:
        print(
            f"No manifests found under {root} "
            f"(pattern={args.pattern!r}, max-depth={args.max_depth})."
        )
        return

    if args.fmt == "json":
        print(json.dumps(projects, indent=2, default=str))
        return

    if args.fmt == "tsv":
        print("project\tkind\tpath\tsamples\tanalyses\tcompleted_cells\ttotal_cells\tpct")
        for p in projects:
            print(
                f"{p['name']}\t{p.get('kind','v0.2')}\t{p['path']}\t{p['samples']}\t"
                f"{p['analyses']}\t{p['completed_cells']}\t{p['total_cells']}\t{p['pct']}"
            )
        return

    # table
    name_w = max(12, max(len(p["name"]) for p in projects))
    header = (
        f"{'Project':<{name_w}}  {'Kind':<5}  {'Samples':>7}  "
        f"{'Analyses':>8}  {'Complete':>9}"
    )
    sep = "─" * (name_w + 40)
    print("")
    print(header)
    print(sep)
    for p in projects:
        kind = p.get("kind", "v0.2")
        print(
            f"{p['name']:<{name_w}}  {kind:<5}  {p['samples']:>7}  "
            f"{p['analyses']:>8}  {p['pct']:>7.1f}% "
            + ("█" * int(p["pct"] / 10) + "░" * (10 - int(p["pct"] / 10)))
        )
    print(sep)
    print(f"{len(projects)} project(s) under {root}")


def cmd_add_metadata(args):
    """Dispatch `casetrack add-metadata` to flat or project mode."""
    if getattr(args, "project_dir", None):
        return cmd_add_metadata_project(args)
    return cmd_add_metadata_flat(args)


def cmd_add_metadata_flat(args):
    """Attach metadata columns to an existing manifest without the analysis
    append path: no `_done` timestamp, no schema entry.

    Default collision policy is strict — refuse to touch existing columns.
    Use `--fill-only` to fill NaN cells (smart merge) or `--overwrite` to
    replace existing columns wholesale.
    """
    _warn_flat_deprecation()
    manifest_path = args.manifest
    metadata_path = args.metadata
    key_col = args.key

    if not os.path.exists(manifest_path):
        print(f"Error: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(metadata_path):
        print(f"Error: metadata file not found: {metadata_path}", file=sys.stderr)
        sys.exit(1)
    if args.overwrite and args.fill_only:
        print("Error: --overwrite and --fill-only are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    metadata = pd.read_csv(metadata_path, sep="\t")
    if key_col not in metadata.columns:
        print(
            f"Error: key column '{key_col}' not in metadata file. "
            f"Columns: {list(metadata.columns)}",
            file=sys.stderr,
        )
        sys.exit(1)

    new_cols = [c for c in metadata.columns if c != key_col]
    if not new_cols:
        print("Error: metadata file has no columns besides the key.", file=sys.stderr)
        sys.exit(1)

    with ManifestLock(manifest_path):
        manifest = pd.read_csv(manifest_path, sep="\t")
        if key_col not in manifest.columns:
            print(
                f"Error: key column '{key_col}' not in manifest. "
                f"Columns: {list(manifest.columns)}",
                file=sys.stderr,
            )
            sys.exit(1)

        metadata_keys = set(metadata[key_col].astype(str))
        manifest_keys = set(manifest[key_col].astype(str))
        unknown = metadata_keys - manifest_keys
        if unknown and not args.allow_new:
            print(
                f"Warning: {len(unknown)} sample(s) in metadata not in manifest: "
                f"{sorted(unknown)[:5]}...\n"
                f"Use --allow-new --yes to add them as new rows.",
                file=sys.stderr,
            )
        elif unknown and args.allow_new and not args.yes:
            preview = sorted(unknown)[:20]
            suffix = (f"\n  ... and {len(unknown) - 20} more"
                      if len(unknown) > 20 else "")
            print(
                f"Refusing to add {len(unknown)} new sample(s) without --yes.\n"
                f"New IDs that would be added:\n  "
                + "\n  ".join(preview) + suffix + "\n"
                f"Re-run with --allow-new --yes to commit.",
                file=sys.stderr,
            )
            sys.exit(2)
        elif unknown and args.allow_new and args.yes:
            preview = sorted(unknown)[:5]
            more = f" (+ {len(unknown) - 5} more)" if len(unknown) > 5 else ""
            print(
                f"Adding {len(unknown)} new sample(s): "
                f"{', '.join(preview)}{more}",
                file=sys.stderr,
            )

        existing_cols = set(manifest.columns)
        collisions = [c for c in new_cols if c in existing_cols]

        if collisions and not (args.overwrite or args.fill_only):
            print(
                f"Error: {len(collisions)} column(s) already present in manifest: "
                f"{collisions}\n"
                f"Re-run with --fill-only to fill NaN cells, or --overwrite to "
                f"replace existing values.",
                file=sys.stderr,
            )
            sys.exit(1)

        if collisions and args.overwrite:
            manifest = manifest.drop(columns=collisions)
            print(f"Overwriting existing columns: {collisions}", file=sys.stderr)
            how = "outer" if args.allow_new else "left"
            merged = manifest.merge(metadata, on=key_col, how=how)
        elif collisions and args.fill_only:
            merged = fill_nan_cells(manifest, metadata, key_col, collisions)
            new_only = [c for c in new_cols if c not in existing_cols]
            if new_only:
                metadata_new = metadata[[key_col] + new_only]
                how = "outer" if args.allow_new else "left"
                merged = merged.merge(metadata_new, on=key_col, how=how)
        else:
            how = "outer" if args.allow_new else "left"
            merged = manifest.merge(metadata, on=key_col, how=how)

        merged.to_csv(manifest_path, sep="\t", index=False)

    samples_updated = len(metadata_keys & manifest_keys)
    log_provenance(manifest_path, {
        "action": "add-metadata",
        "metadata_file": str(metadata_path),
        "metadata_checksum": _checksum(metadata_path),
        "columns_added": new_cols,
        "collisions": collisions,
        "collision_policy": (
            "overwrite" if args.overwrite
            else ("fill-only" if args.fill_only else "none")
        ),
        "samples_updated": samples_updated,
        "samples_new": len(unknown) if args.allow_new else 0,
    })

    print(
        f"Added {len(new_cols)} metadata column(s) for {samples_updated} "
        f"sample(s)."
    )


def _sql_escape(s: str) -> str:
    """Escape single quotes for literal use inside a DuckDB SQL string."""
    return str(s).replace("'", "''")


def cmd_query(args):
    """Run a SQL query against a v0.3 project (--project-dir), one flat
    manifest (--manifest), or a union of many (--root) via DuckDB.

    Project mode: casetrack.db is ATTACH-ed read-only as `proj`. Views on
    `patients`, `specimens`, `assays`, and `_` (the assays⋈specimens⋈
    patients join) live in the default in-memory catalog so queries read
    naturally.
    """
    if getattr(args, "project_dir", None):
        return cmd_query_project(args)
    if getattr(args, "manifest", None):
        _warn_flat_deprecation()
    try:
        import duckdb as _duckdb
    except ImportError:
        print(
            "Error: duckdb is missing but is a required dependency of casetrack.\n"
            "Your install is broken — reinstall with: pip install --force-reinstall casetrack\n"
            "(or install duckdb directly: pip install duckdb)",
            file=sys.stderr,
        )
        sys.exit(1)

    sql = args.sql
    alias = args.as_name or "_"

    if bool(args.manifest) == bool(args.root):
        print(
            "Error: exactly one of --manifest or --root is required.",
            file=sys.stderr,
        )
        sys.exit(1)

    con = _duckdb.connect(":memory:")

    if args.manifest:
        if not os.path.exists(args.manifest):
            print(f"Error: manifest not found: {args.manifest}", file=sys.stderr)
            sys.exit(1)
        mpath = str(Path(args.manifest).resolve())
        con.execute(
            f"CREATE VIEW {alias} AS "
            f"SELECT * FROM read_csv('{_sql_escape(mpath)}', "
            f"delim='\t', header=true, sample_size=-1)"
        )
    else:
        root = Path(args.root)
        if not root.exists() or not root.is_dir():
            print(
                f"Error: root not found or not a directory: {root}",
                file=sys.stderr,
            )
            sys.exit(1)
        manifests = _find_project_manifests(root, args.pattern, args.max_depth)
        if not manifests:
            print(
                f"No manifests found under {root} "
                f"(pattern={args.pattern!r}, max-depth={args.max_depth}).",
                file=sys.stderr,
            )
            sys.exit(1)

        parts = []
        for m in manifests:
            project = _sql_escape(m.parent.name)
            mpath = _sql_escape(str(m.resolve()))
            parts.append(
                f"SELECT '{project}' AS project, * FROM read_csv("
                f"'{mpath}', delim='\t', header=true, sample_size=-1)"
            )
        view_sql = "\nUNION ALL BY NAME\n".join(parts)
        con.execute(f"CREATE VIEW {alias} AS {view_sql}")

    try:
        rel = con.sql(sql)
    except _duckdb.Error as e:
        print(f"Error: SQL failed: {e}", file=sys.stderr)
        sys.exit(2)

    df = rel.df()

    out_stream = sys.stdout
    close_after = False
    if args.output:
        out_stream = open(args.output, "w", encoding="utf-8")
        close_after = True

    try:
        if args.fmt == "json":
            out_stream.write(df.to_json(orient="records", indent=2, date_format="iso") or "")
            out_stream.write("\n")
        elif args.fmt == "tsv":
            df.to_csv(out_stream, sep="\t", index=False)
        elif args.fmt == "csv":
            df.to_csv(out_stream, index=False)
        else:  # table
            # DuckDB relations render themselves as a pretty table; grab that
            # via the relation before it goes out of scope — but since we've
            # already materialized df, use tabulate-style from pandas instead.
            out_stream.write(df.to_string(index=False))
            out_stream.write("\n")
            out_stream.write(f"({len(df)} row{'s' if len(df) != 1 else ''})\n")
    finally:
        if close_after:
            out_stream.close()


def _render_dashboard_html(manifest, key_col: str, done_cols: list,
                           prov_entries: list, schema: dict,
                           manifest_path: str, prov_limit: int = 100) -> str:
    """Build a self-contained HTML dashboard. Returns the full HTML string.

    No external resources are referenced — all CSS is inline and no JavaScript
    libraries are loaded. Safe to scp to a laptop and open offline.
    """
    esc = html.escape
    n_samples = len(manifest)
    n_cols = len(manifest.columns)

    analyses = [c[: -len(DONE_COLUMN_SUFFIX)] for c in done_cols]
    total_cells = n_samples * len(done_cols)
    completed_cells = int(sum(manifest[dc].notna().sum() for dc in done_cols))
    overall_pct = (100.0 * completed_cells / total_cells) if total_cells else 0.0

    per_analysis = []
    for analysis, dc in zip(analyses, done_cols):
        completed = int(manifest[dc].notna().sum())
        missing = manifest.loc[manifest[dc].isna(), key_col].astype(str).tolist()
        pct = (100.0 * completed / n_samples) if n_samples else 0.0
        per_analysis.append({
            "name": analysis, "completed": completed, "total": n_samples,
            "pct": pct, "missing": missing,
        })

    # Heatmap: vectorized boolean matrix; rows = samples, cols = analyses.
    sample_ids = manifest[key_col].astype(str).tolist()
    if done_cols:
        done_matrix = manifest[done_cols].notna().to_numpy()
    else:
        done_matrix = None

    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"casetrack dashboard — {os.path.basename(manifest_path)}"

    # ── sections ──────────────────────────────────────────────────────────────
    head = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{esc(title)}</title>
<style>
  :root {{
    --done: #2f855a; --done-bg: #c6f6d5;
    --missing: #a0aec0; --missing-bg: #edf2f7;
    --fg: #1a202c; --muted: #4a5568; --border: #e2e8f0;
    --accent: #2b6cb0;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
         Roboto, Arial, sans-serif; color: var(--fg);
         margin: 0; padding: 24px; background: #fafbfc; }}
  h1 {{ margin: 0 0 4px 0; font-size: 22px; }}
  h2 {{ margin: 28px 0 12px 0; font-size: 16px;
        border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
  .muted {{ color: var(--muted); font-size: 12px; }}
  .metrics {{ display: flex; gap: 32px; margin: 16px 0 8px 0; flex-wrap: wrap; }}
  .metric .value {{ font-size: 24px; font-weight: 600; }}
  .metric .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase;
                   letter-spacing: 0.05em; }}
  .bar {{ background: var(--missing-bg); border-radius: 4px; overflow: hidden;
          height: 14px; flex: 1; }}
  .bar > div {{ background: var(--done); height: 100%; }}
  .analysis-row {{ display: flex; align-items: center; gap: 12px; margin: 6px 0; }}
  .analysis-row .name {{ width: 240px; font-family: ui-monospace, monospace;
                         font-size: 13px; overflow-wrap: anywhere; }}
  .analysis-row .pct {{ width: 70px; text-align: right; font-variant-numeric: tabular-nums; }}
  .analysis-row .count {{ width: 100px; text-align: right; color: var(--muted);
                          font-variant-numeric: tabular-nums; font-size: 12px; }}
  details {{ margin: 2px 0 10px 252px; }}
  details summary {{ cursor: pointer; color: var(--accent); font-size: 12px; }}
  details .missing {{ font-family: ui-monospace, monospace; font-size: 12px;
                      color: var(--muted); margin-top: 4px;
                      word-break: break-all; }}
  .heatmap {{ overflow: auto; border: 1px solid var(--border);
              border-radius: 4px; background: white; }}
  .heatmap table {{ border-collapse: collapse; font-size: 11px; }}
  .heatmap th, .heatmap td {{ padding: 0; text-align: center; }}
  .heatmap thead th {{ position: sticky; top: 0; background: #f7fafc;
                       border-bottom: 1px solid var(--border); padding: 6px 8px;
                       font-weight: 500; white-space: nowrap;
                       writing-mode: vertical-rl; transform: rotate(180deg);
                       height: 140px; vertical-align: bottom; }}
  .heatmap tbody th {{ position: sticky; left: 0; background: #f7fafc;
                       text-align: right; padding: 4px 10px;
                       font-family: ui-monospace, monospace; font-weight: 400;
                       border-right: 1px solid var(--border); white-space: nowrap; }}
  .heatmap td {{ width: 20px; height: 20px;
                 border-right: 1px solid #f0f1f3;
                 border-bottom: 1px solid #f0f1f3; }}
  .heatmap td.done    {{ background: var(--done); }}
  .heatmap td.missing {{ background: var(--missing-bg); }}
  .timeline {{ list-style: none; padding: 0; margin: 0;
               font-family: ui-monospace, monospace; font-size: 12px;
               border-left: 2px solid var(--border); padding-left: 16px; }}
  .timeline li {{ padding: 4px 0; color: var(--muted); }}
  .timeline li b {{ color: var(--fg); font-weight: 500; }}
  .footer {{ margin-top: 32px; color: var(--muted); font-size: 11px;
             border-top: 1px solid var(--border); padding-top: 12px; }}
</style></head><body>
<h1>{esc(title)}</h1>
<div class="muted">Generated {esc(generated_at)} · {esc(manifest_path)}</div>
"""

    # Summary metrics
    metrics = f"""
<div class="metrics">
  <div class="metric"><div class="value">{n_samples}</div><div class="label">Samples</div></div>
  <div class="metric"><div class="value">{n_cols}</div><div class="label">Columns</div></div>
  <div class="metric"><div class="value">{len(done_cols)}</div><div class="label">Analyses</div></div>
  <div class="metric"><div class="value">{overall_pct:.1f}%</div><div class="label">Overall complete</div></div>
</div>
"""

    # Per-analysis progress
    analysis_html = ['<h2>Analyses</h2>']
    if not per_analysis:
        analysis_html.append(
            '<div class="muted">No analyses recorded yet. '
            'Run <code>casetrack append</code> to populate this section.</div>'
        )
    for row in per_analysis:
        bar_width = f"{row['pct']:.1f}%"
        analysis_html.append(
            '<div class="analysis-row">'
            f'<div class="name">{esc(row["name"])}</div>'
            f'<div class="bar"><div style="width: {bar_width}"></div></div>'
            f'<div class="pct">{row["pct"]:.1f}%</div>'
            f'<div class="count">{row["completed"]}/{row["total"]}</div>'
            '</div>'
        )
        if row["missing"]:
            missing_str = ", ".join(esc(s) for s in row["missing"])
            analysis_html.append(
                f'<details><summary>{len(row["missing"])} missing</summary>'
                f'<div class="missing">{missing_str}</div></details>'
            )

    # Heatmap
    heatmap_html = ['<h2>Per-sample heatmap</h2>']
    if done_matrix is None or not sample_ids:
        heatmap_html.append('<div class="muted">Nothing to display.</div>')
    else:
        rows = ['<div class="heatmap"><table><thead><tr><th></th>']
        for a in analyses:
            rows.append(f'<th>{esc(a)}</th>')
        rows.append('</tr></thead><tbody>')
        for i, sid in enumerate(sample_ids):
            rows.append(f'<tr><th>{esc(sid)}</th>')
            for j, a in enumerate(analyses):
                done = bool(done_matrix[i, j])
                cls = "done" if done else "missing"
                status = "done" if done else "missing"
                rows.append(
                    f'<td class="{cls}" title="{esc(sid)} / {esc(a)}: {status}"></td>'
                )
            rows.append('</tr>')
        rows.append('</tbody></table></div>')
        heatmap_html.extend(rows)

    # Provenance timeline (reverse chronological, capped)
    timeline_html = ['<h2>Provenance timeline</h2>']
    if not prov_entries:
        timeline_html.append('<div class="muted">No provenance log found.</div>')
    else:
        shown = list(reversed(prov_entries))[:prov_limit]
        timeline_html.append('<ul class="timeline">')
        for entry in shown:
            ts = entry.get("timestamp", "?")
            action = (entry.get("action") or "?").upper()
            user = entry.get("user", "?")
            job = entry.get("slurm_job_id")
            analysis = entry.get("analysis", "")
            detail_parts = []
            if action == "APPEND":
                cols = entry.get("columns_added", []) or []
                n_upd = entry.get("samples_updated", "?")
                detail_parts.append(f"{len(cols)} cols, {n_upd} samples")
            elif action == "INIT":
                detail_parts.append(f"{entry.get('n_samples', '?')} samples")
            elif action == "RERUN":
                detail_parts.append(
                    f"{entry.get('n_submitted', 0)} submitted, {entry.get('n_failed', 0)} failed"
                )
            detail = " — " + "; ".join(esc(p) for p in detail_parts) if detail_parts else ""
            job_str = f" [SLURM {esc(str(job))}]" if job else ""
            analysis_str = f" <i>({esc(analysis)})</i>" if analysis else ""
            git = entry.get("git") or {}
            git_str = ""
            if git.get("commit"):
                short = git["commit"][:8]
                dirty_mark = "*" if git.get("dirty") else ""
                branch = git.get("branch") or ""
                branch_str = f"@{esc(branch)}" if branch and branch != "HEAD" else ""
                git_str = f" · <code>{esc(short)}{dirty_mark}{branch_str}</code>"
            timeline_html.append(
                f'<li>{esc(ts)} · <b>{esc(action)}</b>{analysis_str} · {esc(user)}'
                f'{job_str}{git_str}{detail}</li>'
            )
        if len(prov_entries) > prov_limit:
            timeline_html.append(
                f'<li class="muted">… {len(prov_entries) - prov_limit} older entries omitted.</li>'
            )
        timeline_html.append('</ul>')

    footer = (
        f'<div class="footer">casetrack dashboard · '
        f'manifest: {esc(manifest_path)} · '
        f'schema analyses: {esc(", ".join(schema.keys()) or "—")}</div>'
        '</body></html>'
    )

    return "".join([
        head, metrics,
        *analysis_html,
        *heatmap_html,
        *timeline_html,
        footer,
    ])


def cmd_dashboard(args):
    """Dispatch `casetrack dashboard` to flat or project mode."""
    if getattr(args, "project_dir", None):
        return cmd_dashboard_project(args)
    return cmd_dashboard_flat(args)


def cmd_dashboard_flat(args):
    """Generate a self-contained HTML dashboard from the manifest."""
    _warn_flat_deprecation()
    manifest_path = args.manifest
    output_path = args.output

    if not os.path.exists(manifest_path):
        print(f"Error: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = pd.read_csv(manifest_path, sep="\t")

    key_col = args.key
    if key_col not in manifest.columns:
        key_col = manifest.columns[0]

    done_cols = [c for c in manifest.columns if c.endswith(DONE_COLUMN_SUFFIX)]

    prov_entries = []
    prov_path = manifest_path + PROVENANCE_SUFFIX
    if os.path.exists(prov_path):
        with open(prov_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    prov_entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    schema = {}
    schema_path = manifest_path + SCHEMA_SUFFIX
    if os.path.exists(schema_path):
        try:
            with open(schema_path) as f:
                schema = json.load(f)
        except json.JSONDecodeError:
            pass

    html_str = _render_dashboard_html(
        manifest, key_col, done_cols, prov_entries, schema, manifest_path
    )

    with open(output_path, "w") as f:
        f.write(html_str)

    print(f"Dashboard written: {output_path} "
          f"({len(manifest)} samples, {len(done_cols)} analyses)")


def cmd_rerun(args):
    """Dispatch `casetrack rerun` to flat or project mode."""
    if getattr(args, "project_dir", None):
        return cmd_rerun_project(args)
    return cmd_rerun_flat(args)


def cmd_rerun_flat(args):
    """Generate (or submit) SLURM commands for samples missing a given analysis."""
    _warn_flat_deprecation()
    manifest_path = args.manifest
    analysis = args.analysis

    if not os.path.exists(manifest_path):
        print(f"Error: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = pd.read_csv(manifest_path, sep="\t")

    key_col = args.key
    if key_col not in manifest.columns:
        key_col = manifest.columns[0]

    done_col = f"{analysis}{DONE_COLUMN_SUFFIX}"
    if done_col not in manifest.columns:
        print(
            f"Note: no '{done_col}' column yet — treating all samples as incomplete.",
            file=sys.stderr,
        )
        incomplete = manifest[key_col].astype(str).tolist()
    else:
        incomplete = manifest.loc[manifest[done_col].isna(), key_col].astype(str).tolist()

    # Drop null/NA keys that would otherwise produce broken commands.
    incomplete = [s for s in incomplete if s and s.lower() != "nan"]

    if not incomplete:
        print(f"All {len(manifest)} sample(s) have '{analysis}' completed. Nothing to do.")
        return

    if args.list_only:
        for s in incomplete:
            print(s)
        return

    extra = args.extra.split() if args.extra else []
    manifest_abspath = os.path.abspath(manifest_path)

    commands = [
        ["sbatch", args.script, sid, manifest_abspath, *extra] for sid in incomplete
    ]

    if not args.submit:
        print(
            f"# {len(commands)} sample(s) incomplete for '{analysis}'. Review, "
            f"then re-run with --submit to dispatch.",
            file=sys.stderr,
        )
        for cmd in commands:
            print(" ".join(cmd))
        return

    import subprocess

    submitted = []
    failed = []
    for cmd in commands:
        sid = cmd[2]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except FileNotFoundError:
            print(
                "Error: 'sbatch' not found in PATH. Submit from a SLURM login node.",
                file=sys.stderr,
            )
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "").strip() or (e.stdout or "").strip()
            failed.append({"sample_id": sid, "stderr": err, "returncode": e.returncode})
            print(f"FAIL {sid}: {err}", file=sys.stderr)
            continue

        # SLURM sbatch prints "Submitted batch job <id>".
        out = (res.stdout or "").strip()
        job_id = out.split()[-1] if out else "?"
        submitted.append({"sample_id": sid, "job_id": job_id})
        print(f"Submitted {sid}: SLURM {job_id}")

    log_provenance(
        manifest_path,
        {
            "action": "rerun",
            "analysis": analysis,
            "script": args.script,
            "n_submitted": len(submitted),
            "n_failed": len(failed),
            "submitted": submitted,
            "failed": failed,
        },
    )

    if failed and not submitted:
        sys.exit(1)


def cmd_export(args):
    """Dispatch `casetrack export` to flat or project mode."""
    if getattr(args, "project_dir", None):
        return cmd_export_project(args)
    return cmd_export_flat(args)


def cmd_export_flat(args):
    """Export manifest to other formats."""
    _warn_flat_deprecation()
    manifest_path = args.manifest
    output_path = args.output

    if not os.path.exists(manifest_path):
        print(f"Error: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = pd.read_csv(manifest_path, sep="\t")
    ext = Path(output_path).suffix.lower()

    if ext == ".xlsx":
        try:
            manifest.to_excel(output_path, index=False, engine="openpyxl")
        except ImportError:
            print("Error: openpyxl required for Excel export. pip install openpyxl", file=sys.stderr)
            sys.exit(1)
    elif ext == ".csv":
        manifest.to_csv(output_path, index=False)
    elif ext == ".json":
        manifest.to_json(output_path, orient="records", indent=2)
    elif ext in (".parquet", ".pq"):
        try:
            manifest.to_parquet(output_path, index=False)
        except ImportError:
            print("Error: pyarrow required for Parquet export. pip install pyarrow", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Error: unsupported format '{ext}'. Use .xlsx, .csv, .json, or .parquet", file=sys.stderr)
        sys.exit(1)

    print(f"Exported {len(manifest)} samples to {output_path}")


# ── v0.3 Migration (flat TSV → SQLite project) ─────────────────────────────────
#
# Implements `casetrack migrate` per proposal 0001 §13.1. Flow:
#   1. Read flat TSV + required level-key columns.
#   2. Classify each non-key column to patient/specimen/assay.
#        - --metadata-map is an explicit override.
#        - Otherwise: column is assigned to the coarsest level at which its
#          value is constant within every group (ignoring NaN). Never
#          ambiguous by construction — every column lands exactly once.
#   3. Infer a SQLite column type from the pandas dtype.
#   4. Write casetrack.toml, init the DB, insert rows per level inside one
#      transaction. FK violations during insert abort with rollback.
#   5. Emit .migration_report.{tsv,md}; copy source TSV to sandbox/.


class MigrationError(RuntimeError):
    """Raised on migration pre-flight or routing failures."""


def _parse_metadata_map(spec: str | None) -> dict:
    """Parse 'patient:a,b;specimen:c,d' into {level: set(cols)}.

    Unknown levels raise MigrationError. Empty/None spec returns empty dict.
    """
    out = {level: set() for level in LEVEL_ORDER}
    if not spec:
        return out
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise MigrationError(
                f"--metadata-map: expected 'level:col1,col2', got {chunk!r}"
            )
        level, cols = chunk.split(":", 1)
        level = level.strip()
        if level not in LEVEL_ORDER:
            raise MigrationError(
                f"--metadata-map: unknown level {level!r}; must be one of {list(LEVEL_ORDER)}"
            )
        for col in cols.split(","):
            col = col.strip()
            if col:
                out[level].add(col)
    return out


def _classify_column(df: "pd.DataFrame", col: str, patient_col: str, specimen_col: str) -> tuple[str, str]:
    """Decide which level a column belongs to from constant-within-group analysis.

    Returns (level, reason). `reason` is a short string that goes into the
    audit report so the user can understand every routing decision.
    """
    if _is_constant_per_group(df, col, patient_col):
        return "patient", "constant within every patient"
    if _is_constant_per_group(df, col, specimen_col):
        return "specimen", "constant within every specimen"
    return "assay", "varies within specimens"


def _is_constant_per_group(df: "pd.DataFrame", col: str, group_col: str) -> bool:
    """True iff every group in `group_col` has ≤1 non-NaN unique value of `col`."""
    for _, group in df.groupby(group_col, dropna=False):
        if group[col].dropna().nunique() > 1:
            return False
    return True


def _infer_column_type(series: "pd.Series") -> str:
    """Map a pandas column dtype to one of {TEXT, INTEGER, REAL, BOOLEAN, DATE}."""
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(dtype):
        return "INTEGER"
    if pd.api.types.is_float_dtype(dtype):
        # Could be an int column with NaN. REAL is safest without re-parsing.
        return "REAL"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "DATE"
    return "TEXT"


def _render_migration_toml(
    project_name: str,
    patient_col: str,
    specimen_col: str,
    assay_col: str,
    classifications: dict,
    types: dict,
) -> str:
    """Render a schema TOML string from the migration plan."""
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    lines = [
        "[project]",
        f'name     = "{project_name}"',
        "schema_v = 1",
        f'created  = "{now}"',
        "",
    ]

    level_columns: dict[str, list[tuple[str, str]]] = {lv: [] for lv in LEVEL_ORDER}
    # Each level's key column is required + unique.
    level_columns["patient"].append((patient_col, "TEXT"))
    level_columns["specimen"].append((specimen_col, "TEXT"))
    level_columns["specimen"].append((patient_col, "TEXT"))  # FK
    level_columns["assay"].append((assay_col, "TEXT"))
    level_columns["assay"].append((specimen_col, "TEXT"))  # FK

    for col, level in classifications.items():
        level_columns[level].append((col, types[col]))

    keys = {"patient": patient_col, "specimen": specimen_col, "assay": assay_col}
    parents = {"patient": None, "specimen": "patient", "assay": "specimen"}
    parent_keys = {"patient": None, "specimen": patient_col, "assay": specimen_col}

    for level in LEVEL_ORDER:
        lines.append(f"[levels.{level}]")
        lines.append(f'key        = "{keys[level]}"')
        if parents[level]:
            lines.append(f'parent     = "{parents[level]}"')
            lines.append(f'parent_key = "{parent_keys[level]}"')
        lines.append("")
        lines.append(f"[levels.{level}.columns]")
        seen = set()
        for col, ctype in level_columns[level]:
            if col in seen:
                continue
            seen.add(col)
            props = [f'type = "{ctype}"']
            if col == keys[level]:
                props += ["required = true", "unique = true"]
            elif col == parent_keys[level]:
                props += ["required = true"]
            lines.append(f"{col} = {{ {', '.join(props)} }}")
        lines.append("")

    lines += [
        "[analysis_defaults]",
        'default_level = "assay"',
        "",
        "[engine]",
        "wal             = true",
        f"busy_timeout_ms = {SQLITE_BUSY_TIMEOUT_MS}",
        "",
    ]
    return "\n".join(lines)


def _insert_rows_by_level(
    conn: sqlite3.Connection,
    df: "pd.DataFrame",
    patient_col: str,
    specimen_col: str,
    assay_col: str,
    classifications: dict,
    schema: dict | None = None,
) -> dict:
    """Insert deduplicated rows into patients → specimens → assays.

    Returns {level: n_inserted}. Runs in the caller's transaction.

    When `schema` is provided (migrate path), validates every new hierarchy
    key value against the schema's format rules (proposal 0005 Part A).
    When `schema` is None (recover path), validation is skipped — recover
    must replay existing IDs exactly, including any that predate Part A.
    """
    counts = {"patient": 0, "specimen": 0, "assay": 0}

    # Group columns by their assigned level (keys/FKs implicit).
    level_cols = {lv: [] for lv in LEVEL_ORDER}
    for col, level in classifications.items():
        level_cols[level].append(col)

    # v0.6: validate every unique hierarchy key from the source TSV before
    # any INSERT runs. Surfaces the offending value + rule at the top of the
    # transaction so partial writes don't happen and the error message
    # points at the actual problem.
    if schema is not None:
        for level, col in (
            ("patient", patient_col),
            ("specimen", specimen_col),
            ("assay", assay_col),
        ):
            for value in df[col].dropna().unique():
                validate_hierarchy_id(value, schema, level)

    # Patients: one row per unique patient_id. Collapse metadata by taking the
    # first non-NaN value in each patient group.
    patient_rows = _dedupe_group(df, [patient_col], level_cols["patient"])
    _insert_rows(conn, "patients", patient_rows, [patient_col] + level_cols["patient"])
    counts["patient"] = len(patient_rows)

    # Specimens: one row per unique specimen_id, carrying its patient_id FK.
    specimen_rows = _dedupe_group(
        df, [specimen_col, patient_col], level_cols["specimen"]
    )
    _insert_rows(
        conn,
        "specimens",
        specimen_rows,
        [specimen_col, patient_col] + level_cols["specimen"],
    )
    counts["specimen"] = len(specimen_rows)

    # Assays: every row of the flat TSV is one assay row.
    assay_cols = [assay_col, specimen_col] + level_cols["assay"]
    assay_rows = df[assay_cols].where(pd.notnull(df[assay_cols]), None).to_dict(orient="records")
    _insert_rows(conn, "assays", assay_rows, assay_cols)
    counts["assay"] = len(assay_rows)

    return counts


def _dedupe_group(df: "pd.DataFrame", key_cols: list, value_cols: list) -> list[dict]:
    """For each unique combination of `key_cols`, take the first non-NaN of each `value_col`."""
    out = []
    for key_vals, group in df.groupby(key_cols, dropna=False):
        row = dict(zip(key_cols, key_vals if isinstance(key_vals, tuple) else (key_vals,)))
        for col in value_cols:
            non_na = group[col].dropna()
            row[col] = non_na.iloc[0] if not non_na.empty else None
        out.append(row)
    return out


def _insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict], cols: list) -> None:
    if not rows:
        return
    placeholders = ", ".join("?" * len(cols))
    quoted_cols = ", ".join(_quote_ident(c) for c in cols)
    sql = f"INSERT INTO {_quote_ident(table)} ({quoted_cols}) VALUES ({placeholders})"
    values = [tuple(_coerce_for_sqlite(r.get(c)) for c in cols) for r in rows]
    conn.executemany(sql, values)


def _coerce_for_sqlite(value):
    """pandas NaN → None; numpy bool/int/float → python scalar."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


def _write_migration_reports(
    out_dir: Path,
    source_path: Path,
    classifications: dict,
    types: dict,
    reasons: dict,
    counts: dict,
    patient_col: str,
    specimen_col: str,
    assay_col: str,
) -> None:
    """Emit .migration_report.tsv (machine-readable) and .migration_report.md."""
    tsv_path = out_dir / ".migration_report.tsv"
    md_path = out_dir / ".migration_report.md"

    rows = []
    for col, level in sorted(classifications.items()):
        rows.append({
            "column": col,
            "assigned_level": level,
            "sqlite_type": types[col],
            "reason": reasons[col],
        })
    pd.DataFrame(rows).to_csv(tsv_path, sep="\t", index=False)

    md_lines = [
        f"# Casetrack migration report — {datetime.datetime.now().strftime(TIMESTAMP_FMT)}",
        "",
        f"- **Source**: `{source_path}`",
        f"- **Level keys**: patient=`{patient_col}`, specimen=`{specimen_col}`, assay=`{assay_col}`",
        f"- **Rows inserted**: patients={counts['patient']}, "
        f"specimens={counts['specimen']}, assays={counts['assay']}",
        "",
        "## Column routing",
        "",
        "| column | level | type | reason |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        md_lines.append(
            f"| `{row['column']}` | {row['assigned_level']} | "
            f"{row['sqlite_type']} | {row['reason']} |"
        )
    md_lines.append("")
    md_lines.append(
        "> If a column was routed to the wrong level, re-run with "
        "`--metadata-map 'patient:colA;specimen:colB'` to override."
    )
    md_path.write_text("\n".join(md_lines) + "\n")


def _insert_project_id_into_toml(toml_path: Path, project_id: str) -> None:
    """In-place insert `project_id = "..."` directly under `[project]`.

    Mirrors the line-insert idiom used by `_write_qc_toml_block` and the
    test harness's `_set_toml_line` so we don't need a full TOML rewriter.
    """
    text = toml_path.read_text()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "[project]":
            # Skip past any existing project_id line first (idempotent).
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("project_id"):
                lines.pop(j)
            lines.insert(i + 1, f'project_id = "{project_id}"')
            toml_path.write_text("\n".join(lines) + "\n")
            return
    raise ValueError(f"{toml_path}: no [project] section to insert project_id into")


def _migrate_one_project(
    project_dir: Path,
    *,
    explicit_project_id: str | None = None,
    yes: bool = False,
    interactive: bool = True,
) -> dict:
    """Migrate a single project to the v0.6 identity scheme. Returns a result
    dict {project_dir, project_id, action} where action is one of
    'noop' | 'migrated' | 'skipped' (with reason in 'reason').

    Idempotent: if TOML and project_meta both already have a matching
    project_id and the registry is up to date, this is a no-op.
    """
    toml_path = project_dir / PROJECT_TOML_NAME
    db_path = project_dir / PROJECT_DB_NAME
    if not toml_path.exists():
        return {"project_dir": project_dir, "action": "skipped",
                "reason": "no casetrack.toml"}
    if not db_path.exists():
        return {"project_dir": project_dir, "action": "skipped",
                "reason": "no casetrack.db"}

    try:
        schema = load_schema(toml_path)
    except SchemaError as e:
        return {"project_dir": project_dir, "action": "skipped",
                "reason": f"invalid schema: {e}"}
    project_name = schema.get("project", {}).get("name") or project_dir.name
    toml_pid = schema.get("project", {}).get("project_id")

    conn = open_project_db(db_path)
    try:
        meta = read_project_meta(conn)
    finally:
        conn.close()
    db_pid = meta["project_id"] if meta else None

    # Detect drift before touching anything.
    if toml_pid and db_pid and toml_pid != db_pid:
        return {"project_dir": project_dir, "action": "skipped",
                "reason": (f"TOML project_id {toml_pid!r} disagrees with DB "
                           f"project_meta {db_pid!r}; resolve manually before migrating")}

    # Decide the target project_id.
    target = explicit_project_id or toml_pid or db_pid
    if not target:
        # Need to derive a slug. suggest_project_id falls back through name → dir.
        suggestion = (
            suggest_project_id(project_name)
            or suggest_project_id(project_dir.resolve().name)
        )
        if interactive and not yes:
            prompt = (
                f"\n[{project_dir}]\n"
                f"  Current project name: {project_name!r}\n"
                f"  Suggested project_id: {suggestion or '(none — name not slugifiable)'}\n"
                f"  Enter project_id (Enter to accept, ^C to abort): "
            )
            try:
                user_input = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                return {"project_dir": project_dir, "action": "skipped",
                        "reason": "user aborted"}
            target = user_input or suggestion
        else:
            target = suggestion
        if not target:
            return {"project_dir": project_dir, "action": "skipped",
                    "reason": "could not derive project_id; pass --project-id explicitly"}

    try:
        validate_project_id(target)
    except ValueError as e:
        return {"project_dir": project_dir, "action": "skipped", "reason": str(e)}

    # Refuse if the slug is already taken in the registry by a different project
    # (chosen call #2 from design-review: keep user in control, no auto-suffix).
    existing_path = registry_resolve(target)
    if existing_path is not None and existing_path.resolve() != project_dir.resolve():
        return {
            "project_dir": project_dir, "action": "skipped",
            "reason": (
                f"project_id {target!r} is already registered to "
                f"{existing_path}. Pass --project-id <other-slug> for this "
                f"project, or `casetrack projects deregister {target}` first "
                f"if you really want to retarget."
            ),
        }

    # ── do the writes ────────────────────────────────────────────────────────
    actions_taken: list[str] = []

    # 1. TOML — only rewrite if missing or different.
    if toml_pid != target:
        _insert_project_id_into_toml(toml_path, target)
        actions_taken.append("toml")

    # 2. project_meta row — write iff missing.
    if db_pid is None:
        conn = open_project_db(db_path)
        try:
            with begin_immediate(conn):
                write_project_meta(
                    conn, target, project_name,
                    int(schema.get("project", {}).get("schema_v", 1)),
                )
        finally:
            conn.close()
        actions_taken.append("project_meta")

    # 3. registry — register if missing.
    if existing_path is None:
        try:
            registry_register(target, project_dir, project_name)
            actions_taken.append("registry")
        except (OSError, ValueError) as e:
            return {"project_dir": project_dir, "action": "skipped",
                    "reason": f"registry write failed: {e}"}

    if not actions_taken:
        return {"project_dir": project_dir, "action": "noop",
                "project_id": target, "reason": "already migrated"}

    # Provenance entry — always log the migration so future audits see it.
    try:
        log_project_provenance(project_dir, {
            "action": "migrate_project_id",
            "project_id": target,
            "applied_to": actions_taken,
            "transaction_id": _new_transaction_id(),
            "schema_v_before": schema["project"]["schema_v"],
            "schema_v_after": schema["project"]["schema_v"],
        })
    except Exception:
        pass  # provenance failures shouldn't block the migration itself

    return {"project_dir": project_dir, "action": "migrated",
            "project_id": target, "applied_to": actions_taken}


def cmd_migrate_project_id(args):
    """`casetrack migrate-project-id` — bring legacy projects into the v0.6
    identity scheme (proposal 0005 §7).

    Two modes:
      --project-dir <path>     interactive single-project (default)
      --scan <root>            batch: walk the tree, migrate every casetrack
                               project missing a project_meta row

    Idempotent: re-running on an already-migrated project is a no-op.
    """
    scan_root = getattr(args, "scan", None)
    project_dir = getattr(args, "project_dir", None)
    if scan_root and project_dir:
        print(
            "Error: --scan and --project-dir are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not scan_root and not project_dir:
        print(
            "Error: pass either --project-dir <path> or --scan <root>.",
            file=sys.stderr,
        )
        sys.exit(1)

    explicit_id = getattr(args, "project_id", None)
    yes = bool(getattr(args, "yes", False))

    if scan_root:
        root = Path(scan_root)
        if not root.is_dir():
            print(f"Error: --scan root not found or not a directory: {root}",
                  file=sys.stderr)
            sys.exit(1)
        if explicit_id:
            print(
                "Error: --project-id is incompatible with --scan (each "
                "project needs its own slug). Re-run per-project for "
                "explicit overrides.",
                file=sys.stderr,
            )
            sys.exit(1)
        toml_paths = _find_v03_projects(root, max_depth=8)
        if not toml_paths:
            print(f"No casetrack projects found under {root}.")
            return
        results = []
        for toml in toml_paths:
            results.append(_migrate_one_project(
                toml.parent, explicit_project_id=None,
                yes=yes, interactive=not yes,
            ))
    else:
        results = [_migrate_one_project(
            Path(project_dir), explicit_project_id=explicit_id,
            yes=yes, interactive=not yes,
        )]

    # ── report ───────────────────────────────────────────────────────────────
    migrated = [r for r in results if r["action"] == "migrated"]
    noops    = [r for r in results if r["action"] == "noop"]
    skipped  = [r for r in results if r["action"] == "skipped"]

    for r in migrated:
        print(f"Migrated  {r['project_dir']}  → project_id={r['project_id']!r} "
              f"(updated: {', '.join(r['applied_to'])})")
    for r in noops:
        print(f"No-op     {r['project_dir']}  → project_id={r['project_id']!r} "
              f"(already migrated)")
    for r in skipped:
        print(f"Skipped   {r['project_dir']}  — {r['reason']}", file=sys.stderr)

    print(
        f"\nDone: {len(migrated)} migrated, {len(noops)} no-op, "
        f"{len(skipped)} skipped."
    )
    # Non-zero exit if anything was skipped so CI can catch silent failures.
    sys.exit(1 if skipped else 0)


def cmd_migrate(args):
    """Convert a v0.2 flat manifest into a v0.3 casetrack project."""
    source_path = Path(args.flat)
    out_dir = Path(args.out_dir)
    patient_col = args.patient_col
    specimen_col = args.specimen_col
    assay_col = args.assay_col

    if not source_path.exists():
        print(f"Error: flat manifest not found: {source_path}", file=sys.stderr)
        sys.exit(1)

    db_path = out_dir / PROJECT_DB_NAME
    if db_path.exists() and not args.force:
        print(
            f"Error: {db_path} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        metadata_map = _parse_metadata_map(args.metadata_map)
    except MigrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    df = pd.read_csv(source_path, sep="\t")
    for col in (patient_col, specimen_col, assay_col):
        if col not in df.columns:
            print(
                f"Error: required column {col!r} not in {source_path}. "
                f"Available: {list(df.columns)}",
                file=sys.stderr,
            )
            sys.exit(1)

    level_key_cols = {patient_col, specimen_col, assay_col}
    classifications: dict = {}
    reasons: dict = {}
    types: dict = {}
    for col in df.columns:
        if col in level_key_cols:
            continue
        # Manual override wins.
        override_level = None
        for level, cols in metadata_map.items():
            if col in cols:
                override_level = level
                break
        if override_level is not None:
            classifications[col] = override_level
            reasons[col] = f"--metadata-map override → {override_level}"
        else:
            level, reason = _classify_column(df, col, patient_col, specimen_col)
            classifications[col] = level
            reasons[col] = reason
        types[col] = _infer_column_type(df[col])

    out_dir.mkdir(parents=True, exist_ok=True)
    project_name = args.project_name or out_dir.resolve().name

    toml_text = _render_migration_toml(
        project_name, patient_col, specimen_col, assay_col, classifications, types
    )
    toml_path = out_dir / PROJECT_TOML_NAME
    toml_path.write_text(toml_text)

    try:
        schema = load_schema(toml_path)
    except SchemaError as e:
        print(f"Error: generated schema failed validation: {e}", file=sys.stderr)
        sys.exit(1)

    if db_path.exists():
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            leftover = Path(str(db_path) + suffix)
            if leftover.exists():
                leftover.unlink()

    conn = open_project_db(db_path)
    try:
        with begin_immediate(conn):
            for ddl in schema_to_ddl(schema):
                conn.execute(ddl)
        with begin_immediate(conn):
            counts = _insert_rows_by_level(
                conn, df, patient_col, specimen_col, assay_col, classifications,
                schema=schema,
            )
    except ValueError as e:
        # Raised by validate_hierarchy_id when the source TSV contains an
        # ID that violates the schema's format rules (proposal 0005 Part A).
        conn.close()
        print(
            f"Error: migration aborted — {e}\n"
            f"Hint: clean the source TSV IDs, or loosen the rules via "
            f"[levels.<level>] id_pattern in the generated casetrack.toml.",
            file=sys.stderr,
        )
        sys.exit(1)
    except sqlite3.IntegrityError as e:
        conn.close()
        print(
            f"Error: migration aborted — {type(e).__name__}: {e}\n"
            f"Hint: check for FK violations (specimens without patients, etc.) "
            f"or duplicate IDs in the source TSV.",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        if conn:
            conn.close()

    # Post-migration artifacts.
    prov_path = out_dir / PROJECT_PROVENANCE_NAME
    if not prov_path.exists():
        prov_path.touch()
    gitignore_path = out_dir / PROJECT_GITIGNORE_NAME
    if not gitignore_path.exists():
        gitignore_path.write_text(_project_gitignore_contents())

    sandbox = out_dir / "sandbox"
    sandbox.mkdir(exist_ok=True)
    shutil.copy2(source_path, sandbox / "source_manifest.tsv")

    _write_migration_reports(
        out_dir, source_path, classifications, types, reasons,
        counts, patient_col, specimen_col, assay_col,
    )

    log_project_provenance(out_dir, {
        "action": "migrate",
        "transaction_id": _new_transaction_id(),
        "source": str(source_path),
        "source_checksum": _checksum(str(source_path)),
        "rows_inserted": counts,
        "column_classifications": classifications,
        "column_types": types,
        "schema_v_before": 0,
        "schema_v_after": schema["project"]["schema_v"],
    })

    print(
        f"Migrated {source_path} → {out_dir}/\n"
        f"  patients  : {counts['patient']} rows\n"
        f"  specimens : {counts['specimen']} rows\n"
        f"  assays    : {counts['assay']} rows\n"
        f"Routing: {len(classifications)} non-key columns classified "
        f"(see .migration_report.md)."
    )


# ── v0.3 Project-mode helpers (shared across register/append/add-metadata) ─────


def _resolve_project(
    project_dir: str | Path | None,
    *,
    project_id: str | None = None,
    bypass_legacy_gate: bool = False,
) -> tuple[Path, dict]:
    """Validate a v0.3 project and load its schema.

    `bypass_legacy_gate` is an internal opt-out for upgrade-path commands
    (e.g. `migrate-qc`, `recover`) that by definition need to operate on
    projects without v0.6 identity wiring. End-user commands should leave
    it False; they go through the env-var bypass instead.

    Resolution order:
      1. If `project_id` is given, look it up in the registry.
      2. Else if `project_dir` is given, use it directly.
      3. Else exit with a clear error.

    When both are given, `project_id` wins; if `project_dir` doesn't match
    the registry's recorded path, emit a warning (the registry's `last_seen`
    is updated to the new path on success).

    Always runs `check_project_identity_consistency` to catch TOML↔DB
    `project_id` mismatches (a hard error) and bumps registry `last_seen`
    on a successful resolve.
    """
    if project_id is not None and not project_dir:
        resolved = registry_resolve(project_id)
        if resolved is None:
            reg_path = _registry_path()
            print(
                f"Error: project_id {project_id!r} is not in the registry "
                f"({reg_path}).\n"
                f"  - Run `casetrack projects list` to see known projects.\n"
                f"  - Or pass --project-dir <path> for a project that hasn't "
                f"been registered yet (and re-run `casetrack projects "
                f"register --project-dir <path>` to add it).",
                file=sys.stderr,
            )
            sys.exit(1)
        project_dir = resolved
    elif project_id is not None and project_dir:
        resolved = registry_resolve(project_id)
        if resolved is not None and resolved.resolve() != Path(project_dir).resolve():
            print(
                f"Warning: --project {project_id!r} maps to {resolved} in the "
                f"registry, but --project-dir is {project_dir}. Using "
                f"--project-dir; registry will be touched but path not changed.",
                file=sys.stderr,
            )
    if not project_dir:
        print(
            "Error: pass either --project-dir <path> or --project <project_id>.",
            file=sys.stderr,
        )
        sys.exit(1)

    project_dir = Path(project_dir)
    toml_path = project_dir / PROJECT_TOML_NAME
    db_path = project_dir / PROJECT_DB_NAME
    if not project_dir.is_dir():
        print(f"Error: project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)
    if not toml_path.exists():
        print(f"Error: {PROJECT_TOML_NAME} not found in {project_dir}", file=sys.stderr)
        sys.exit(1)
    if not db_path.exists():
        print(f"Error: {PROJECT_DB_NAME} not found in {project_dir}", file=sys.stderr)
        sys.exit(1)
    try:
        schema = load_schema(toml_path)
    except SchemaError as e:
        print(f"Error: invalid schema in {toml_path}: {e}", file=sys.stderr)
        sys.exit(1)

    # v0.6 Part B: two checks against the project's identity wiring.
    #   1. require_project_identity_or_fail — refuses un-migrated projects
    #      (alpha was tolerant; beta added the migrate command; final
    #      flips this on so legacy state is loud, not silent).
    #      Bypass via CASETRACK_ALLOW_LEGACY=1 for one-off audits.
    #   2. check_project_identity_consistency — hard error when TOML
    #      project_id differs from project_meta (DB was copied into the
    #      wrong directory, or TOML was hand-edited after init).
    conn = open_project_db(db_path)
    try:
        if not bypass_legacy_gate:
            require_project_identity_or_fail(conn, schema, project_dir)
        check_project_identity_consistency(conn, schema, project_dir)
    except ValueError as e:
        conn.close()
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Touch registry last_seen if this project is registered. Cheap and
    # silent — failures here never block the command.
    toml_pid = schema.get("project", {}).get("project_id")
    if toml_pid:
        registry_touch(toml_pid)

    return project_dir, schema


def _parse_meta_kv(spec: str | None) -> dict:
    """Parse 'age=60,sex=F,brca_status=brca1' into {'age': '60', 'sex': 'F', …}.

    Values remain strings; type coercion happens in _coerce_meta_to_schema
    against the column type declared in casetrack.toml.
    """
    out: dict = {}
    if not spec:
        return out
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"--meta: expected 'key=value', got {chunk!r}")
        k, v = chunk.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError(f"--meta: empty key in {chunk!r}")
        out[k] = v.strip()
    return out


_BOOL_TRUE = {"true", "t", "1", "yes", "y"}
_BOOL_FALSE = {"false", "f", "0", "no", "n"}


def _coerce_meta_to_schema(meta: dict, level_spec: dict) -> dict:
    """Coerce string meta values to the types declared in the level's schema.

    Rejects unknown columns. Lets SQLite's CHECK enforce enum constraints at
    INSERT time rather than pre-validating — one source of truth is better.
    """
    cols = level_spec["columns"]
    out: dict = {}
    for k, v in meta.items():
        if k not in cols:
            raise ValueError(
                f"--meta: column {k!r} not declared in schema; known: {sorted(cols)}"
            )
        ctype = cols[k]["type"]
        out[k] = _coerce_scalar(v, ctype, k)
    return out


def _coerce_scalar(value: str, ctype: str, field_name: str):
    if ctype == "TEXT" or ctype == "DATE":
        return value
    if ctype == "INTEGER":
        try:
            return int(value)
        except ValueError as e:
            raise ValueError(f"--meta {field_name}: expected INTEGER, got {value!r}") from e
    if ctype == "REAL":
        try:
            return float(value)
        except ValueError as e:
            raise ValueError(f"--meta {field_name}: expected REAL, got {value!r}") from e
    if ctype == "BOOLEAN":
        lv = value.lower()
        if lv in _BOOL_TRUE:
            return 1
        if lv in _BOOL_FALSE:
            return 0
        raise ValueError(
            f"--meta {field_name}: expected BOOLEAN (true/false/1/0), got {value!r}"
        )
    raise ValueError(f"--meta {field_name}: unsupported type {ctype!r}")


def _parent_exists(conn: sqlite3.Connection, parent_level: str, parent_id: str) -> bool:
    table = f"{parent_level}s"
    (count,) = conn.execute(
        f"SELECT COUNT(*) FROM {_quote_ident(table)} "
        f"WHERE {_quote_ident(f'{parent_level}_id')} = ?",
        (parent_id,),
    ).fetchone()
    return count == 1


# ── v0.3 register ──────────────────────────────────────────────────────────────


class _ParentMissing(Exception):
    """Internal signal that a parent row doesn't exist and --allow-new-parent
    was not passed. Raised inside the BEGIN IMMEDIATE block so `begin_immediate`
    rolls back any partial work; caught by cmd_register to emit exit-2."""

    def __init__(self, level: str, parent_id: str):
        super().__init__(f"{level} {parent_id!r} not found")
        self.level = level
        self.parent_id = parent_id


def cmd_register(args):
    """Insert a single row at `--level` with optional inline parent creation.

    Strict FK enforcement per proposal 0001 §19 Q2: unknown parents cause
    exit 2 unless the user opts in with --allow-new-parent --yes.
    """
    project_dir, schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    from casetrack_lifecycle.gate import assert_not_archived as _assert_not_archived
    _assert_not_archived(
        project_dir,
        force_archived=getattr(args, "force_archived", False),
        yes=getattr(args, "yes", False),
    )
    level = args.level

    if level not in LEVEL_ORDER:
        print(
            f"Error: --level must be one of {list(LEVEL_ORDER)}, got {level!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    level_spec = schema["levels"][level]
    key_col = level_spec["key"]
    table = f"{level}s"
    parent_level = level_spec.get("parent")
    parent_key_col = level_spec.get("parent_key")

    # Parent argument validation.
    if parent_level and not args.parent:
        print(
            f"Error: --parent is required for --level {level} "
            f"(expected {parent_level}_id)",
            file=sys.stderr,
        )
        sys.exit(1)
    if not parent_level and args.parent:
        print(
            f"Error: --level patient does not take --parent",
            file=sys.stderr,
        )
        sys.exit(1)

    # --allow-new-parent at the assay level would require inventing a
    # patient_id for the new specimen, which is unsafe. Reject explicitly.
    if level == "assay" and args.allow_new_parent:
        print(
            "Error: --allow-new-parent is not supported for --level assay "
            "(would need to invent a patient_id for the new specimen).\n"
            "Register the specimen first:\n"
            f"    casetrack register --project-dir {project_dir} --level specimen "
            f"--id {args.parent} --parent <PATIENT_ID> --allow-new-parent --yes",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.allow_new_parent and not args.yes:
        print(
            "Error: --allow-new-parent requires --yes to commit new parent creation.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        raw_meta = _parse_meta_kv(args.meta)
        meta = _coerce_meta_to_schema(raw_meta, level_spec)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Prevent --meta from reassigning level keys / parent FK behind our back.
    for reserved in (key_col, parent_key_col):
        if reserved and reserved in meta:
            print(
                f"Error: --meta cannot set {reserved!r}; use --id / --parent instead.",
                file=sys.stderr,
            )
            sys.exit(1)

    # v0.6: hierarchy ID format check (proposal 0005 Part A). Validate the
    # target --id and --parent against the schema's format rules before we
    # open the DB — fail loudly with a clear message at the root of the
    # problem, not three commands downstream.
    try:
        validate_hierarchy_id(args.id, schema, level)
        if parent_level and args.parent is not None:
            validate_hierarchy_id(args.parent, schema, parent_level)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    db_path = project_dir / PROJECT_DB_NAME
    conn = open_project_db(db_path)
    executed_sql: list[str] = []
    rows_affected = 0
    parent_created = False

    try:
        with begin_immediate(conn):
            # Case-variant check runs inside the transaction so concurrent
            # writers can't race between the check and the insert.
            check_id_case_unique(conn, schema, level, args.id)
            # Parent-existence check runs *inside* the transaction so a
            # concurrent writer can't race us between check and insert.
            if parent_level and not _parent_exists(conn, parent_level, args.parent):
                if not args.allow_new_parent:
                    # Roll back so we don't emit a provenance entry or partial writes.
                    raise _ParentMissing(parent_level, args.parent)
                parent_created = True
                # Parent will be created as a stub — validate its case-uniqueness too.
                check_id_case_unique(conn, schema, parent_level, args.parent)

            if parent_created:
                parent_table = f"{parent_level}s"
                parent_key = f"{parent_level}_id"
                stub_sql = (
                    f"INSERT INTO {_quote_ident(parent_table)} "
                    f"({_quote_ident(parent_key)}) VALUES (?)"
                )
                conn.execute(stub_sql, (args.parent,))
                executed_sql.append(stub_sql)
                rows_affected += 1

            # Build the target row: key + parent FK (if any) + meta columns.
            row: dict = {key_col: args.id}
            if parent_key_col:
                row[parent_key_col] = args.parent
            row.update(meta)

            cols = list(row.keys())
            quoted_cols = ", ".join(_quote_ident(c) for c in cols)
            placeholders = ", ".join("?" * len(cols))
            insert_sql = (
                f"INSERT INTO {_quote_ident(table)} ({quoted_cols}) "
                f"VALUES ({placeholders})"
            )
            conn.execute(insert_sql, tuple(row[c] for c in cols))
            executed_sql.append(insert_sql)
            rows_affected += 1

    except _ParentMissing as e:
        conn.close()
        print(
            f"Error: {e.level} {e.parent_id!r} does not exist.\n"
            f"To create it inline, re-run with --allow-new-parent --yes.",
            file=sys.stderr,
        )
        sys.exit(2)
    except ValueError as e:
        # Raised by check_id_case_unique when a case-variant already exists.
        conn.close()
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.IntegrityError as e:
        conn.close()
        print(f"Error: register aborted — {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if conn:
            conn.close()

    log_project_provenance(project_dir, {
        "action": "register",
        "level": level,
        "id": args.id,
        "parent": args.parent,
        "parent_created": parent_created,
        "meta": meta,
        "transaction_id": _new_transaction_id(),
        "sql": executed_sql,
        "rows_affected": rows_affected,
        "schema_v_before": schema["project"]["schema_v"],
        "schema_v_after": schema["project"]["schema_v"],
    })

    if parent_created:
        print(
            f"Registered {level} {args.id!r} under new {parent_level} {args.parent!r} "
            f"(stub; fill metadata with 'casetrack register --level {parent_level} "
            f"--id {args.parent} …' later)."
        )
    else:
        print(f"Registered {level} {args.id!r}.")


# ── v0.3 append ────────────────────────────────────────────────────────────────
#
# Implements `casetrack append --project-dir DIR` per proposal 0001 §7.1 & Q7.
# Writes go to the table corresponding to --level (default: schema's
# analysis_defaults.default_level, which the shipped templates set to "assay").
#
# Type inference (Q7 hybrid): infer the SQLite type from the summary TSV's
# pandas dtype; users override via `--col-type name:TYPE,...`.


def _parse_col_type_overrides(spec: str | None) -> dict:
    """Parse '--col-type col1:REAL,col2:INTEGER' into {col: TYPE}."""
    out: dict = {}
    if not spec:
        return out
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"--col-type: expected 'col:TYPE', got {chunk!r}")
        name, ctype = chunk.split(":", 1)
        name, ctype = name.strip(), ctype.strip().upper()
        if not name:
            raise ValueError(f"--col-type: empty column name in {chunk!r}")
        if ctype not in VALID_COLUMN_TYPES:
            raise ValueError(
                f"--col-type {name!r}: unsupported type {ctype!r}; "
                f"must be one of {sorted(VALID_COLUMN_TYPES)}"
            )
        out[name] = ctype
    return out


def _get_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return the column names of `table` in declared order."""
    return [row[1] for row in conn.execute(
        f"PRAGMA table_info({_quote_ident(table)})"
    ).fetchall()]


def _default_analysis_level(schema: dict) -> str:
    return (
        schema.get("analysis_defaults", {}).get("default_level")
        or "assay"
    )


class _MissingKeys(Exception):
    """Raised inside the append transaction when the results TSV references
    keys that don't exist in the target table — begin_immediate rolls back
    any ALTER TABLEs that had already been applied."""

    def __init__(self, missing: set):
        super().__init__(f"{len(missing)} missing keys")
        self.missing = missing


class _AppendOnCensored(Exception):
    """Raised when `casetrack append` targets entities whose qc_status is
    fail/censored/consent_revoked without the --force-append-on-censored --yes
    opt-in. Proposal 0002 §5.1.1 / §9."""

    def __init__(self, level: str, entity_ids: list[str]):
        super().__init__(f"{len(entity_ids)} censored {level}(s)")
        self.level = level
        self.entity_ids = entity_ids


def cmd_append_project(args):
    """Attach analysis results to rows at --level, extending the schema as needed."""
    project_dir, schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    from casetrack_lifecycle.gate import assert_not_archived as _assert_not_archived
    _assert_not_archived(
        project_dir,
        force_archived=getattr(args, "force_archived", False),
        yes=getattr(args, "yes", False),
    )
    level = args.level or _default_analysis_level(schema)
    if level not in LEVEL_ORDER:
        print(
            f"Error: --level must be one of {list(LEVEL_ORDER)}, got {level!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    level_spec = schema["levels"][level]
    key_col = level_spec["key"]
    table = f"{level}s"
    analysis = args.analysis

    results_path = Path(args.results)
    if not results_path.exists():
        print(f"Error: results file not found: {results_path}", file=sys.stderr)
        sys.exit(1)

    try:
        col_type_overrides = _parse_col_type_overrides(getattr(args, "col_type", None))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # --column-prefix: rename analysis columns to {prefix}_{name} on the way in
    # so two analyses sharing the same specimens table can't collide under
    # fill-only COALESCE. Protects the key column, v0.4 autoflag columns, and
    # the {analysis}_done timestamp — see the premerge_runs pattern README.
    column_prefix = getattr(args, "column_prefix", None) or ""
    if column_prefix:
        import re as _re
        if not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", column_prefix):
            print(
                f"Error: --column-prefix must be a valid identifier "
                f"(letters/digits/underscores, not starting with a digit); "
                f"got {column_prefix!r}",
                file=sys.stderr,
            )
            sys.exit(1)

    results = pd.read_csv(results_path, sep="\t")
    if key_col not in results.columns:
        print(
            f"Error: key column {key_col!r} not in {results_path}. "
            f"Columns: {list(results.columns)}",
            file=sys.stderr,
        )
        sys.exit(1)

    # v0.5: inject the inferred run_tag as a column so it flows through the
    # normal column-prefix / type-inference / UPDATE pathway. Users re-running
    # the same tool with a new run_tag must pass --overwrite; the fill-only
    # default preserves the first run_tag that landed.
    inferred_run_tag = getattr(args, "_inferred_run_tag", None)
    if inferred_run_tag and "run_tag" not in results.columns:
        results["run_tag"] = inferred_run_tag

    # v0.4: carve out the autoflag columns (qc_pass / qc_fail_reason / qc_warn)
    # before column-type inference so they never become analysis columns.
    from casetrack_qc.autoflag import AUTOFLAG_COLUMNS as _QC_AUTOFLAG_COLS
    autoflag_cols_present = [c for c in _QC_AUTOFLAG_COLS if c in results.columns]

    done_col = f"{analysis}{DONE_COLUMN_SUFFIX}"

    # Compute the rename plan (original_name → prefixed_name) before renaming
    # the DataFrame, so --col-type lookups below can still match original names.
    prefix_rename: dict = {}
    if column_prefix:
        for c in results.columns:
            if c == key_col:
                continue
            if c in _QC_AUTOFLAG_COLS:
                continue
            if c == done_col:
                continue
            prefix_rename[c] = f"{column_prefix}_{c}"
    if prefix_rename:
        results = results.rename(columns=prefix_rename)

    # Translate --col-type overrides from TSV names to prefixed names so the
    # user can keep writing them as they appear in their summary file.
    if prefix_rename and col_type_overrides:
        col_type_overrides = {
            prefix_rename.get(k, k): v for k, v in col_type_overrides.items()
        }

    analysis_cols = [
        c for c in results.columns
        if c != key_col and c not in _QC_AUTOFLAG_COLS
    ]
    if not analysis_cols and not autoflag_cols_present:
        print(
            f"Error: {results_path} has no columns besides {key_col!r}.",
            file=sys.stderr,
        )
        sys.exit(1)

    if done_col not in results.columns:
        results[done_col] = datetime.datetime.now().strftime(TIMESTAMP_FMT)
        analysis_cols.append(done_col)

    # Build the final column→type map (overrides > done-col → TEXT > pandas dtype).
    col_type_map: dict = {}
    for col in analysis_cols:
        if col in col_type_overrides:
            col_type_map[col] = col_type_overrides[col]
        elif col == done_col:
            col_type_map[col] = "TEXT"
        else:
            col_type_map[col] = _infer_column_type(results[col])

    # Reject override names that aren't in the TSV — most commonly a typo.
    unknown_overrides = set(col_type_overrides) - set(analysis_cols)
    if unknown_overrides:
        print(
            f"Error: --col-type references columns not in {results_path}: "
            f"{sorted(unknown_overrides)}",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = project_dir / PROJECT_DB_NAME
    conn = open_project_db(db_path)
    executed_sql: list[str] = []
    columns_added: list[str] = []
    rows_updated = 0
    autoflag_emitted: list[dict] = []
    txn_id = _new_transaction_id()

    try:
        with begin_immediate(conn):
            # Every row's key must already exist — append never auto-creates.
            # Users who need inline creation can use `casetrack register`
            # --allow-new-parent first.
            existing_keys = {
                r[0] for r in conn.execute(
                    f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)}"
                ).fetchall()
            }
            tsv_keys = set(results[key_col].astype(str))
            missing = tsv_keys - {str(k) for k in existing_keys}
            if missing:
                raise _MissingKeys(missing)

            # v0.4: strict-refuse append on entities that are already censored
            # (qc_status in {fail, censored, consent_revoked}). Pipeline
            # re-runs on known-bad assays waste cluster hours, so default is
            # exit 2 unless the user deliberately opts in with
            # --force-append-on-censored --yes (proposal §5.1.1 / §9).
            from casetrack_qc.schema import qc_schema_exists as _qc_schema_exists
            if _qc_schema_exists(conn):
                qc_rows = dict(
                    conn.execute(
                        f"SELECT {_quote_ident(key_col)}, qc_status "
                        f"FROM {_quote_ident(table)}"
                    ).fetchall()
                )
                censored = {
                    k for k in tsv_keys
                    if qc_rows.get(k) in ("fail", "censored", "consent_revoked")
                }
                force = (
                    getattr(args, "force_append_on_censored", False)
                    and getattr(args, "yes", False)
                )
                if censored and not force:
                    raise _AppendOnCensored(level, sorted(censored))
                if censored and force:
                    ids = ", ".join(sorted(censored))
                    print(
                        f"\u26A0 Forcing append on {len(censored)} censored "
                        f"{level}: {ids}\n"
                        "  Data written. Entity remains excluded from read paths.",
                        file=sys.stderr,
                    )

            # Add any new columns up-front so subsequent UPDATE can reference them.
            existing_cols = set(_get_table_columns(conn, table))
            for col in analysis_cols:
                if col not in existing_cols:
                    ctype = col_type_map[col]
                    ddl = (
                        f"ALTER TABLE {_quote_ident(table)} "
                        f"ADD COLUMN {_quote_ident(col)} {ctype}"
                    )
                    conn.execute(ddl)
                    executed_sql.append(ddl)
                    columns_added.append(col)

            # Build one UPDATE per row. With --overwrite: unconditional SET.
            # Otherwise COALESCE(col, ?) preserves any pre-existing non-null value.
            if args.overwrite:
                set_clauses = ", ".join(f"{_quote_ident(c)} = ?" for c in analysis_cols)
            else:
                set_clauses = ", ".join(
                    f"{_quote_ident(c)} = COALESCE({_quote_ident(c)}, ?)"
                    for c in analysis_cols
                )
            update_sql = (
                f"UPDATE {_quote_ident(table)} SET {set_clauses} "
                f"WHERE {_quote_ident(key_col)} = ?"
            )

            for _, row in results.iterrows():
                values = tuple(_coerce_for_sqlite(row[c]) for c in analysis_cols)
                values += (row[key_col],)
                conn.execute(update_sql, values)
                rows_updated += 1

            executed_sql.append(update_sql)

            # v0.4 SLURM auto-flag (§6): if the summary TSV had qc_pass /
            # qc_fail_reason / qc_warn columns, turn them into qc_events rows
            # + bump qc_status. Same transaction — if any DB write fails,
            # both data and QC roll back together.
            if autoflag_cols_present and _qc_schema_exists(conn):
                from casetrack_qc.autoflag import apply_autoflag as _apply_autoflag
                autoflag_emitted = _apply_autoflag(
                    conn, results, key_col,
                    level=level,
                    transaction_id=txn_id,
                    source="slurm",
                )
    except _MissingKeys as e:
        conn.close()
        preview = sorted(e.missing)[:5]
        print(
            f"Error: {len(e.missing)} key(s) in {results_path} do not exist in "
            f"table {table!r}: {preview}{'…' if len(e.missing) > 5 else ''}\n"
            f"Register them first with 'casetrack register --level {level} --id ... "
            f"--parent ...' before appending.",
            file=sys.stderr,
        )
        sys.exit(2)
    except _AppendOnCensored as e:
        conn.close()
        preview = e.entity_ids[:3]
        more = f" (+{len(e.entity_ids) - 3} more)" if len(e.entity_ids) > 3 else ""
        print(
            f"Error: {len(e.entity_ids)} {e.level}(s) in {results_path} are "
            f"censored: {preview}{more}\n"
            f"Suggestions:\n"
            f"  - If the issue is resolved: "
            f"casetrack uncensor --level {e.level} --id <ID> "
            f"--reason '<fix>'\n"
            f"  - If you need to land data anyway (rare): "
            f"--force-append-on-censored --yes",
            file=sys.stderr,
        )
        sys.exit(2)
    except sqlite3.IntegrityError as e:
        conn.close()
        print(f"Error: append aborted — {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if conn:
            conn.close()

    log_project_provenance(project_dir, {
        "action": "append",
        "level": level,
        "analysis": analysis,
        "column_prefix": column_prefix or None,
        "prefix_rename": prefix_rename or None,
        "columns_added": columns_added,
        "rows_affected": rows_updated,
        "results_file": str(results_path),
        "results_checksum": _checksum(str(results_path)),
        "col_type_overrides": col_type_overrides,
        "transaction_id": txn_id,
        "sql": executed_sql,
        "schema_v_before": schema["project"]["schema_v"],
        "schema_v_after": schema["project"]["schema_v"],
        "autoflag_events": [e["qc_event_id"] for e in autoflag_emitted],
        "run_tag": inferred_run_tag,
    })

    # One `censor` provenance entry per autoflag event, all sharing the
    # append's transaction_id (proposal §12 Q7 lean: one entry per event).
    for em in autoflag_emitted:
        log_project_provenance(project_dir, {
            "action": "censor",
            "level": level,
            "entity_id": em["entity_id"],
            "kind": em["kind"],
            "reason": em["reason"],
            "source": "slurm",
            "created_by": f"slurm:{os.environ.get('SLURM_JOB_ID', 'unknown')}",
            "transaction_id": txn_id,
            "qc_event_id": em["qc_event_id"],
            "new_qc_status": em["new_qc_status"],
            "from_analysis": analysis,
        })

    added_note = f" (+{len(columns_added)} new column{'s' if len(columns_added) != 1 else ''})" if columns_added else ""
    autoflag_note = (
        f" (+{len(autoflag_emitted)} qc event{'s' if len(autoflag_emitted) != 1 else ''})"
        if autoflag_emitted else ""
    )
    print(
        f"Appended {analysis!r} to {rows_updated} {level} row(s){added_note}{autoflag_note}."
    )


# ── v0.3 schema {show, dump, check, apply} ────────────────────────────────────
#
# `casetrack schema --project-dir D <action>` — manage the TOML↔DB schema
# lifecycle (proposal 0001 §7.2).
#   show   — print the current casetrack.toml
#   dump   — regenerate TOML from the live DB (useful if TOML was lost)
#   check  — report TOML↔DB drift (declared-missing / undeclared columns)
#   apply  — add any TOML-declared columns that are missing in the DB via
#            ALTER TABLE; bumps schema_v on change.


_DB_TYPE_TO_TOML = {
    "TEXT": "TEXT",
    "INTEGER": "INTEGER",
    "REAL": "REAL",
    "BOOLEAN": "BOOLEAN",
    "DATE": "DATE",
    "": "TEXT",  # no declared type → TEXT (SQLite default affinity)
}


def _get_table_column_types(conn: sqlite3.Connection, table: str) -> dict:
    """Return {colname: declared_type} for `table` via PRAGMA table_info."""
    out = {}
    for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall():
        # row = (cid, name, type, notnull, dflt_value, pk)
        out[row[1]] = (row[2] or "").upper()
    return out


def cmd_schema_project(args):
    action = getattr(args, "action", None) or "show"
    if action not in {"show", "dump", "check", "apply"}:
        print(
            f"Error: unknown schema action {action!r}; must be show|dump|check|apply",
            file=sys.stderr,
        )
        sys.exit(1)
    project_dir, schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    toml_path = project_dir / PROJECT_TOML_NAME

    if action == "show":
        print(toml_path.read_text(), end="")
        return

    if action == "dump":
        conn = open_project_db(project_dir / PROJECT_DB_NAME)
        try:
            print(_dump_schema_from_db(conn, schema["project"]["name"],
                                       schema["project"].get("schema_v", 1)))
        finally:
            conn.close()
        return

    if action in ("check", "apply"):
        issues = _schema_drift(project_dir, schema)
        if action == "check":
            if not issues:
                print("Schema OK: TOML matches DB.")
                return
            print(f"Schema drift ({len(issues)} issue(s)):", file=sys.stderr)
            for i in issues:
                print(f"  - {_format_drift_issue(i)}", file=sys.stderr)
            sys.exit(1)
        return _schema_apply(project_dir, schema, issues)


# Columns added by the v0.4 QC subsystem. The drift checker skips these so
# that `casetrack schema check` doesn't flag a project's own QC plumbing as
# undeclared drift.
_V04_QC_MANAGED_COLUMNS: dict[str, tuple[str, ...]] = {
    "patient": ("qc_status", "consent_status", "consent_date", "withdrawal_date"),
    "specimen": ("qc_status",),
    "assay": ("qc_status",),
}


def _schema_drift(project_dir: Path, schema: dict) -> list[dict]:
    """Return a structured list of drift entries {'kind', 'table', 'column', ...}."""
    issues: list[dict] = []
    conn = open_project_db(project_dir / PROJECT_DB_NAME)
    try:
        for level in LEVEL_ORDER:
            table = f"{level}s"
            declared = schema["levels"][level]["columns"]
            actual = _get_table_column_types(conn, table)
            for col, spec in declared.items():
                if col not in actual:
                    issues.append({
                        "kind": "missing_in_db",
                        "table": table,
                        "column": col,
                        "type": spec["type"],
                    })
            for col in actual:
                if col in declared:
                    continue
                if col.endswith(DONE_COLUMN_SUFFIX):
                    # Analysis-added columns legitimately live in the DB only.
                    continue
                # v0.4 QC columns are managed by the QC subsystem, not the TOML.
                if col in _V04_QC_MANAGED_COLUMNS.get(level, ()):
                    continue
                # Check if this column was introduced by an `append` entry in
                # provenance — that's how v0.3 tracks dynamic analysis columns.
                if _is_analysis_column(project_dir / PROJECT_PROVENANCE_NAME, col):
                    continue
                issues.append({
                    "kind": "undeclared_in_db",
                    "table": table,
                    "column": col,
                })
    finally:
        conn.close()
    return issues


def _format_drift_issue(issue: dict) -> str:
    table = issue["table"]
    col = issue["column"]
    if issue["kind"] == "missing_in_db":
        return f"{table}.{col} ({issue['type']}) — declared in TOML but not in DB (run `schema apply`)"
    if issue["kind"] == "undeclared_in_db":
        return f"{table}.{col} — exists in DB but not declared in TOML (declare it or drop it)"
    return f"{table}.{col} — unknown drift: {issue}"


def _is_analysis_column(prov_path: Path, col: str) -> bool:
    cols_by_analysis = _collect_analysis_columns_from_provenance(prov_path)
    return any(col in cols for cols in cols_by_analysis.values())


def _schema_apply(project_dir: Path, schema: dict, issues: list[dict]) -> None:
    """Apply `missing_in_db` drift via ALTER TABLE ADD COLUMN. Bump schema_v."""
    to_add = [i for i in issues if i["kind"] == "missing_in_db"]
    if not to_add:
        print("Schema up to date; nothing to apply.")
        return

    conn = open_project_db(project_dir / PROJECT_DB_NAME)
    executed_sql: list[str] = []
    try:
        with begin_immediate(conn):
            for i in to_add:
                ddl = (
                    f"ALTER TABLE {_quote_ident(i['table'])} "
                    f"ADD COLUMN {_quote_ident(i['column'])} {i['type']}"
                )
                conn.execute(ddl)
                executed_sql.append(ddl)
    finally:
        conn.close()

    # Bump schema_v in the TOML.
    toml_path = project_dir / PROJECT_TOML_NAME
    old_v = schema["project"].get("schema_v", 1)
    new_v = old_v + 1
    toml_text = toml_path.read_text()
    # Simple line-level substitution — the templates write schema_v on its
    # own line. If this fails we fall back to leaving the file alone; the
    # provenance entry still records the intended version.
    import re
    patched = re.sub(
        r"(?m)^schema_v\s*=\s*\d+\s*$",
        f"schema_v = {new_v}",
        toml_text,
        count=1,
    )
    if patched != toml_text:
        toml_path.write_text(patched)

    log_project_provenance(project_dir, {
        "action": "schema_apply",
        "transaction_id": _new_transaction_id(),
        "columns_added": [(i["table"], i["column"], i["type"]) for i in to_add],
        "sql": executed_sql,
        "schema_v_before": old_v,
        "schema_v_after": new_v,
    })

    print(
        f"Applied {len(to_add)} schema change(s); schema_v {old_v} → {new_v}."
    )
    for i in to_add:
        print(f"  + {i['table']}.{i['column']} {i['type']}")


def _dump_schema_from_db(conn: sqlite3.Connection, project_name: str, schema_v: int) -> str:
    """Reverse-engineer a casetrack.toml from the live DB's PRAGMA data."""
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    lines = [
        "[project]",
        f'name     = "{project_name}"',
        f"schema_v = {schema_v}",
        f'created  = "{now}"',
        "",
    ]
    parents = {"patient": (None, None), "specimen": ("patient", "patient_id"),
               "assay": ("specimen", "specimen_id")}
    for level in LEVEL_ORDER:
        table = f"{level}s"
        key_col = f"{level}_id"
        cols = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
        lines.append(f"[levels.{level}]")
        lines.append(f'key        = "{key_col}"')
        parent, parent_key = parents[level]
        if parent:
            lines.append(f'parent     = "{parent}"')
            lines.append(f'parent_key = "{parent_key}"')
        lines.append("")
        lines.append(f"[levels.{level}.columns]")
        for _cid, name, ctype, notnull, _dflt, pk in cols:
            # Skip analysis-added columns — they belong in provenance, not schema.
            if name.endswith(DONE_COLUMN_SUFFIX):
                continue
            normalized = _DB_TYPE_TO_TOML.get((ctype or "").upper(), "TEXT")
            props = [f'type = "{normalized}"']
            if pk or notnull:
                props.append("required = true")
            if pk:
                props.append("unique = true")
            lines.append(f"{name} = {{ {', '.join(props)} }}")
        lines.append("")
    lines += [
        "[analysis_defaults]",
        'default_level = "assay"',
        "",
        "[engine]",
        "wal             = true",
        f"busy_timeout_ms = {SQLITE_BUSY_TIMEOUT_MS}",
        "",
    ]
    return "\n".join(lines)


# ── v0.3 projects --root (v0.3 + v0.2 detection) ───────────────────────────────
#
# Walk a root directory, finding both flat-mode manifest.tsv files AND v0.3
# projects (casetrack.toml + casetrack.db). Summaries come from the same
# `_summarize_project` function for flat mode; v0.3 projects get their own
# summarizer that talks to SQLite.


def _find_v03_projects(root: Path, max_depth: int) -> list:
    """Return casetrack.toml paths under `root` that have a sibling
    casetrack.db. A TOML without a DB is treated as a partial / broken
    project and skipped — the `projects` overview is meant for live state."""
    root = root.resolve()
    matches = []
    for toml in root.rglob(PROJECT_TOML_NAME):
        if not toml.is_file():
            continue
        try:
            rel_parts = toml.resolve().relative_to(root).parts
        except ValueError:
            continue
        depth = len(rel_parts) - 1
        if depth > max_depth:
            continue
        if any(part.startswith(".") or part == "sandbox" for part in rel_parts[:-1]):
            continue
        if not (toml.parent / PROJECT_DB_NAME).exists():
            continue
        matches.append(toml)
    return matches


def _summarize_v03_project(toml_path: Path) -> dict:
    project_dir = toml_path.parent
    schema = load_schema(toml_path)
    conn = open_project_db(project_dir / PROJECT_DB_NAME)
    try:
        counts = {
            level: conn.execute(
                f"SELECT COUNT(*) FROM {_quote_ident(f'{level}s')}"
            ).fetchone()[0]
            for level in LEVEL_ORDER
        }
        done_by_level = _discover_done_columns(conn)
        done_total = sum(len(v) for v in done_by_level.values())
        total_cells = sum(
            len(done_by_level[lv]) * counts[lv] for lv in LEVEL_ORDER
        )
        completed = 0
        for level in LEVEL_ORDER:
            table = f"{level}s"
            for dc in done_by_level[level]:
                (n,) = conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_ident(table)} "
                    f"WHERE {_quote_ident(dc)} IS NOT NULL"
                ).fetchone()
                completed += n
    finally:
        conn.close()

    pct = round(100.0 * completed / total_cells, 1) if total_cells else 0.0
    return {
        "kind": "v0.3",
        "name": project_dir.name,
        "path": str(toml_path),
        # `samples` = assays count so the overview table compares apples-
        # to-apples with v0.2 (one flat row ≈ one assay in v0.3).
        "samples": counts["assay"],
        "patients": counts["patient"],
        "specimens": counts["specimen"],
        "assays": counts["assay"],
        "analyses": done_total,
        "completed_cells": completed,
        "total_cells": total_cells,
        "pct": pct,
        "schema_v": schema["project"].get("schema_v"),
    }


# ── v0.3 doctor (concurrency stress test) ─────────────────────────────────────
#
# Proposal 0001 §9.3. Forks workers that concurrently INSERT into a scratch
# table in the project DB, measures contention, and verifies no corruption.
# Intended to run once per project at kickoff on a new cluster / filesystem.


def _doctor_worker(db_path_str: str, writes: int, worker_id: int) -> dict:
    """Run `writes` INSERTs into the __doctor_scratch table. Returns stats."""
    conn = sqlite3.connect(db_path_str)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    ok = 0
    fail = 0
    errors: list[str] = []
    import time as _t
    t0 = _t.perf_counter()
    try:
        for i in range(writes):
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO __doctor_scratch (worker, i, ts) VALUES (?, ?, ?)",
                    (worker_id, i, _t.time()),
                )
                conn.commit()
                ok += 1
            except sqlite3.Error as e:
                fail += 1
                if len(errors) < 3:
                    errors.append(f"{type(e).__name__}: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
    finally:
        conn.close()
    return {
        "worker_id": worker_id,
        "ok": ok,
        "fail": fail,
        "errors": errors,
        "elapsed_s": _t.perf_counter() - t0,
    }


def _filesystem_name(path: Path) -> str:
    """Best-effort filesystem type ID via statfs (Linux)."""
    try:
        import ctypes
        import ctypes.util

        # Prefer the `os.statvfs` route plus /proc/mounts for the fs name.
        mounts_path = Path("/proc/mounts")
        if mounts_path.exists():
            target = str(path.resolve())
            best = ""
            best_len = -1
            for line in mounts_path.read_text().splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1]
                fs = parts[2]
                if target.startswith(mnt) and len(mnt) > best_len:
                    best = fs
                    best_len = len(mnt)
            if best:
                return best
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _suggest_clean_id(bad: str) -> str | None:
    """Return a regex-compliant slug derived from `bad`, or None if not possible.

    Heuristic: replace each run of forbidden characters with a single `_`,
    strip leading `_`/`-`/`.`, truncate to 64. If the result matches the
    default _ID_PATTERN, return it; else return None (manual rename needed).
    """
    if not isinstance(bad, str):
        return None
    # Collapse runs of forbidden chars to a single underscore.
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", bad)
    # Strip leading/trailing separators.
    cleaned = cleaned.lstrip("_-.")
    cleaned = cleaned.rstrip("_-.")
    cleaned = cleaned[:64]
    if not cleaned:
        return None
    return cleaned if _ID_PATTERN.fullmatch(cleaned) else None


def _scan_ids_for_format(project_dir: Path, schema: dict) -> list[dict]:
    """Scan patients / specimens / assays for IDs that violate the schema's
    format rules. Returns a list of violation dicts with level, id, rule,
    and an optional suggestion. Read-only — never mutates.
    """
    db_path = project_dir / PROJECT_DB_NAME
    violations: list[dict] = []
    conn = open_project_db(db_path)
    try:
        for level in LEVEL_ORDER:
            level_spec = schema["levels"][level]
            key_col = level_spec["key"]
            table = f"{level}s"
            rows = conn.execute(
                f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)}"
            ).fetchall()
            for (value,) in rows:
                try:
                    validate_hierarchy_id(value, schema, level)
                except ValueError as e:
                    violations.append({
                        "level": level,
                        "id": value,
                        "rule": str(e),
                        "suggestion": _suggest_clean_id(
                            value if isinstance(value, str) else str(value)
                        ),
                    })
    finally:
        conn.close()
    return violations


def _print_id_format_report(violations: list[dict], fmt: str) -> None:
    """Emit the scan report in either human-table or TSV form."""
    if fmt == "tsv":
        print("level\tid\tsuggestion\trule")
        for v in violations:
            sug = v["suggestion"] or ""
            print(f"{v['level']}\t{v['id']}\t{sug}\t{v['rule']}")
        return
    # Human-readable table.
    if not violations:
        print("✓ All hierarchy IDs conform to the schema's format rules.")
        return
    print(f"Found {len(violations)} malformed hierarchy ID(s):\n")
    for v in violations:
        sug = v["suggestion"]
        hint = f" → suggested rename: {sug!r}" if sug else " → no safe suggestion (manual rename needed)"
        print(f"  [{v['level']}] {v['id']!r}{hint}")
        print(f"      rule: {v['rule']}")
        print()
    print(
        "No auto-rename: patient/specimen/assay renames have FK cascade "
        "implications. Produce a migration TSV (old_id → new_id) and apply "
        "it manually, or loosen the rule via [levels.<level>] id_pattern / "
        "allow_case_variants / [project] allow_unicode_ids in casetrack.toml."
    )


def cmd_doctor_project(args):
    project_dir, schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    db_path = project_dir / PROJECT_DB_NAME

    # v0.6: --id-format switches to hierarchy-ID scan mode (proposal 0005
    # Part A). Read-only; exits non-zero if any non-conforming IDs exist so
    # CI can catch drift without the user having to re-parse free-text output.
    if getattr(args, "id_format", False):
        fmt = getattr(args, "fmt", None) or "table"
        if fmt not in ("table", "tsv"):
            print(
                f"Error: --fmt must be one of table|tsv, got {fmt!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            violations = _scan_ids_for_format(project_dir, schema)
        except sqlite3.Error as e:
            print(f"Error: DB read failed: {e}", file=sys.stderr)
            sys.exit(2)
        _print_id_format_report(violations, fmt)
        sys.exit(1 if violations else 0)

    workers = args.workers or 8
    writes = args.writes or 50

    # Create the scratch table in the main DB. Drop any leftover from a
    # prior aborted doctor run.
    conn = open_project_db(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS __doctor_scratch")
        conn.execute("""
            CREATE TABLE __doctor_scratch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker INTEGER NOT NULL,
                i INTEGER NOT NULL,
                ts REAL NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()

    fs_name = _filesystem_name(db_path)
    total = workers * writes
    print(f"Testing SQLite concurrency on {project_dir}/ ...")
    print(f"  Filesystem:            {fs_name}")
    print(f"  Spawning {workers} workers × {writes} INSERTs = {total} total ...")

    import multiprocessing as _mp
    import time as _t

    t0 = _t.perf_counter()
    ctx = _mp.get_context("fork")
    with ctx.Pool(processes=workers) as pool:
        results = pool.starmap(
            _doctor_worker,
            [(str(db_path), writes, w) for w in range(workers)],
        )
    elapsed = _t.perf_counter() - t0

    ok = sum(r["ok"] for r in results)
    fail = sum(r["fail"] for r in results)
    all_errors: list[str] = []
    for r in results:
        all_errors.extend(r["errors"])

    # Verify count matches what we successfully committed.
    conn = open_project_db(db_path)
    try:
        (n_rows,) = conn.execute(
            "SELECT COUNT(*) FROM __doctor_scratch"
        ).fetchone()
        # Clean up.
        conn.execute("DROP TABLE __doctor_scratch")
        conn.commit()
    finally:
        conn.close()

    print(f"  Elapsed:               {elapsed:.1f}s")
    print(f"  Successful commits:    {ok}/{total}")
    print(f"  Failed commits:        {fail}")
    print(f"  Rows committed in DB:  {n_rows}")

    healthy = (
        fail == 0
        and ok == total
        and n_rows == total
        and not any("CORRUPT" in e or "MISUSE" in e for e in all_errors)
    )
    if healthy:
        print(f"  ✓ SQLite concurrency healthy on this filesystem.")
        return

    print(f"  ✗ Concurrency issues detected:", file=sys.stderr)
    if ok != n_rows:
        print(
            f"    - {ok} commits reported success but only {n_rows} rows in DB "
            f"(silent partial commit)",
            file=sys.stderr,
        )
    for e in all_errors[:5]:
        print(f"    - {e}", file=sys.stderr)
    print(
        "  If you see CORRUPT / MISUSE errors or silent partial commits, "
        "this filesystem's POSIX lock semantics are unreliable. Consider "
        "moving the project to a POSIX-compliant fs (WekaFS is known good) "
        "or escalate to Tier 2 concurrency (per-task JSONL + merger).",
        file=sys.stderr,
    )
    sys.exit(1)


# ── v0.3 recover (rebuild DB from provenance) ─────────────────────────────────
#
# Proposal 0001 §9.4. Replays provenance.jsonl entries to reconstruct
# casetrack.db. Self-contained for init_project / register / schema_apply.
# For append / add_metadata / migrate: replays by re-reading the source file
# that was logged in provenance, after verifying its checksum. If the source
# has moved or changed, the entry is reported and the user asked to fix it.


def cmd_recover_project(args):
    project_dir = Path(args.project_dir)
    if not project_dir.is_dir():
        print(f"Error: project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    prov_path = Path(getattr(args, "from_", None) or project_dir / PROJECT_PROVENANCE_NAME)
    if not prov_path.exists():
        print(f"Error: provenance log not found: {prov_path}", file=sys.stderr)
        sys.exit(1)

    entries = []
    for line in prov_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"Error: malformed provenance line: {e}", file=sys.stderr)
            sys.exit(1)

    if not entries:
        print("No provenance entries to replay.")
        return

    db_path = project_dir / PROJECT_DB_NAME
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            if not args.force:
                print(
                    f"Error: {p} already exists. Use --force to overwrite the "
                    f"current DB before replaying provenance.",
                    file=sys.stderr,
                )
                sys.exit(1)
            p.unlink()

    stats = {
        "init_project": 0, "register": 0, "schema_apply": 0,
        "append": 0, "migrate": 0, "add_metadata": 0,
        "censor": 0, "uncensor": 0, "ethics_override": 0, "migrate_qc": 0,
        "skipped": 0,
    }
    warnings: list[str] = []

    # v0.4 QC replay is plugged in via casetrack_qc.recover_qc_action so the
    # subsystem's provenance actions land byte-identical to the original DB
    # (success criterion in proposal §13).
    from casetrack_qc.recover import recover_qc_action as _recover_qc_action

    conn = None
    try:
        for entry in entries:
            action = entry.get("action")
            if action == "init_project":
                conn = _recover_init_project(db_path, entry)
                stats["init_project"] += 1
            elif action == "register":
                if conn is None:
                    raise RuntimeError("register entry seen before init_project")
                _recover_register(conn, entry)
                stats["register"] += 1
            elif action == "schema_apply":
                if conn is None:
                    raise RuntimeError("schema_apply seen before init_project")
                _recover_schema_apply(conn, entry)
                stats["schema_apply"] += 1
            elif action == "append":
                msg = _recover_append(conn, project_dir, entry)
                if msg:
                    warnings.append(msg)
                    stats["skipped"] += 1
                else:
                    stats["append"] += 1
            elif action == "add_metadata":
                msg = _recover_add_metadata(conn, project_dir, entry)
                if msg:
                    warnings.append(msg)
                    stats["skipped"] += 1
                else:
                    stats["add_metadata"] += 1
            elif action == "migrate":
                msg = _recover_migrate(conn, project_dir, entry)
                if msg:
                    warnings.append(msg)
                    stats["skipped"] += 1
                else:
                    stats["migrate"] += 1
            elif action in ("censor", "uncensor", "ethics_override", "migrate_qc"):
                if conn is None:
                    raise RuntimeError(
                        f"{action} entry seen before init_project"
                    )
                handled, warn = _recover_qc_action(conn, entry)
                if not handled:
                    warnings.append(f"unknown action {action!r}; skipping")
                    stats["skipped"] += 1
                elif warn:
                    warnings.append(warn)
                    stats["skipped"] += 1
                else:
                    stats[action] += 1
            else:
                warnings.append(f"unknown action {action!r}; skipping")
                stats["skipped"] += 1
    finally:
        if conn is not None:
            conn.close()

    print(f"Rebuilt {db_path} from {len(entries)} provenance entries:")
    for k, v in stats.items():
        if v:
            print(f"  {k:<18} {v}")
    for w in warnings:
        print(f"  [warn] {w}", file=sys.stderr)

    if stats["skipped"] and not args.permit_partial:
        print(
            "\nOne or more entries could not be replayed. Re-run with "
            "--permit-partial to accept the partial rebuild, or restore the "
            "missing source files and retry.",
            file=sys.stderr,
        )
        sys.exit(2)


def _recover_init_project(db_path: Path, entry: dict) -> sqlite3.Connection:
    """Recreate the three tables from the CREATE TABLE statements captured
    in the init_project provenance entry."""
    conn = open_project_db(db_path)
    with begin_immediate(conn):
        for stmt in entry.get("sql", []):
            conn.execute(stmt)
    return conn


def _recover_register(conn: sqlite3.Connection, entry: dict) -> None:
    level = entry["level"]
    table = f"{level}s"
    key_col = f"{level}_id"
    row_id = entry["id"]
    parent_id = entry.get("parent")
    parent_created = entry.get("parent_created", False)
    meta = entry.get("meta") or {}

    with begin_immediate(conn):
        if parent_created:
            # Infer parent level from the hierarchy, not the entry (keeps
            # us decoupled from how the entry was produced).
            parents = {"specimen": "patient", "assay": "specimen"}
            parent_level = parents[level]
            parent_table = f"{parent_level}s"
            parent_key = f"{parent_level}_id"
            conn.execute(
                f"INSERT INTO {_quote_ident(parent_table)} "
                f"({_quote_ident(parent_key)}) VALUES (?)",
                (parent_id,),
            )
        row = {key_col: row_id}
        if level in ("specimen", "assay"):
            parent_key_col = "patient_id" if level == "specimen" else "specimen_id"
            row[parent_key_col] = parent_id
        row.update(meta)
        cols = list(row.keys())
        placeholders = ", ".join("?" * len(cols))
        quoted = ", ".join(_quote_ident(c) for c in cols)
        conn.execute(
            f"INSERT INTO {_quote_ident(table)} ({quoted}) VALUES ({placeholders})",
            tuple(row[c] for c in cols),
        )


def _recover_schema_apply(conn: sqlite3.Connection, entry: dict) -> None:
    with begin_immediate(conn):
        for stmt in entry.get("sql", []):
            conn.execute(stmt)


def _check_source_file(entry_path: str, recorded_checksum: str | None) -> tuple[Path | None, str | None]:
    """Return (path, None) if the source file is present and checksum matches.
    Return (None, warning) otherwise."""
    p = Path(entry_path)
    if not p.exists():
        return None, f"source file missing: {entry_path}"
    if recorded_checksum:
        actual = _checksum(str(p))
        if actual != recorded_checksum:
            return None, (
                f"source checksum mismatch for {entry_path} "
                f"(recorded {recorded_checksum}, got {actual})"
            )
    return p, None


def _recover_append(conn: sqlite3.Connection, project_dir: Path,
                    entry: dict) -> str | None:
    path, err = _check_source_file(entry.get("results_file", ""),
                                    entry.get("results_checksum"))
    if err:
        return f"append({entry.get('analysis')}): {err}"

    level = entry.get("level") or "assay"
    level_spec = _level_spec_from_provenance_or_db(conn, level)
    key_col = level_spec["key"]
    table = f"{level}s"
    analysis = entry.get("analysis", "unknown")

    results = pd.read_csv(path, sep="\t")
    # v0.4: strip autoflag columns so replay matches what the original append
    # wrote to the DB (those columns never became analysis columns).
    from casetrack_qc.autoflag import AUTOFLAG_COLUMNS as _QC_AUTOFLAG_COLS
    analysis_cols = [
        c for c in results.columns
        if c != key_col and c not in _QC_AUTOFLAG_COLS
    ]
    done_col = f"{analysis}{DONE_COLUMN_SUFFIX}"
    # Re-add columns that appear in entry.columns_added (fresh DB has base only).
    with begin_immediate(conn):
        existing = set(_get_table_columns(conn, table))
        for col_spec in entry.get("columns_added", []):
            # columns_added is a list of column names in v0.3's format.
            col = col_spec if isinstance(col_spec, str) else col_spec[1]
            if col in existing:
                continue
            # Infer a type by looking at the source TSV, or use TEXT for done.
            if col == done_col:
                ctype = "TEXT"
            elif col in results.columns:
                ctype = _infer_column_type(results[col])
            else:
                ctype = "TEXT"
            conn.execute(
                f"ALTER TABLE {_quote_ident(table)} "
                f"ADD COLUMN {_quote_ident(col)} {ctype}"
            )
            existing.add(col)

        if done_col not in results.columns:
            results[done_col] = entry.get("timestamp", "")
            analysis_cols.append(done_col)

        set_clauses = ", ".join(
            f"{_quote_ident(c)} = COALESCE({_quote_ident(c)}, ?)" for c in analysis_cols
        )
        update_sql = (
            f"UPDATE {_quote_ident(table)} SET {set_clauses} "
            f"WHERE {_quote_ident(key_col)} = ?"
        )
        for _, row in results.iterrows():
            values = tuple(_coerce_for_sqlite(row[c]) for c in analysis_cols)
            values += (row[key_col],)
            conn.execute(update_sql, values)
    return None


def _level_spec_from_provenance_or_db(conn: sqlite3.Connection, level: str) -> dict:
    return {"key": f"{level}_id"}


def _recover_add_metadata(conn: sqlite3.Connection, project_dir: Path,
                          entry: dict) -> str | None:
    path, err = _check_source_file(entry.get("metadata_file", ""),
                                    entry.get("metadata_checksum"))
    if err:
        return f"add_metadata({entry.get('level')}): {err}"

    level = entry["level"]
    table = f"{level}s"
    key_col = f"{level}_id"
    metadata = pd.read_csv(path, sep="\t")
    cols = [c for c in metadata.columns if c != key_col]

    with begin_immediate(conn):
        existing_keys = {
            str(r[0]) for r in conn.execute(
                f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)}"
            ).fetchall()
        }
        set_clauses = ", ".join(
            f"{_quote_ident(c)} = COALESCE({_quote_ident(c)}, ?)" for c in cols
        )
        update_sql = (
            f"UPDATE {_quote_ident(table)} SET {set_clauses} "
            f"WHERE {_quote_ident(key_col)} = ?"
        )
        insert_cols = [key_col] + cols
        placeholders = ", ".join("?" * len(insert_cols))
        insert_sql = (
            f"INSERT INTO {_quote_ident(table)} "
            f"({', '.join(_quote_ident(c) for c in insert_cols)}) "
            f"VALUES ({placeholders})"
        )
        for _, row in metadata.iterrows():
            k = str(row[key_col])
            if k in existing_keys:
                values = tuple(_coerce_for_sqlite(row[c]) for c in cols) + (k,)
                conn.execute(update_sql, values)
            else:
                values = tuple(_coerce_for_sqlite(row[c]) for c in insert_cols)
                conn.execute(insert_sql, values)
    return None


def _recover_migrate(conn: sqlite3.Connection, project_dir: Path,
                     entry: dict) -> str | None:
    """Re-run migration from sandbox/source_manifest.tsv preserved at migrate-time."""
    source = project_dir / "sandbox" / "source_manifest.tsv"
    path, err = _check_source_file(str(source), entry.get("source_checksum"))
    if err:
        return f"migrate: {err}"

    df = pd.read_csv(path, sep="\t")
    classifications = entry.get("column_classifications", {})
    # Determine level key columns from the classifications' absence — they are
    # the columns NOT in classifications (since classifications excludes keys).
    # Fallback to the standard names.
    patient_col = "patient_id"
    specimen_col = "specimen_id"
    assay_col = "assay_id"

    with begin_immediate(conn):
        _insert_rows_by_level(
            conn, df, patient_col, specimen_col, assay_col, classifications,
        )
    return None


# ── v0.3 dashboard (nested HTML) ──────────────────────────────────────────────
#
# Nested HTML: cohort summary → one <details> per patient → nested <details>
# per specimen → inline table of assays with completion markers per analysis.
# Self-contained (inline CSS, no JS libs) so it scp's to a laptop and opens
# offline.


def cmd_dashboard_project(args):
    project_dir, schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    output_path = Path(args.output)

    conn = open_project_db(project_dir / PROJECT_DB_NAME)
    qc_info: dict = {}
    try:
        patients = conn.execute("SELECT * FROM patients ORDER BY patient_id").fetchall()
        patient_cols = [d[0] for d in conn.execute(
            "SELECT * FROM patients LIMIT 0"
        ).description]
        specimens = conn.execute("SELECT * FROM specimens ORDER BY specimen_id").fetchall()
        specimen_cols = [d[0] for d in conn.execute(
            "SELECT * FROM specimens LIMIT 0"
        ).description]
        assays = conn.execute("SELECT * FROM assays ORDER BY assay_id").fetchall()
        assay_cols = [d[0] for d in conn.execute(
            "SELECT * FROM assays LIMIT 0"
        ).description]
        done_by_level = _discover_done_columns(conn)

        # v0.4 QC-aware dashboard section (proposal §11). Silently degrades on
        # pre-migrate projects — the info dict just stays empty.
        from casetrack_qc.schema import qc_schema_exists as _qc_schema_exists
        if _qc_schema_exists(conn):
            from casetrack_qc.reader import exclusion_breakdown as _exclusion_breakdown
            qc_info = _exclusion_breakdown(conn)
            qc_info["active_events"] = conn.execute(
                "SELECT level, entity_id, kind, reason, source, created_at "
                "FROM qc_events WHERE resolved_at IS NULL ORDER BY id"
            ).fetchall()
    finally:
        conn.close()

    html_str = _render_v03_dashboard_html(
        project_dir=project_dir,
        schema=schema,
        patients=[dict(zip(patient_cols, r)) for r in patients],
        specimens=[dict(zip(specimen_cols, r)) for r in specimens],
        assays=[dict(zip(assay_cols, r)) for r in assays],
        done_by_level=done_by_level,
        qc_info=qc_info,
    )
    output_path.write_text(html_str)

    total_assays = len(assays)
    total_analyses = sum(len(v) for v in done_by_level.values())
    print(
        f"Dashboard written: {output_path} "
        f"({len(patients)} patients, {len(specimens)} specimens, "
        f"{total_assays} assays, {total_analyses} analyses)"
    )


def _render_v03_dashboard_html(*, project_dir: Path, schema: dict,
                               patients: list, specimens: list, assays: list,
                               done_by_level: dict,
                               qc_info: dict | None = None) -> str:
    esc = html.escape
    project_name = schema["project"].get("name", project_dir.name)
    schema_v = schema["project"].get("schema_v", 1)
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Index children by parent for quick lookup during rendering.
    specimens_by_patient: dict = {}
    for s in specimens:
        specimens_by_patient.setdefault(s["patient_id"], []).append(s)
    assays_by_specimen: dict = {}
    for a in assays:
        assays_by_specimen.setdefault(a["specimen_id"], []).append(a)

    # Per-analysis completion at the ASSAY level (the common case).
    assay_done_cols = done_by_level["assay"]
    per_analysis = []
    for dc in assay_done_cols:
        name = dc[: -len(DONE_COLUMN_SUFFIX)]
        done = sum(1 for a in assays if a.get(dc) is not None)
        pct = (100.0 * done / len(assays)) if assays else 0.0
        per_analysis.append({"name": name, "done": done, "total": len(assays), "pct": pct})

    overall_pct = (
        sum(p["pct"] for p in per_analysis) / len(per_analysis)
        if per_analysis else 0.0
    )

    # Build the body section per patient.
    body_sections = []
    for p in patients:
        pid = p["patient_id"]
        meta_cells = _dashboard_meta_cells(p, patient_key="patient_id")
        p_specimens = specimens_by_patient.get(pid, [])
        # Patient-level analyses done = non-null patient-level `_done` columns.
        p_done = sum(1 for dc in done_by_level["patient"] if p.get(dc))
        p_total = len(done_by_level["patient"])
        p_badge = f'<span class="badge">{p_done}/{p_total} patient-level</span>' if p_total else ""

        spec_html = []
        for sp in p_specimens:
            sid = sp["specimen_id"]
            s_meta = _dashboard_meta_cells(sp, patient_key="specimen_id")
            s_assays = assays_by_specimen.get(sid, [])
            s_done = sum(1 for dc in done_by_level["specimen"] if sp.get(dc))
            s_total = len(done_by_level["specimen"])
            s_badge = (f'<span class="badge">{s_done}/{s_total} specimen-level</span>'
                       if s_total else "")

            # Assay table: assay_id + per-analysis check/blank.
            table_rows = []
            for a in s_assays:
                cells = []
                for dc in assay_done_cols:
                    if a.get(dc) is not None:
                        cells.append(f'<td class="done" title="{esc(str(a[dc]))}">✓</td>')
                    else:
                        cells.append('<td class="missing"></td>')
                table_rows.append(
                    f'<tr><td class="id">{esc(a["assay_id"])}</td>'
                    f'<td class="small muted">{esc(str(a.get("assay_type","")))}</td>'
                    + "".join(cells) + "</tr>"
                )
            headers = "".join(
                f'<th class="vtext">{esc(dc[: -len(DONE_COLUMN_SUFFIX)])}</th>'
                for dc in assay_done_cols
            )
            n_assay_analyses = len(assay_done_cols) or 1
            n_done_here = sum(
                1 for a in s_assays for dc in assay_done_cols if a.get(dc) is not None
            )
            s_pct = (100.0 * n_done_here /
                     (len(s_assays) * n_assay_analyses)) if s_assays else 0.0
            assay_table = (
                f'<table class="assays"><thead><tr>'
                f'<th>Assay</th><th>Type</th>{headers}</tr></thead>'
                f'<tbody>{"".join(table_rows)}</tbody></table>'
                if s_assays else '<p class="muted">no assays</p>'
            )
            spec_html.append(
                f'<details class="specimen"><summary>'
                f'<strong>{esc(sid)}</strong>'
                f'<span class="muted small"> ({len(s_assays)} assays, {s_pct:.0f}% done) </span>'
                f'{s_badge}</summary>'
                f'<div class="meta">{s_meta}</div>{assay_table}</details>'
            )

        body_sections.append(
            f'<details class="patient" open><summary>'
            f'<strong>{esc(pid)}</strong>'
            f'<span class="muted small"> ({len(p_specimens)} specimens) </span>'
            f'{p_badge}</summary>'
            f'<div class="meta">{meta_cells}</div>'
            + ("".join(spec_html) if spec_html else '<p class="muted">no specimens</p>')
            + '</details>'
        )

    # Per-analysis progress bars.
    per_analysis_html = []
    for a in per_analysis:
        bar_w = max(0, min(100, int(a["pct"])))
        per_analysis_html.append(
            f'<div class="analysis-row">'
            f'<div class="name">{esc(a["name"])}</div>'
            f'<div class="bar"><div style="width:{bar_w}%"></div></div>'
            f'<div class="pct">{a["pct"]:.1f}%</div>'
            f'<div class="count">{a["done"]}/{a["total"]}</div>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>casetrack dashboard — {esc(project_name)}</title>
<style>
  :root {{
    --done: #2f855a; --done-bg: #c6f6d5;
    --missing: #a0aec0; --missing-bg: #edf2f7;
    --fg: #1a202c; --muted: #4a5568; --border: #e2e8f0;
    --accent: #2b6cb0; --panel: #ffffff;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
         Arial, sans-serif; color: var(--fg); margin: 0; padding: 24px;
         background: #fafbfc; }}
  h1 {{ margin: 0 0 4px 0; font-size: 22px; }}
  h2 {{ margin: 28px 0 12px 0; font-size: 16px;
        border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
  .muted {{ color: var(--muted); }}
  .small {{ font-size: 12px; }}
  .metrics {{ display: flex; gap: 32px; margin: 16px 0 12px 0; flex-wrap: wrap; }}
  .metric .value {{ font-size: 24px; font-weight: 600; }}
  .metric .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase;
                   letter-spacing: 0.05em; }}
  .bar {{ background: var(--missing-bg); border-radius: 4px; overflow: hidden;
          height: 14px; flex: 1; }}
  .bar > div {{ background: var(--done); height: 100%; }}
  .analysis-row {{ display: flex; align-items: center; gap: 12px; margin: 6px 0; }}
  .analysis-row .name {{ width: 200px; font-family: ui-monospace, monospace; font-size: 13px; }}
  .analysis-row .pct {{ width: 60px; text-align: right; font-variant-numeric: tabular-nums; }}
  .analysis-row .count {{ width: 80px; text-align: right; color: var(--muted);
                          font-variant-numeric: tabular-nums; font-size: 12px; }}
  details.patient {{ background: var(--panel); border: 1px solid var(--border);
                     border-radius: 4px; margin: 10px 0; padding: 10px 14px; }}
  details.patient > summary {{ cursor: pointer; list-style: none; font-size: 15px; }}
  details.patient > summary::-webkit-details-marker {{ display: none; }}
  details.patient > summary::before {{ content: "▸ "; color: var(--accent);
                                        display: inline-block; width: 1em; }}
  details.patient[open] > summary::before {{ content: "▾ "; }}
  details.specimen {{ margin: 6px 0 6px 18px; padding: 6px 10px;
                      border-left: 2px solid var(--border); background: #fbfcfd; }}
  details.specimen > summary {{ cursor: pointer; list-style: none; }}
  details.specimen > summary::-webkit-details-marker {{ display: none; }}
  details.specimen > summary::before {{ content: "▸ "; color: var(--accent); }}
  details.specimen[open] > summary::before {{ content: "▾ "; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 4px 0 8px 18px;
           color: var(--muted); font-size: 12px; }}
  .meta span {{ white-space: nowrap; }}
  .meta b {{ color: var(--fg); font-weight: 500; }}
  .qc-chips {{ margin: 4px 0 12px 0; display: flex; gap: 8px; flex-wrap: wrap; }}
  .qc-chip {{ padding: 2px 10px; border-radius: 10px; font-size: 12px;
              font-weight: 600; }}
  .qc-chip-red {{ background: #fed7d7; color: #742a2a; }}
  .qc-chip-amber {{ background: #feebc8; color: #7b341e; }}
  .qc-chip-grey {{ background: var(--missing-bg); color: var(--muted); }}
  table.qc-events {{ border-collapse: collapse; width: 100%; font-size: 12px;
                     margin: 8px 0 20px 0; }}
  table.qc-events th, table.qc-events td {{ padding: 4px 8px; text-align: left;
                                             border-bottom: 1px solid var(--border); }}
  table.qc-events th {{ background: #f7fafc; font-weight: 600; }}
  table.assays {{ border-collapse: collapse; margin: 4px 0 4px 18px;
                  font-size: 12px; }}
  table.assays th {{ padding: 6px 10px; text-align: left;
                     border-bottom: 1px solid var(--border); font-weight: 600; }}
  table.assays th.vtext {{ writing-mode: vertical-rl; text-orientation: mixed;
                           padding: 10px 4px; min-height: 70px; }}
  table.assays td {{ padding: 4px 10px; border-bottom: 1px solid #f1f5f9; }}
  table.assays td.id {{ font-family: ui-monospace, monospace; }}
  table.assays td.done {{ background: var(--done-bg); color: var(--done);
                          text-align: center; font-weight: 600; }}
  table.assays td.missing {{ background: var(--missing-bg); text-align: center; }}
  .badge {{ display: inline-block; background: var(--missing-bg); color: var(--muted);
            padding: 1px 8px; border-radius: 10px; font-size: 11px; margin-left: 8px; }}
</style>
</head>
<body>
  <h1>{esc(project_name)}</h1>
  <div class="muted small">
    Project dir: <code>{esc(str(project_dir))}</code>
    · schema_v {schema_v} · generated {esc(generated_at)}
  </div>

  <div class="metrics">
    <div class="metric"><div class="value">{len(patients)}</div>
      <div class="label">Patients</div></div>
    <div class="metric"><div class="value">{len(specimens)}</div>
      <div class="label">Specimens</div></div>
    <div class="metric"><div class="value">{len(assays)}</div>
      <div class="label">Assays</div></div>
    <div class="metric"><div class="value">{overall_pct:.1f}%</div>
      <div class="label">Avg analysis completion</div></div>
  </div>

  {_qc_chips_html(qc_info)}

  <h2>Per-analysis completion (assay level)</h2>
  {"".join(per_analysis_html) if per_analysis_html
     else '<p class="muted">No assay-level analyses recorded yet.</p>'}

  {_qc_excluded_html(qc_info)}

  <h2>Patients</h2>
  {"".join(body_sections) if body_sections
     else '<p class="muted">No patients registered yet.</p>'}
</body></html>
"""


def _qc_chips_html(qc_info: dict | None) -> str:
    if not qc_info:
        return ""
    esc = html.escape
    parts = []
    nq = len(qc_info.get("qc_failed_assays", []))
    nc = len(qc_info.get("censored_assays", []))
    nr = len(qc_info.get("consent_revoked_patients", []))
    if nr:
        parts.append(f'<span class="qc-chip qc-chip-red">{nr} consent-revoked</span>')
    if nq:
        parts.append(f'<span class="qc-chip qc-chip-amber">{nq} QC-failed</span>')
    if nc:
        parts.append(f'<span class="qc-chip qc-chip-grey">{nc} censored</span>')
    if not parts:
        return ""
    return f'<div class="qc-chips">{"".join(parts)}</div>'


def _qc_excluded_html(qc_info: dict | None) -> str:
    if not qc_info:
        return ""
    esc = html.escape
    events = qc_info.get("active_events") or []
    if not events:
        return ""
    rows = []
    for level, entity_id, kind, reason, source, created_at in events:
        rows.append(
            "<tr>"
            f"<td>{esc(level)}</td><td class='id'>{esc(entity_id)}</td>"
            f"<td>{esc(kind)}</td><td class='muted small'>{esc(source)}</td>"
            f"<td class='small'>{esc(str(created_at))}</td>"
            f"<td class='muted small'>{esc(reason)}</td>"
            "</tr>"
        )
    return (
        '<h2>Excluded (active QC events)</h2>'
        '<table class="qc-events">'
        '<thead><tr><th>Level</th><th>Entity</th><th>Kind</th>'
        '<th>Source</th><th>Created</th><th>Reason</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _dashboard_meta_cells(row: dict, *, patient_key: str) -> str:
    """Render the non-key metadata fields of a row as inline <span> cells.
    Skips `_done` columns (those belong to the analysis completion story)."""
    esc = html.escape
    cells = []
    for k, v in row.items():
        if k == patient_key or k.endswith(DONE_COLUMN_SUFFIX):
            continue
        if v is None or v == "":
            continue
        cells.append(f"<span><b>{esc(k)}:</b> {esc(str(v))}</span>")
    return "".join(cells) if cells else '<span class="muted">(no metadata)</span>'


# ── v0.3 query (DuckDB ATTACH of casetrack.db) ────────────────────────────────
#
# Uses duckdb's sqlite_scanner (bundled in modern duckdb) — no external install
# step needed. The casetrack.db is attached READ_ONLY so a running query can
# never corrupt a live writer's WAL.


def _prepare_v03_query_connection(db_path: Path):
    """Open a DuckDB connection with casetrack.db attached + helper views.

    Views published in the default (in-memory) catalog:
      - patients, specimens, assays  (pass-through of each SQLite table)
      - _  = assays ⋈ specimens ⋈ patients (inner join, one row per assay)
    """
    import duckdb as _duckdb
    con = _duckdb.connect(":memory:")
    try:
        con.execute("INSTALL sqlite")
    except Exception:
        # Install may fail in air-gapped environments — extension may
        # already be shipped. LOAD will surface any real issue next.
        pass
    con.execute("LOAD sqlite")
    con.execute(f"ATTACH '{_sql_escape(str(db_path))}' AS proj (TYPE SQLITE, READ_ONLY)")
    for level in LEVEL_ORDER:
        table = f"{level}s"
        con.execute(
            f"CREATE VIEW {_quote_ident(table)} AS "
            f"SELECT * FROM proj.{_quote_ident(table)}"
        )
    con.execute("""
        CREATE VIEW "_" AS
        SELECT *
        FROM assays
        JOIN specimens USING (specimen_id)
        JOIN patients USING (patient_id)
    """)
    # v0.4: expose `_active` — same join with the §4.4 cascade applied. Silent
    # no-op on v0.3-only DBs that don't have qc_status/consent_status columns.
    from casetrack_qc.reader import install_active_views as _install_active_views
    _install_active_views(con)
    return con


def cmd_query_project(args):
    try:
        import duckdb as _duckdb  # noqa: F401
    except ImportError:
        print(
            "Error: duckdb is missing but is a required dependency of casetrack.\n"
            "Your install is broken — reinstall with: pip install --force-reinstall casetrack",
            file=sys.stderr,
        )
        sys.exit(1)

    project_dir, _schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    db_path = project_dir / PROJECT_DB_NAME
    con = _prepare_v03_query_connection(db_path)
    try:
        try:
            cursor = con.execute(args.sql)
        except Exception as e:
            print(f"Error: SQL failed — {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(2)
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description]
    finally:
        con.close()

    _emit_query_rows(rows, cols, fmt=args.fmt, output=args.output)


def _emit_query_rows(rows, cols, *, fmt: str, output: str | None) -> None:
    """Render query results to stdout or `output` in the chosen format."""
    import io

    if fmt == "json":
        dict_rows = [dict(zip(cols, r)) for r in rows]
        text = json.dumps(dict_rows, indent=2, default=str)
    elif fmt in ("tsv", "csv"):
        sep = "\t" if fmt == "tsv" else ","
        buf = io.StringIO()
        buf.write(sep.join(cols) + "\n")
        for r in rows:
            buf.write(sep.join("" if v is None else str(v) for v in r) + "\n")
        text = buf.getvalue()
    else:  # table
        widths = [max(len(c), *(len(str(v if v is not None else "")) for v in col_vals))
                  for c, col_vals in zip(cols, zip(*rows) if rows else [[] for _ in cols])]
        lines = [
            "  ".join(c.ljust(w) for c, w in zip(cols, widths)),
            "  ".join("-" * w for w in widths),
        ]
        for r in rows:
            lines.append("  ".join(
                ("" if v is None else str(v)).ljust(w) for v, w in zip(r, widths)
            ))
        text = "\n".join(lines)

    if output:
        Path(output).write_text(text if text.endswith("\n") else text + "\n")
    else:
        print(text)


# ── v0.3 export ────────────────────────────────────────────────────────────────
#
# `casetrack export --project-dir D --output OUT [--shape {tables,joined}]
#   [--sql "SELECT ..."]`
#
# shape=tables (default): writes one file per level table under OUT/ (or
#   OUT.{patient,specimen,assay}.<ext> if OUT is a file prefix). Format is
#   inferred from the extension, with a directory-or-prefix override.
# shape=joined: writes the assays⋈specimens⋈patients join to a single file.
# --sql: run arbitrary SQL, write the result (overrides shape).
# --tables: restrict shape=tables to a subset, e.g. --tables patients,assays.
#
# Same format matrix as flat export: .tsv, .csv, .json, .xlsx, .parquet.


def cmd_export_project(args):
    project_dir, _schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    output = Path(args.output)

    # --sql short-circuits shape / tables.
    if args.sql:
        con = _prepare_v03_query_connection(project_dir / PROJECT_DB_NAME)
        try:
            cursor = con.execute(args.sql)
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        finally:
            con.close()
        df = pd.DataFrame(rows, columns=cols)
        _write_df(df, output)
        print(f"Exported {len(df)} rows via --sql to {output}.")
        return

    # v0.4: figure out QC filtering up-front so the audit line matches the
    # subset that actually got written (proposal §5.2).
    include_censored = getattr(args, "include_censored", False)
    include_consent_revoked = getattr(args, "include_consent_revoked", False)
    active_ids: dict[str, set[str] | None] = {"patient": None, "specimen": None, "assay": None}
    excluded_count = 0
    _qc_conn = open_project_db(project_dir / PROJECT_DB_NAME)
    try:
        from casetrack_qc.schema import qc_schema_exists as _qc_schema_exists
        from casetrack_qc.reader import (
            active_assay_ids as _active_assay_ids,
            active_patient_ids as _active_patient_ids,
            active_specimen_ids as _active_specimen_ids,
            exclusion_breakdown as _exclusion_breakdown,
        )
        if _qc_schema_exists(_qc_conn) and not (
            include_censored and include_consent_revoked
        ):
            active_ids = {
                "patient": _active_patient_ids(
                    _qc_conn,
                    include_censored=include_censored,
                    include_consent_revoked=include_consent_revoked,
                ),
                "specimen": _active_specimen_ids(
                    _qc_conn,
                    include_censored=include_censored,
                    include_consent_revoked=include_consent_revoked,
                ),
                "assay": _active_assay_ids(
                    _qc_conn,
                    include_censored=include_censored,
                    include_consent_revoked=include_consent_revoked,
                ),
            }
            breakdown = _exclusion_breakdown(_qc_conn)
            filt_msg = []
            if not include_censored:
                nq = len(breakdown["qc_failed_assays"]) + len(breakdown["censored_assays"])
                if nq:
                    filt_msg.append(f"{nq} QC-failed/censored assay(s)")
            if not include_consent_revoked:
                nc = len(breakdown["consent_revoked_assays"])
                if nc:
                    filt_msg.append(f"{nc} consent-revoked assay(s)")
            if filt_msg:
                print(
                    f"\u26A0 export filter: excluded " + ", ".join(filt_msg)
                    + "  (--include-censored / --include-consent-revoked to opt in)",
                    file=sys.stderr,
                )
    finally:
        _qc_conn.close()

    shape = args.shape or "tables"
    if shape == "joined":
        con = _prepare_v03_query_connection(project_dir / PROJECT_DB_NAME)
        try:
            # Use the _active view when filtering, the raw _ view otherwise.
            if any(a is not None for a in active_ids.values()):
                from casetrack_qc.reader import install_active_views as _install_views
                _install_views(con)
                try:
                    df = con.execute('SELECT * FROM "_active"').fetchdf()
                except Exception:
                    df = con.execute('SELECT * FROM "_"').fetchdf()
            else:
                df = con.execute('SELECT * FROM "_"').fetchdf()
        finally:
            con.close()
        _write_df(df, output)
        print(f"Exported joined view ({len(df)} rows) to {output}.")
        return

    if shape != "tables":
        print(f"Error: unknown --shape {shape!r}", file=sys.stderr)
        sys.exit(1)

    # --tables accepts either table names (plural) or level names (singular).
    # Normalize to level names for the loop.
    _plural_to_level = {f"{lv}s": lv for lv in LEVEL_ORDER}
    _valid = set(LEVEL_ORDER) | set(_plural_to_level)
    if args.tables:
        wanted = [w.strip() for w in args.tables.split(",") if w.strip()]
    else:
        wanted = list(LEVEL_ORDER)
    normalized = []
    for name in wanted:
        if name not in _valid:
            print(
                f"Error: --tables contains unknown name {name!r}; "
                f"must be one of {sorted(_valid)}",
                file=sys.stderr,
            )
            sys.exit(1)
        normalized.append(_plural_to_level.get(name, name))
    wanted = normalized

    # If the output path has a known extension, treat it as a prefix.
    # Otherwise treat it as a directory.
    prefix_mode = output.suffix in {".tsv", ".csv", ".json", ".xlsx", ".parquet"}
    if prefix_mode:
        prefix_base = output.with_suffix("")
        ext = output.suffix
    else:
        output.mkdir(parents=True, exist_ok=True)
        prefix_base = None
        ext = ".tsv"

    include_lineage = getattr(args, "include_lineage", False)
    # Auto-enable lineage export for XLSX — multi-sheet is the natural fit.
    if ext == ".xlsx" and not args.sql and shape != "joined":
        include_lineage = True

    conn = open_project_db(project_dir / PROJECT_DB_NAME)
    try:
        written = []
        for level in wanted:
            table = f"{level}s"
            key_col = f"{level}_id"
            # v0.4 filter: if `active_ids[level]` is set, restrict the export
            # to those keys.
            active = active_ids.get(level)
            if active is None:
                df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
            elif not active:
                df = pd.read_sql_query(
                    f"SELECT * FROM {table} WHERE 1=0", conn
                )
            else:
                placeholders = ", ".join("?" * len(active))
                df = pd.read_sql_query(
                    f"SELECT * FROM {table} WHERE {key_col} IN ({placeholders})",
                    conn,
                    params=list(active),
                )
            if prefix_mode:
                out_path = Path(f"{prefix_base}.{table}{ext}")
            else:
                out_path = output / f"{table}{ext}"
            _write_df(df, out_path)
            written.append((table, out_path, len(df)))

        # v0.6 lineage tables — assay_sources + batches.
        if include_lineage:
            existing_tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            for extra_table in ("batches", "assay_sources"):
                if extra_table not in existing_tables:
                    continue
                df_extra = pd.read_sql_query(f"SELECT * FROM {extra_table}", conn)
                if prefix_mode:
                    out_path = Path(f"{prefix_base}.{extra_table}{ext}")
                else:
                    out_path = output / f"{extra_table}{ext}"
                _write_df(df_extra, out_path)
                written.append((extra_table, out_path, len(df_extra)))
    finally:
        conn.close()

    # For XLSX: rewrite all sheets into a single workbook if multiple files
    # were written to the same .xlsx path (prefix_mode + XLSX).
    if prefix_mode and ext == ".xlsx" and len(written) > 1:
        xlsx_path = output  # the user-supplied path, e.g. export.xlsx
        try:
            import openpyxl  # noqa: F401
            with pd.ExcelWriter(xlsx_path, engine="openpyxl") as ew:
                for table, tpath, _ in written:
                    df_sheet = pd.read_csv(str(tpath).replace(".xlsx", ".tsv"),
                                           sep="\t") if not tpath.exists() else \
                               pd.read_excel(tpath)
                    df_sheet.to_excel(ew, sheet_name=table[:31], index=False)
            # Remove individual per-table xlsx files — single workbook is canonical.
            for table, tpath, _ in written:
                if tpath != xlsx_path and tpath.exists():
                    tpath.unlink()
            print(f"Exported {len(written)} sheet(s) to {xlsx_path}:")
            for table, _, n in written:
                print(f"  {table}: {n} rows")
            return
        except ImportError:
            print("Error: openpyxl required for .xlsx; pip install 'casetrack[excel]'",
                  file=sys.stderr)
            sys.exit(1)

    for table, path, n in written:
        print(f"  {table}: {n} rows → {path}")
    print(f"Exported {len(written)} table(s).")


def _write_df(df: "pd.DataFrame", output: Path) -> None:
    """Write `df` to `output`, inferring format from the extension."""
    ext = output.suffix.lower()
    output.parent.mkdir(parents=True, exist_ok=True)
    if ext == ".tsv":
        df.to_csv(output, sep="\t", index=False)
    elif ext == ".csv":
        df.to_csv(output, index=False)
    elif ext == ".json":
        df.to_json(output, orient="records", indent=2)
    elif ext == ".xlsx":
        try:
            df.to_excel(output, index=False)
        except ImportError:
            print("Error: openpyxl required for .xlsx; pip install 'casetrack[excel]'",
                  file=sys.stderr)
            sys.exit(1)
    elif ext == ".parquet":
        try:
            df.to_parquet(output, index=False)
        except ImportError:
            print("Error: pyarrow required for .parquet; pip install 'casetrack[parquet]'",
                  file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Error: unsupported output extension {ext!r}", file=sys.stderr)
        sys.exit(1)


# ── v0.3 rerun (assays missing an analysis) ───────────────────────────────────


def _source_assays_for_keys(
    conn: sqlite3.Connection,
    keys: list[str],
    level: str,
) -> list[str]:
    """Return deduplicated source_assay_ids from assay_sources for the given keys.

    Handles both lineage modes:
      - Mode B: keys are specimen IDs  (consumer_specimen_id column)
      - Mode A: keys are assay IDs     (merged_assay_id column)
    Assay-level keys are tried against both columns.
    """
    if not keys:
        return []
    placeholders = ", ".join("?" * len(keys))
    if level == "specimen":
        rows = conn.execute(
            f"SELECT DISTINCT source_assay_id FROM assay_sources "
            f"WHERE consumer_specimen_id IN ({placeholders})",
            keys,
        ).fetchall()
    elif level == "assay":
        rows = conn.execute(
            f"SELECT DISTINCT source_assay_id FROM assay_sources "
            f"WHERE merged_assay_id IN ({placeholders})",
            keys,
        ).fetchall()
    else:
        rows = []
    return [r[0] for r in rows]


def cmd_rerun_project(args):
    project_dir, schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    if not args.list_only and not args.script:
        print("Error: --script is required unless --list-only is used.", file=sys.stderr)
        sys.exit(1)
    level = args.level or _default_analysis_level(schema)
    if level not in LEVEL_ORDER:
        print(f"Error: --level must be one of {list(LEVEL_ORDER)}, got {level!r}",
              file=sys.stderr)
        sys.exit(1)

    level_spec = schema["levels"][level]
    table = f"{level}s"
    key_col = level_spec["key"]
    done_col = f"{args.analysis}{DONE_COLUMN_SUFFIX}"

    conn = open_project_db(project_dir / PROJECT_DB_NAME)
    try:
        actual_cols = set(_get_table_columns(conn, table))
        if done_col not in actual_cols:
            # Analysis has never been run — every row is "missing".
            missing_keys = [
                r[0] for r in conn.execute(
                    f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)} "
                    f"ORDER BY {_quote_ident(key_col)}"
                ).fetchall()
            ]
        else:
            missing_keys = [
                r[0] for r in conn.execute(
                    f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)} "
                    f"WHERE {_quote_ident(done_col)} IS NULL "
                    f"ORDER BY {_quote_ident(key_col)}"
                ).fetchall()
            ]

        # v0.4: filter out censored / consent-revoked by default. --force-censored
        # opts back in and prints a loud stderr warning (proposal §5.2).
        from casetrack_qc.schema import qc_schema_exists as _qc_schema_exists
        force_censored = getattr(args, "force_censored", False)
        if not force_censored and _qc_schema_exists(conn) and level == "assay":
            from casetrack_qc.reader import active_assay_ids as _active_assay_ids
            active = _active_assay_ids(conn)
            before = len(missing_keys)
            missing_keys = [k for k in missing_keys if k in active]
            skipped = before - len(missing_keys)
            if skipped:
                print(
                    f"\u26A0 Skipped {skipped} censored/consent-revoked "
                    f"{level}(s). Re-run with --force-censored to include.",
                    file=sys.stderr,
                )
        elif force_censored and _qc_schema_exists(conn):
            print(
                "\u26A0 --force-censored: including censored/consent-revoked "
                f"{level}s.",
                file=sys.stderr,
            )
    finally:
        conn.close()

    # v0.6: --include-sources — also collect source assays from assay_sources.
    source_keys: list[str] = []
    if getattr(args, "include_sources", False) and missing_keys:
        conn2 = open_project_db(project_dir / PROJECT_DB_NAME)
        try:
            tables_in_db = {r[0] for r in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "assay_sources" in tables_in_db:
                source_keys = _source_assays_for_keys(conn2, missing_keys, level)
        finally:
            conn2.close()

    if not missing_keys and not source_keys:
        print(f"No {level}s missing analysis {args.analysis!r}.")
        return

    if args.list_only:
        for k in missing_keys:
            print(k)
        if source_keys:
            print(f"\n# source assays ({len(source_keys)}):")
            for k in source_keys:
                print(k)
        return

    extra = f" {args.extra}" if args.extra else ""
    submitted = 0
    failed = 0
    all_jobs = list(missing_keys)
    if source_keys:
        print(f"# {len(source_keys)} source assay(s) added via --include-sources",
              file=sys.stderr)
        all_jobs += source_keys
    for k in all_jobs:
        cmd = f"sbatch --export=ALL,{key_col.upper()}={k},PROJECT_DIR={project_dir} {args.script}{extra}"
        if args.submit:
            rc = subprocess_run(cmd)
            if rc == 0:
                submitted += 1
            else:
                failed += 1
        else:
            print(cmd)

    if args.submit:
        print(f"Submitted {submitted} job(s); {failed} failed." if failed
              else f"Submitted {submitted} job(s).")
        if failed:
            sys.exit(1)


def subprocess_run(cmd: str) -> int:
    """Run a shell command, return its exit code. Thin wrapper for testability."""
    import subprocess
    return subprocess.run(cmd, shell=True).returncode


# ── v0.3 add-metadata (bulk UPDATE / INSERT) ──────────────────────────────────
#
# `casetrack add-metadata --project-dir DIR --level LEVEL --metadata TSV`
# is the bulk companion to `register`. The TSV must start with the level's
# key column; remaining columns are treated as metadata to merge into the
# target table under fill-only (default) or --overwrite semantics.
#
# Rows whose key doesn't exist require `--allow-new --yes` to insert. Every
# inserted row must also carry a valid parent_key; we do not create parent
# stubs from add-metadata (use `register --allow-new-parent` for that).


class _MetadataRouting(Exception):
    """Raised inside the add-metadata transaction on missing keys without
    --allow-new, so begin_immediate rolls back any schema mutations."""

    def __init__(self, missing_keys: set, missing_parents: set):
        self.missing_keys = missing_keys
        self.missing_parents = missing_parents
        super().__init__(
            f"{len(missing_keys)} missing key(s), "
            f"{len(missing_parents)} missing parent(s)"
        )


def cmd_add_metadata_project(args):
    project_dir, schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    from casetrack_lifecycle.gate import assert_not_archived as _assert_not_archived
    _assert_not_archived(
        project_dir,
        force_archived=getattr(args, "force_archived", False),
        yes=getattr(args, "yes", False),
    )
    level = args.level
    if level not in LEVEL_ORDER:
        print(
            f"Error: --level must be one of {list(LEVEL_ORDER)}, got {level!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.overwrite and args.fill_only:
        print("Error: --overwrite and --fill-only are mutually exclusive.", file=sys.stderr)
        sys.exit(1)
    if args.allow_new and not args.yes:
        print(
            "Error: --allow-new requires --yes to commit inserts.",
            file=sys.stderr,
        )
        sys.exit(1)

    metadata_path = Path(args.metadata)
    if not metadata_path.exists():
        print(f"Error: metadata file not found: {metadata_path}", file=sys.stderr)
        sys.exit(1)

    level_spec = schema["levels"][level]
    table = f"{level}s"
    key_col = level_spec["key"]
    parent_key_col = level_spec.get("parent_key")
    parent_level = level_spec.get("parent")

    metadata = pd.read_csv(metadata_path, sep="\t")
    if key_col not in metadata.columns:
        print(
            f"Error: key column {key_col!r} not in {metadata_path}. "
            f"Columns: {list(metadata.columns)}",
            file=sys.stderr,
        )
        sys.exit(1)

    meta_cols = [c for c in metadata.columns if c != key_col]
    if not meta_cols:
        print(
            f"Error: {metadata_path} has no columns besides {key_col!r}.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate every column is declared in the schema.
    declared_cols = set(level_spec["columns"])
    unknown = [c for c in meta_cols if c not in declared_cols]
    if unknown:
        print(
            f"Error: columns not declared in casetrack.toml: {unknown}\n"
            f"Declare them under [levels.{level}.columns] first, or use "
            f"`casetrack append` (which auto-adds analysis columns).",
            file=sys.stderr,
        )
        sys.exit(1)

    # If inserting, the parent FK column must be in the TSV for specimen/assay.
    if args.allow_new and parent_key_col and parent_key_col not in metadata.columns:
        print(
            f"Error: --allow-new at --level {level} requires column "
            f"{parent_key_col!r} in {metadata_path} (parent FK).",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = project_dir / PROJECT_DB_NAME
    conn = open_project_db(db_path)
    executed_sql: list[str] = []
    n_updated = 0
    n_inserted = 0

    try:
        with begin_immediate(conn):
            existing_keys = {
                str(r[0]) for r in conn.execute(
                    f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)}"
                ).fetchall()
            }
            tsv_keys = metadata[key_col].astype(str).tolist()
            tsv_key_set = set(tsv_keys)
            missing_keys = tsv_key_set - existing_keys

            # If we're inserting, validate parents first.
            missing_parents: set = set()
            if missing_keys and args.allow_new and parent_level:
                new_rows = metadata[metadata[key_col].astype(str).isin(missing_keys)]
                parent_col = parent_key_col
                parents_needed = set(new_rows[parent_col].astype(str))
                parent_table = f"{parent_level}s"
                parents_have = {
                    str(r[0]) for r in conn.execute(
                        f"SELECT {_quote_ident(parent_col)} FROM {_quote_ident(parent_table)}"
                    ).fetchall()
                }
                missing_parents = parents_needed - parents_have

            if missing_keys and not args.allow_new:
                raise _MetadataRouting(missing_keys, set())
            if missing_parents:
                raise _MetadataRouting(set(), missing_parents)

            # UPDATEs for existing keys.
            update_keys = [k for k in tsv_keys if k in existing_keys]
            if update_keys:
                if args.overwrite:
                    set_clauses = ", ".join(f"{_quote_ident(c)} = ?" for c in meta_cols)
                else:
                    # Default = fill-only (matches v0.2 add-metadata default).
                    set_clauses = ", ".join(
                        f"{_quote_ident(c)} = COALESCE({_quote_ident(c)}, ?)"
                        for c in meta_cols
                    )
                update_sql = (
                    f"UPDATE {_quote_ident(table)} SET {set_clauses} "
                    f"WHERE {_quote_ident(key_col)} = ?"
                )
                executed_sql.append(update_sql)
                idx_by_key = {
                    str(metadata.iloc[i][key_col]): i for i in range(len(metadata))
                }
                for k in update_keys:
                    row = metadata.iloc[idx_by_key[k]]
                    values = tuple(_coerce_for_sqlite(row[c]) for c in meta_cols)
                    values += (k,)
                    conn.execute(update_sql, values)
                    n_updated += 1

            # INSERTs for new keys.
            if args.allow_new and missing_keys:
                # v0.6: validate every new key against the schema's format
                # rules (proposal 0005 Part A) before the INSERT runs, so
                # malformed IDs surface at the source of the problem.
                for new_key in missing_keys:
                    validate_hierarchy_id(new_key, schema, level)
                # Also validate parent keys we're about to reference — if
                # missing_parents was non-empty, we've already failed above;
                # here we only reach valid parents, but their case-variants
                # still need checking.
                if parent_level:
                    for parent_id in parents_needed:
                        validate_hierarchy_id(parent_id, schema, parent_level)
                # Case-variant check against existing rows, once per batch.
                folded_existing = _preload_folded_ids(conn, schema, level)
                for new_key in missing_keys:
                    check_id_case_unique(
                        conn, schema, level, new_key, folded_existing
                    )
                new_rows = metadata[metadata[key_col].astype(str).isin(missing_keys)]
                insert_cols = [key_col] + meta_cols
                quoted_cols = ", ".join(_quote_ident(c) for c in insert_cols)
                placeholders = ", ".join("?" * len(insert_cols))
                insert_sql = (
                    f"INSERT INTO {_quote_ident(table)} "
                    f"({quoted_cols}) VALUES ({placeholders})"
                )
                executed_sql.append(insert_sql)
                for _, row in new_rows.iterrows():
                    values = tuple(
                        _coerce_for_sqlite(row[c]) for c in insert_cols
                    )
                    conn.execute(insert_sql, values)
                    n_inserted += 1

    except _MetadataRouting as e:
        conn.close()
        if e.missing_keys:
            preview = sorted(e.missing_keys)[:5]
            print(
                f"Error: {len(e.missing_keys)} key(s) in {metadata_path} do not "
                f"exist in table {table!r}: {preview}"
                f"{'…' if len(e.missing_keys) > 5 else ''}\n"
                f"Pass --allow-new --yes to create new rows.",
                file=sys.stderr,
            )
        else:
            preview = sorted(e.missing_parents)[:5]
            print(
                f"Error: {len(e.missing_parents)} parent {parent_level}(s) not found: "
                f"{preview}{'…' if len(e.missing_parents) > 5 else ''}\n"
                f"Register them first with `casetrack register --level "
                f"{parent_level} --id ...`.",
                file=sys.stderr,
            )
        sys.exit(2)
    except ValueError as e:
        # Raised by validate_hierarchy_id / check_id_case_unique when a new
        # key violates the schema's format rules (proposal 0005 Part A).
        conn.close()
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.IntegrityError as e:
        conn.close()
        print(f"Error: add-metadata aborted — {type(e).__name__}: {e}",
              file=sys.stderr)
        sys.exit(1)
    finally:
        if conn:
            conn.close()

    log_project_provenance(project_dir, {
        "action": "add_metadata",
        "level": level,
        "metadata_file": str(metadata_path),
        "metadata_checksum": _checksum(str(metadata_path)),
        "columns": meta_cols,
        "rows_updated": n_updated,
        "rows_inserted": n_inserted,
        "mode": "overwrite" if args.overwrite else ("fill_only" if args.fill_only else "fill_only"),
        "transaction_id": _new_transaction_id(),
        "sql": executed_sql,
        "schema_v_before": schema["project"]["schema_v"],
        "schema_v_after": schema["project"]["schema_v"],
    })

    print(
        f"add-metadata → {level}: "
        f"updated={n_updated}, inserted={n_inserted}, columns={len(meta_cols)}."
    )


# ── v0.3 status ────────────────────────────────────────────────────────────────
#
# Implements `casetrack status --project-dir` per proposal 0001 §7.1. Four
# group-by modes — analysis (default), assay, specimen, patient — all driven
# by discovering `_done` columns on each table and querying via SQL.


def _discover_done_columns(conn: sqlite3.Connection) -> dict:
    """Return {level: [done_col, ...]} for every `_done` column on each table."""
    out: dict = {}
    for level in LEVEL_ORDER:
        table = f"{level}s"
        out[level] = [
            c for c in _get_table_columns(conn, table)
            if c.endswith(DONE_COLUMN_SUFFIX)
        ]
    return out


def _emit_lineage_section(conn: sqlite3.Connection) -> None:
    """Print a lineage summary table if assay_sources exists and has rows."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "assay_sources" not in tables:
        print("\n[lineage] assay_sources table not found — run `casetrack migrate-lineage` first.")
        return
    (n_rows,) = conn.execute("SELECT COUNT(*) FROM assay_sources").fetchone()
    if n_rows == 0:
        print("\n[lineage] No source links recorded yet. Use `casetrack link-sources` to add them.")
        return

    print(f"\n── Assay lineage ({n_rows} link(s)) " + "─" * 40)

    # Mode B: assay → specimen
    mode_b = conn.execute(
        "SELECT consumer_specimen_id, source_assay_id "
        "FROM assay_sources WHERE consumer_specimen_id IS NOT NULL "
        "ORDER BY consumer_specimen_id, source_assay_id"
    ).fetchall()
    if mode_b:
        print("\n  Mode B — run assay → specimen (implicit merge):")
        current = None
        for spec, src in mode_b:
            if spec != current:
                print(f"    {spec}")
                current = spec
            print(f"      └─ {src}")

    # Mode A: assay → merged assay
    mode_a = conn.execute(
        "SELECT merged_assay_id, source_assay_id "
        "FROM assay_sources WHERE merged_assay_id IS NOT NULL "
        "ORDER BY merged_assay_id, source_assay_id"
    ).fetchall()
    if mode_a:
        print("\n  Mode A — run assay → merged assay (explicit merge):")
        current = None
        for merged, src in mode_a:
            if merged != current:
                print(f"    {merged}")
                current = merged
            print(f"      └─ {src}")

    # Batch summary if batches table exists and has rows
    if "batches" in tables:
        (n_batches,) = conn.execute("SELECT COUNT(*) FROM batches").fetchone()
        if n_batches:
            print(f"\n  Batches: {n_batches} registered")
            rows = conn.execute(
                "SELECT b.batch_id, b.prep_date, COUNT(a.assay_id) AS n_assays "
                "FROM batches b "
                "LEFT JOIN assays a ON a.batch_id = b.batch_id "
                "GROUP BY b.batch_id ORDER BY b.batch_id"
            ).fetchall()
            bid_w = max(8, max(len(r[0]) for r in rows))
            print(f"    {'batch_id':<{bid_w}}  prep_date   n_assays")
            print("    " + "─" * (bid_w + 24))
            for bid, prep, n in rows:
                print(f"    {bid:<{bid_w}}  {prep or '—':<10}  {n}")


def cmd_status_project(args):
    project_dir, schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    conn = open_project_db(project_dir / PROJECT_DB_NAME)
    try:
        # v0.4 QC filter: --usable mode short-circuits to the usable/excluded
        # breakdown (§8.1). Otherwise default counts exclude fail/censored +
        # consent-revoked unless opted back in.
        from casetrack_qc.schema import qc_schema_exists as _qc_schema_exists
        qc_on = _qc_schema_exists(conn)
        if qc_on and getattr(args, "usable", False):
            _emit_status_usable(
                conn,
                project_dir,
                analysis=getattr(args, "analysis", None),
            )
            return

        group_by = args.group_by or "analysis"
        done_by_level = _discover_done_columns(conn)

        # Apply QC filter to the "total" counts when qc_on AND not including
        # censored. Kept in a dict so we can pass to _status_by_analysis for
        # per-level totals.
        include_censored = getattr(args, "include_censored", False)
        include_consent_revoked = getattr(args, "include_consent_revoked", False)
        counts_by_level = _status_counts_by_level(
            conn,
            qc_on=qc_on,
            include_censored=include_censored,
            include_consent_revoked=include_consent_revoked,
        )

        if group_by == "analysis":
            report = _status_by_analysis(
                conn, done_by_level, counts_by_level,
                qc_on=qc_on,
                include_censored=include_censored,
                include_consent_revoked=include_consent_revoked,
            )
        elif group_by == "assay":
            report = _status_by_row(conn, "assay", done_by_level["assay"])
        elif group_by == "specimen":
            report = _status_by_rollup(conn, "specimen", done_by_level)
        elif group_by == "patient":
            report = _status_by_rollup(conn, "patient", done_by_level)
        else:
            print(f"Error: unknown --group-by {group_by!r}", file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()

    _emit_status(report, fmt=args.fmt, project_dir=project_dir, group_by=group_by,
                 counts=counts_by_level)

    if getattr(args, "show_lineage", False):
        conn3 = open_project_db(project_dir / PROJECT_DB_NAME)
        try:
            _emit_lineage_section(conn3)
        finally:
            conn3.close()


def _status_counts_by_level(
    conn: sqlite3.Connection,
    *,
    qc_on: bool,
    include_censored: bool,
    include_consent_revoked: bool,
) -> dict:
    if not qc_on or (include_censored and include_consent_revoked):
        return {
            level: conn.execute(
                f"SELECT COUNT(*) FROM {_quote_ident(f'{level}s')}"
            ).fetchone()[0]
            for level in LEVEL_ORDER
        }
    from casetrack_qc.reader import (
        active_assay_ids as _active_assay_ids,
        active_patient_ids as _active_patient_ids,
        active_specimen_ids as _active_specimen_ids,
    )
    return {
        "patient": len(_active_patient_ids(
            conn,
            include_censored=include_censored,
            include_consent_revoked=include_consent_revoked,
        )),
        "specimen": len(_active_specimen_ids(
            conn,
            include_censored=include_censored,
            include_consent_revoked=include_consent_revoked,
        )),
        "assay": len(_active_assay_ids(
            conn,
            include_censored=include_censored,
            include_consent_revoked=include_consent_revoked,
        )),
    }


def _emit_status_usable(
    conn: sqlite3.Connection,
    project_dir: Path,
    *,
    analysis: str | None = None,
) -> None:
    """Render the §8.1 usable-vs-excluded breakdown."""
    from casetrack_qc.reader import (
        active_assay_ids as _active_assay_ids,
        exclusion_breakdown as _exclusion_breakdown,
    )
    usable = _active_assay_ids(conn)
    (total,) = conn.execute("SELECT COUNT(*) FROM assays").fetchone()
    excluded = _exclusion_breakdown(conn)

    done = 0
    pending = 0
    if analysis and usable:
        done_col = f"{analysis}{DONE_COLUMN_SUFFIX}"
        actual_cols = set(_get_table_columns(conn, "assays"))
        if done_col in actual_cols:
            placeholders = ", ".join("?" * len(usable))
            rows = conn.execute(
                f"SELECT {_quote_ident(done_col)} FROM assays "
                f"WHERE assay_id IN ({placeholders})",
                list(usable),
            ).fetchall()
            done = sum(1 for (v,) in rows if v is not None)
            pending = len(rows) - done
    else:
        # Summarize completeness across all known _done columns.
        done_cols = [
            c for c in _get_table_columns(conn, "assays")
            if c.endswith(DONE_COLUMN_SUFFIX)
        ]
        # One assay counts as "complete" iff every declared analysis is done.
        if usable and done_cols:
            placeholders = ", ".join("?" * len(usable))
            sql = (
                "SELECT "
                + ", ".join(
                    f"(SUM(CASE WHEN {_quote_ident(c)} IS NULL THEN 1 ELSE 0 END))"
                    for c in done_cols
                )
                + f" FROM assays WHERE assay_id IN ({placeholders})"
            )
            null_counts = conn.execute(sql, list(usable)).fetchone()
            # If any column has zero NULLs, every usable assay ran that analysis.
            done = len(usable)
            pending = 0
            # Per-analysis breakdown.

    print(f"Project: {project_dir}")
    print(f"  Usable assays: {len(usable)} / {total}")
    if analysis:
        print(f"    Complete:    {done}")
        print(f"    Pending:     {pending}")
    n_qc = len(excluded['qc_failed_assays'])
    n_censored = len(excluded['censored_assays'])
    n_consent = len(excluded['consent_revoked_assays'])
    n_excluded = total - len(usable)
    print(f"  Excluded:      {n_excluded}")
    if n_qc:
        ids = ", ".join(excluded['qc_failed_assays'][:5])
        more = f" (+{n_qc - 5} more)" if n_qc > 5 else ""
        print(f"    QC-failed:   {n_qc}   ({ids}{more})")
    if n_censored:
        print(f"    Censored:    {n_censored}")
    if n_consent:
        pids = excluded['consent_revoked_patients']
        ids = ", ".join(pids[:5])
        more = f" (+{len(pids) - 5} more)" if len(pids) > 5 else ""
        print(f"    Consent-rev: {n_consent}   (patients: {ids}{more})")


def _status_by_analysis(
    conn, done_by_level, counts_by_level,
    *,
    qc_on: bool = False,
    include_censored: bool = False,
    include_consent_revoked: bool = False,
) -> list[dict]:
    rows = []
    # When QC is on and we're filtering, scope `done` counts to the active
    # set so percentages match the filtered totals.
    active_by_level: dict[str, set[str] | None] = {
        level: None for level in LEVEL_ORDER
    }
    if qc_on and not (include_censored and include_consent_revoked):
        from casetrack_qc.reader import (
            active_assay_ids as _active_assay_ids,
            active_patient_ids as _active_patient_ids,
            active_specimen_ids as _active_specimen_ids,
        )
        active_by_level = {
            "patient": _active_patient_ids(
                conn, include_censored=include_censored,
                include_consent_revoked=include_consent_revoked),
            "specimen": _active_specimen_ids(
                conn, include_censored=include_censored,
                include_consent_revoked=include_consent_revoked),
            "assay": _active_assay_ids(
                conn, include_censored=include_censored,
                include_consent_revoked=include_consent_revoked),
        }

    for level in LEVEL_ORDER:
        table = f"{level}s"
        total = counts_by_level[level]
        key_col = f"{level}_id"
        active = active_by_level[level]
        for done_col in done_by_level[level]:
            if active is None:
                (done,) = conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_ident(table)} "
                    f"WHERE {_quote_ident(done_col)} IS NOT NULL"
                ).fetchone()
            elif not active:
                done = 0
            else:
                placeholders = ", ".join("?" * len(active))
                (done,) = conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_ident(table)} "
                    f"WHERE {_quote_ident(done_col)} IS NOT NULL "
                    f"AND {_quote_ident(key_col)} IN ({placeholders})",
                    list(active),
                ).fetchone()
            pct = round(100.0 * done / total, 1) if total else 0.0
            rows.append({
                "analysis": done_col[: -len(DONE_COLUMN_SUFFIX)],
                "level": level,
                "done": done,
                "total": total,
                "pct": pct,
            })
    rows.sort(key=lambda r: (r["level"], r["analysis"]))
    return rows


def _status_by_row(conn, level: str, done_cols: list) -> list[dict]:
    """One row per entity at `level`, listing which analyses are done."""
    if not done_cols:
        return []
    table = f"{level}s"
    key = f"{level}_id"
    select_cols = [key] + done_cols
    sql_cols = ", ".join(_quote_ident(c) for c in select_cols)
    rows = []
    for r in conn.execute(
        f"SELECT {sql_cols} FROM {_quote_ident(table)} ORDER BY {_quote_ident(key)}"
    ).fetchall():
        entity_id, *done_vals = r
        done_names = [
            dc[: -len(DONE_COLUMN_SUFFIX)]
            for dc, v in zip(done_cols, done_vals)
            if v is not None
        ]
        rows.append({
            level + "_id": entity_id,
            "analyses_done": done_names,
            "n_done": len(done_names),
            "n_total": len(done_cols),
        })
    return rows


def _status_by_rollup(conn, level: str, done_by_level: dict) -> list[dict]:
    """Per-entity at `level`, count children and completed assay-level analyses.

    For --group-by specimen: counts assays per specimen and how many of each
    assay-level analysis are done within the specimen.
    For --group-by patient: same, walked through specimens.
    """
    if level == "specimen":
        outer_table, outer_key = "specimens", "specimen_id"
        join_sql = (
            "specimens LEFT JOIN assays "
            "ON assays.specimen_id = specimens.specimen_id"
        )
        group_col = "specimens.specimen_id"
    elif level == "patient":
        outer_table, outer_key = "patients", "patient_id"
        join_sql = (
            "patients "
            "LEFT JOIN specimens ON specimens.patient_id = patients.patient_id "
            "LEFT JOIN assays ON assays.specimen_id = specimens.specimen_id"
        )
        group_col = "patients.patient_id"
    else:
        raise ValueError(f"unsupported rollup level: {level}")

    assay_done_cols = done_by_level["assay"]
    assay_counts = ", ".join(
        f"SUM(CASE WHEN assays.{_quote_ident(dc)} IS NOT NULL THEN 1 ELSE 0 END) "
        f"AS {_quote_ident('done_' + dc)}"
        for dc in assay_done_cols
    )
    count_children = {
        "specimen": "COUNT(assays.assay_id) AS n_assays",
        "patient":  "COUNT(DISTINCT specimens.specimen_id) AS n_specimens, "
                    "COUNT(assays.assay_id) AS n_assays",
    }[level]

    select_parts = [group_col, count_children]
    if assay_counts:
        select_parts.append(assay_counts)

    sql = (
        f"SELECT {', '.join(select_parts)} FROM {join_sql} "
        f"GROUP BY {group_col} ORDER BY {group_col}"
    )

    rows = []
    cursor = conn.execute(sql)
    col_names = [d[0] for d in cursor.description]
    for r in cursor.fetchall():
        row = dict(zip(col_names, r))
        entity_id = row[outer_key]
        rec: dict = {outer_key: entity_id}
        if level == "patient":
            rec["n_specimens"] = row["n_specimens"]
        rec["n_assays"] = row["n_assays"]
        rec["assay_analyses_done"] = {
            dc[: -len(DONE_COLUMN_SUFFIX)]: row[f"done_{dc}"]
            for dc in assay_done_cols
        }
        rows.append(rec)
    return rows


def _emit_status(report, *, fmt: str, project_dir: Path, group_by: str,
                 counts: dict) -> None:
    if fmt == "json":
        print(json.dumps(report, indent=2, default=str))
        return

    if fmt == "tsv":
        if not report:
            return
        keys = list(report[0].keys())
        print("\t".join(keys))
        for row in report:
            print("\t".join(_tsv_cell(row[k]) for k in keys))
        return

    # Table (default)
    print(f"\nProject:   {project_dir}")
    print(f"Group by:  {group_by}")
    print(f"Counts:    patients={counts['patient']}, "
          f"specimens={counts['specimen']}, assays={counts['assay']}")
    print("─" * 60)
    if not report:
        print(f"No data found for --group-by {group_by}.")
        return

    if group_by == "analysis":
        print(f"{'Analysis':<28} {'Level':<10} {'Done':>6} {'Total':>6} {'%':>7}")
        print("─" * 60)
        for row in report:
            bar_len = 10
            filled = int(bar_len * row["pct"] / 100)
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"{row['analysis']:<28} {row['level']:<10} {row['done']:>6} "
                  f"{row['total']:>6} {row['pct']:>6.1f}% {bar}")
    elif group_by == "assay":
        print(f"{'Assay':<20} {'Done':>4}/{'Total':<5} Analyses")
        print("─" * 60)
        for row in report:
            print(f"{row['assay_id']:<20} {row['n_done']:>4}/{row['n_total']:<5} "
                  f"{', '.join(row['analyses_done']) if row['analyses_done'] else '-'}")
    else:  # specimen / patient rollup
        key = f"{group_by}_id"
        if group_by == "patient":
            print(f"{'Patient':<18} {'Specimens':>10} {'Assays':>8}  Analyses (done on assays)")
        else:
            print(f"{'Specimen':<18} {'Assays':>8}  Analyses (done on assays)")
        print("─" * 60)
        for row in report:
            analyses = ", ".join(
                f"{name}={n}" for name, n in sorted(row["assay_analyses_done"].items())
            ) or "-"
            if group_by == "patient":
                print(f"{row[key]:<18} {row['n_specimens']:>10} {row['n_assays']:>8}  {analyses}")
            else:
                print(f"{row[key]:<18} {row['n_assays']:>8}  {analyses}")


def _tsv_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return ",".join(f"{k}={v}" for k, v in value.items())
    return str(value)


# ── v0.3 validate ──────────────────────────────────────────────────────────────


def _collect_analysis_columns_from_provenance(prov_path: Path) -> dict:
    """Return {analysis_name: set(columns_added)} from every `append` in the log."""
    out: dict = {}
    if not prov_path.exists():
        return out
    for line in prov_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("action") != "append":
            continue
        analysis = entry.get("analysis")
        if not analysis:
            continue
        out.setdefault(analysis, set()).update(entry.get("columns_added", []))
    return out


def cmd_validate_project(args):
    project_dir, schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    conn = open_project_db(project_dir / PROJECT_DB_NAME)
    issues: list[str] = []
    try:
        # 1. TOML ↔ DB column drift. Declared cols must exist in the DB.
        for level in LEVEL_ORDER:
            table = f"{level}s"
            declared = schema["levels"][level]["columns"]
            actual = set(_get_table_columns(conn, table))
            for colname in declared:
                if colname not in actual:
                    issues.append(
                        f"{table}: column {colname!r} declared in TOML but missing in DB"
                    )

        # 2. Referential integrity — PRAGMA foreign_key_check returns orphan rows.
        for row in conn.execute("PRAGMA foreign_key_check").fetchall():
            table, rowid, parent, fk_id = row
            issues.append(
                f"orphan row in {table} (rowid={rowid}): FK #{fk_id} to "
                f"{parent} points nowhere"
            )

        # 3. Orphan `_done` columns — a `{x}_done` column with no data
        # companions added by the same analysis. Cross-referenced against
        # `columns_added` entries in provenance.jsonl so we don't rely on
        # fragile name-prefix heuristics.
        columns_by_analysis = _collect_analysis_columns_from_provenance(
            project_dir / PROJECT_PROVENANCE_NAME
        )
        for level in LEVEL_ORDER:
            table = f"{level}s"
            done = [
                c for c in _get_table_columns(conn, table)
                if c.endswith(DONE_COLUMN_SUFFIX)
            ]
            for dc in done:
                analysis = dc[: -len(DONE_COLUMN_SUFFIX)]
                companions = [
                    c for c in columns_by_analysis.get(analysis, set())
                    if c != dc
                ]
                if companions:
                    continue
                (has_data,) = conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_ident(table)} "
                    f"WHERE {_quote_ident(dc)} IS NOT NULL"
                ).fetchone()
                if has_data:
                    issues.append(
                        f"{table}.{dc} has {has_data} completion(s) but no "
                        f"data column from analysis {analysis!r} in provenance"
                    )

        # 4. v0.4 QC invariants.
        from casetrack_qc.schema import qc_schema_exists as _qc_schema_exists
        if _qc_schema_exists(conn):
            from casetrack_qc.consent import (
                consent_event_invariant_violations as _consent_violations,
            )
            for v in _consent_violations(conn):
                issues.append(f"consent invariant: {v['message']}")

            # 4b. Orphan active events: target entity missing.
            for level in LEVEL_ORDER:
                table = f"{level}s"
                key = f"{level}_id"
                orphans = conn.execute(
                    "SELECT id, entity_id FROM qc_events "
                    "WHERE level=? AND resolved_at IS NULL "
                    f"AND entity_id NOT IN (SELECT {_quote_ident(key)} FROM {_quote_ident(table)})",
                    (level,),
                ).fetchall()
                for eid, entity_id in orphans:
                    issues.append(
                        f"qc_events id={eid}: active event references "
                        f"missing {level} {entity_id!r}"
                    )

            # 4c. qc_status ↔ active-events consistency. For every row,
            # recompute the expected status from active events and compare
            # against the materialized column.
            from casetrack_qc.events import derive_status as _derive_status
            for level in LEVEL_ORDER:
                table = f"{level}s"
                key = f"{level}_id"
                rows = conn.execute(
                    f"SELECT {_quote_ident(key)}, qc_status FROM {_quote_ident(table)}"
                ).fetchall()
                for entity_id, materialized in rows:
                    active_kinds = [
                        r[0] for r in conn.execute(
                            "SELECT kind FROM qc_events "
                            "WHERE level=? AND entity_id=? AND resolved_at IS NULL",
                            (level, entity_id),
                        ).fetchall()
                    ]
                    expected = _derive_status(active_kinds, level)
                    if expected != materialized:
                        issues.append(
                            f"{table}.qc_status mismatch for {entity_id!r}: "
                            f"materialized={materialized!r} vs "
                            f"expected-from-events={expected!r}"
                        )

        # 5. v0.6 lineage invariants (if assay_sources / batches tables exist).
        existing_tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "assay_sources" in existing_tables:
            # 5a. All source_assay_id values must reference existing assays.
            orphan_sources = conn.execute(
                "SELECT source_assay_id FROM assay_sources "
                "WHERE source_assay_id NOT IN (SELECT assay_id FROM assays)"
            ).fetchall()
            for (oid,) in orphan_sources:
                issues.append(
                    f"assay_sources: source_assay_id {oid!r} not in assays table"
                )
            # 5b. All merged_assay_id values must reference existing assays.
            orphan_merged = conn.execute(
                "SELECT merged_assay_id FROM assay_sources "
                "WHERE merged_assay_id IS NOT NULL "
                "AND merged_assay_id NOT IN (SELECT assay_id FROM assays)"
            ).fetchall()
            for (oid,) in orphan_merged:
                issues.append(
                    f"assay_sources: merged_assay_id {oid!r} not in assays table"
                )
            # 5c. All consumer_specimen_id values must reference existing specimens.
            orphan_spec = conn.execute(
                "SELECT consumer_specimen_id FROM assay_sources "
                "WHERE consumer_specimen_id IS NOT NULL "
                "AND consumer_specimen_id NOT IN (SELECT specimen_id FROM specimens)"
            ).fetchall()
            for (oid,) in orphan_spec:
                issues.append(
                    f"assay_sources: consumer_specimen_id {oid!r} not in specimens table"
                )

        if "batches" in existing_tables:
            # 5d. Every assays.batch_id must reference an existing batches row.
            orphan_batches = conn.execute(
                "SELECT assay_id, batch_id FROM assays "
                "WHERE batch_id IS NOT NULL "
                "AND batch_id NOT IN (SELECT batch_id FROM batches)"
            ).fetchall()
            for aid, bid in orphan_batches:
                issues.append(
                    f"assays: assay {aid!r} has batch_id {bid!r} not in batches table"
                )
    finally:
        conn.close()

    if issues:
        print(f"Validation found {len(issues)} issue(s):", file=sys.stderr)
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Project OK: {project_dir} "
        f"(schema_v={schema['project']['schema_v']}, no integrity issues)."
    )


# ── v0.3 log ───────────────────────────────────────────────────────────────────


def cmd_log_project(args):
    project_dir, _schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    prov_path = project_dir / PROJECT_PROVENANCE_NAME
    if not prov_path.exists():
        print(f"No provenance log at {prov_path}", file=sys.stderr)
        sys.exit(1)

    entries = [
        json.loads(ln)
        for ln in prov_path.read_text().splitlines()
        if ln.strip()
    ]

    if args.level:
        entries = [e for e in entries if e.get("level") == args.level]
    if args.transaction:
        entries = [e for e in entries if e.get("transaction_id") == args.transaction]

    entries.sort(key=lambda e: e.get("timestamp", ""))

    if args.last:
        entries = entries[-args.last:]

    if not entries:
        print("(no matching provenance entries)")
        return

    for e in entries:
        print(_format_project_log_entry(e))


def _format_project_log_entry(entry: dict) -> str:
    ts = entry.get("timestamp", "?")
    action = entry.get("action", "?")
    user = entry.get("user", "?")
    job = entry.get("slurm_job_id") or ""
    level = entry.get("level") or ""
    txn = entry.get("transaction_id", "")

    job_str = f" [SLURM {job}]" if job else ""
    level_str = f" {level}" if level else ""
    txn_str = f" {txn}" if txn else ""

    if action == "init_project":
        tpl = entry.get("template", "?")
        v = entry.get("schema_v_after", "?")
        return f"  {ts}  INIT_PROJECT  {user}{job_str} — template={tpl}, schema_v={v}"
    if action == "migrate":
        counts = entry.get("rows_inserted", {})
        return (f"  {ts}  MIGRATE  {user}{job_str}{txn_str} — "
                f"patients={counts.get('patient','?')}, "
                f"specimens={counts.get('specimen','?')}, "
                f"assays={counts.get('assay','?')}")
    if action == "register":
        id_ = entry.get("id", "?")
        parent = entry.get("parent") or ""
        stub = " (+new parent)" if entry.get("parent_created") else ""
        parent_str = f" parent={parent}" if parent else ""
        return (f"  {ts}  REGISTER{level_str}  {user}{job_str}{txn_str} — "
                f"id={id_}{parent_str}{stub}")
    if action == "append":
        analysis = entry.get("analysis", "?")
        added = entry.get("columns_added", [])
        rows = entry.get("rows_affected", "?")
        added_str = f" (+{len(added)} cols)" if added else ""
        return (f"  {ts}  APPEND{level_str}  {user}{job_str}{txn_str} — "
                f"analysis={analysis}, rows={rows}{added_str}")
    return f"  {ts}  {action.upper()}{level_str}  {user}{job_str}{txn_str}"


# ── CLI Parser ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="casetrack",
        description="Manifest-centric case management for bioinformatics pipelines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Initialize a new project manifest
  casetrack init --manifest manifest.tsv --samples samples.txt

  # Initialize with metadata columns
  casetrack init --manifest manifest.tsv --samples samples.txt --metadata sample_info.tsv

  # Append modkit results after a SLURM job
  casetrack append --manifest manifest.tsv --results modkit_summary.tsv \\
      --key sample_id --analysis modkit_methylation

  # Check what's done
  casetrack status --manifest manifest.tsv

  # Validate manifest integrity
  casetrack validate --manifest manifest.tsv --key sample_id

  # View provenance log
  casetrack log --manifest manifest.tsv --last 10

  # Export to Excel for sharing
  casetrack export --manifest manifest.tsv --output manifest.xlsx
        """,
    )
    # v0.6 Part B: global --project flag for registry-resolved project lookup.
    # Goes BEFORE the subcommand: `casetrack --project hgsoc-2026 query "..."`.
    # Per-command `--project-dir` still works; pass either, not both.
    parser.add_argument(
        "--project", default=None,
        help="[v0.6] Resolve project by registered project_id (alternative "
             "to --project-dir). See `casetrack projects list`.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # ── init ──
    p_init = subparsers.add_parser(
        "init",
        help="Initialize a new manifest (flat) or casetrack project (v0.3 SQLite)",
    )
    g_init_target = p_init.add_mutually_exclusive_group(required=True)
    g_init_target.add_argument("--manifest", help="[flat mode] Path to manifest TSV")
    g_init_target.add_argument(
        "--project-dir",
        help="[project mode] Directory for a new v0.3 casetrack project",
    )
    p_init.add_argument("--samples", help="[flat mode] Text file with one sample_id per line")
    p_init.add_argument("--key", default="sample_id", help="Key column name (default: sample_id)")
    p_init.add_argument("--metadata", help="[flat mode] Optional TSV with additional sample metadata")
    p_init.add_argument("--cols", help="[flat mode] Comma-separated list of empty columns to pre-create")
    p_init.add_argument(
        "--from-template",
        choices=sorted(TEMPLATES.keys()),
        default="blank",
        help="[project mode] Schema template (default: blank)",
    )
    p_init.add_argument(
        "--project-name",
        help="[project mode] Project name written into casetrack.toml (default: directory basename)",
    )
    p_init.add_argument(
        "--project-id", default=None,
        help="[v0.6] DNS-label slug (^[a-z0-9][a-z0-9-]{2,63}$) used as the "
             "machine-queryable identifier in the registry. Default: derived "
             "from --project-name or directory basename.",
    )
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing manifest (flat) or casetrack.db (project)",
    )
    p_init.add_argument(
        "--bare",
        action="store_true",
        help="[project mode] Skip the full directory scaffold (proposal 0003); "
             "emit only casetrack.{toml,db}, provenance.jsonl, .gitignore.",
    )

    # ── append ──
    p_append = subparsers.add_parser(
        "append",
        help="Append analysis results to a manifest (flat) or project (v0.3 SQLite)",
    )
    # Not required at argparse level: --infer-from-path can supply --project-dir.
    # cmd_append enforces "one of manifest / project_dir / infer_from_path" below.
    g_append_target = p_append.add_mutually_exclusive_group(required=False)
    g_append_target.add_argument("--manifest", help="[flat mode] Path to manifest TSV")
    g_append_target.add_argument(
        "--project-dir",
        help="[project mode] Casetrack project directory",
    )
    p_append.add_argument("--results",
                          help="Path to results TSV (inferred from path when --infer-from-path is used)")
    p_append.add_argument("--key", default="sample_id",
                          help="[flat mode] Key column to join on (default: sample_id)")
    p_append.add_argument("--analysis",
                          help="Name of this analysis (e.g. modkit_methylation); "
                               "inferred from [analyses.<tool>] when --infer-from-path is used")
    p_append.add_argument(
        "--level",
        choices=list(LEVEL_ORDER),
        help="[project mode] Target level (default: analysis_defaults.default_level)",
    )
    p_append.add_argument(
        "--col-type",
        help="[project mode] Override inferred types, e.g. 'mean_meth:REAL,n_reads:INTEGER'. "
             "Names match the TSV (pre-prefix).",
    )
    p_append.add_argument(
        "--column-prefix",
        help="[project mode] Rename every analysis column to {prefix}_{name} on the way "
             "in, so two analyses at different scopes (e.g. merged vs chr17) never "
             "collide under fill-only COALESCE. Key column, qc_pass/qc_fail_reason/"
             "qc_warn (v0.4 autoflag), and {analysis}_done are never prefixed.",
    )
    p_append.add_argument("--overwrite", action="store_true",
                          help="Overwrite existing non-null cells (default: fill-only)")
    p_append.add_argument("--allow-new", action="store_true",
                          help="[flat mode] Allow new sample IDs not in manifest (requires --yes)")
    p_append.add_argument("--yes", action="store_true",
                          help="Confirm --allow-new (flat) or --force-append-on-censored (v0.4)")
    p_append.add_argument(
        "--force-append-on-censored",
        action="store_true",
        help="[v0.4] Allow append on censored entities (requires --yes)",
    )
    p_append.add_argument(
        "--force-archived",
        action="store_true",
        help="[v0.7] Allow mutations on an archived project (requires --yes)",
    )
    p_append.add_argument(
        "--infer-from-path",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="[v0.5] Walk up from PATH (default: $PWD) to find a casetrack "
             "project, then map the path to level/tool/run_tag/ids via "
             "[layout.path_templates]. Fills --project-dir, --level, "
             "--analysis, --column-prefix, and --results from the leaf dir. "
             "Explicit flags still override.",
    )

    # ── status ──
    p_status = subparsers.add_parser(
        "status", help="Show completion status (flat or project mode)"
    )
    g_status_target = p_status.add_mutually_exclusive_group(required=True)
    g_status_target.add_argument("--manifest", help="[flat mode] Path to manifest TSV")
    g_status_target.add_argument("--project-dir", help="[project mode] Casetrack project directory")
    p_status.add_argument("--key", default="sample_id", help="[flat mode] Key column name")
    p_status.add_argument("--analysis", help="[flat mode] Filter to a specific analysis")
    p_status.add_argument(
        "--group-by",
        choices=["analysis", "assay", "specimen", "patient"],
        help="[project mode] Group by (default: analysis)",
    )
    p_status.add_argument("--fmt", choices=["table", "tsv", "json"], default="table",
                          help="Output format")
    p_status.add_argument(
        "--usable", action="store_true",
        help="[v0.4] Show usable vs excluded breakdown (proposal §8.1)",
    )
    p_status.add_argument(
        "--include-censored", action="store_true",
        help="[v0.4] Include QC-failed/censored in counts",
    )
    p_status.add_argument(
        "--include-consent-revoked", action="store_true",
        help="[v0.4] Include consent-revoked patients and their assays",
    )
    p_status.add_argument(
        "--show-lineage", action="store_true",
        help="[v0.6] Append assay-source lineage section (requires migrate-lineage)",
    )

    # ── validate ──
    p_validate = subparsers.add_parser(
        "validate", help="Validate a manifest (flat) or project (v0.3 SQLite)"
    )
    g_validate_target = p_validate.add_mutually_exclusive_group(required=True)
    g_validate_target.add_argument("--manifest", help="[flat mode] Path to manifest TSV")
    g_validate_target.add_argument("--project-dir", help="[project mode] Casetrack project directory")
    p_validate.add_argument("--key", default="sample_id", help="[flat mode] Key column name")

    # ── log ──
    p_log = subparsers.add_parser(
        "log", help="Show provenance log (flat or project mode)"
    )
    g_log_target = p_log.add_mutually_exclusive_group(required=True)
    g_log_target.add_argument("--manifest", help="[flat mode] Path to manifest TSV")
    g_log_target.add_argument("--project-dir", help="[project mode] Casetrack project directory")
    p_log.add_argument("--last", type=int, help="Show only the last N entries")
    p_log.add_argument(
        "--level",
        choices=list(LEVEL_ORDER),
        help="[project mode] Filter to entries touching a specific level",
    )
    p_log.add_argument(
        "--transaction",
        help="[project mode] Filter to entries with a specific transaction_id",
    )

    # ── schema ──
    p_schema = subparsers.add_parser(
        "schema",
        help="Show / dump / check / apply schema (flat: column-to-analysis map)",
    )
    g_schema_target = p_schema.add_mutually_exclusive_group(required=True)
    g_schema_target.add_argument("--manifest", help="[flat mode] Path to manifest TSV")
    g_schema_target.add_argument("--project-dir", help="[project mode] Casetrack project directory")
    p_schema.add_argument(
        "action",
        nargs="?",
        choices=["show", "dump", "check", "apply"],
        default=None,
        help="[project mode] What to do (default: show)",
    )
    p_schema.add_argument("--fmt", choices=["table", "json"], default="table",
                          help="[flat mode] Output format")

    # ── query ──
    p_query = subparsers.add_parser(
        "query",
        help="Run SQL against a v0.3 project (--project-dir), a manifest (--manifest), "
             "or a union of many (--root). DuckDB-backed.",
    )
    g_target = p_query.add_mutually_exclusive_group(required=True)
    g_target.add_argument("--project-dir", help="[project mode] Casetrack project directory")
    g_target.add_argument("--manifest", help="[flat mode] Path to a single manifest TSV")
    g_target.add_argument("--root", help="[flat mode] Scan a root for manifests (cross-project query)")
    p_query.add_argument("sql",
                         help="SQL query. Project mode exposes views: patients, specimens, "
                              "assays, and `_` (assays⋈specimens⋈patients inner join). "
                              "Flat mode exposes the manifest as `_` (or --as NAME).")
    p_query.add_argument("--as", dest="as_name", default=None,
                         help="[flat mode] SQL table/view alias for the manifest (default: _)")
    p_query.add_argument("--pattern", default="manifest.tsv",
                         help="[flat --root] Manifest filename pattern")
    p_query.add_argument("--max-depth", type=int, default=4,
                         help="[flat --root] Maximum directory depth (default: 4)")
    p_query.add_argument("--fmt", choices=["table", "tsv", "csv", "json"], default="table",
                         help="Output format (default: table)")
    p_query.add_argument("--output", help="Write results to this file instead of stdout")

    # ── projects ──
    # v0.6 Part B: nested subactions. `scan` preserves the v0.5 behavior;
    # `list`/`register`/`deregister` operate on ~/.casetrack/registry.json.
    # The scan args (--root et al) are also exposed on the parent parser so
    # the v0.5 form `casetrack projects --root <path>` keeps working.
    p_projects = subparsers.add_parser(
        "projects",
        help="Manage the project registry (~/.casetrack/registry.json) or "
             "scan a directory for manifests",
    )
    p_projects.add_argument("--root", default=None,
                            help="[v0.5 compat] Root directory to scan; "
                                 "equivalent to `projects scan --root`")
    p_projects.add_argument("--pattern", default="manifest.tsv",
                            help="[scan] Manifest filename pattern (default: manifest.tsv)")
    p_projects.add_argument("--max-depth", type=int, default=4,
                            help="[scan] Maximum directory depth to scan (default: 4)")
    p_projects.add_argument("--key", default="sample_id",
                            help="[scan] Key column name")
    p_projects.add_argument("--fmt", choices=["table", "tsv", "json"], default="table",
                            help="Output format")
    projects_sub = p_projects.add_subparsers(
        dest="projects_action", help="action (scan | list | register | deregister)"
    )

    # projects scan — explicit form of the legacy filesystem-walk behavior.
    # Reuses the parent parser's flags so users can pass them in either spot.
    p_proj_scan = projects_sub.add_parser(
        "scan", help="Walk a directory tree and summarize discovered projects"
    )
    p_proj_scan.add_argument("--root", help="Root directory to scan")
    p_proj_scan.add_argument("--pattern", default="manifest.tsv",
                             help="Manifest filename pattern (default: manifest.tsv)")
    p_proj_scan.add_argument("--max-depth", type=int, default=4,
                             help="Maximum directory depth to scan (default: 4)")
    p_proj_scan.add_argument("--key", default="sample_id", help="Key column name")
    p_proj_scan.add_argument("--fmt", choices=["table", "tsv", "json"], default="table",
                             help="Output format")

    # projects list — read the registry (v0.6 Part B).
    p_proj_list = projects_sub.add_parser(
        "list",
        help="List projects in ~/.casetrack/registry.json",
    )
    p_proj_list.add_argument("--fmt", choices=["table", "tsv", "json"], default="table",
                             help="Output format (default: table)")
    p_proj_list.add_argument(
        "--status",
        default="active",
        help="Filter by lifecycle status: active (default), complete, archived, all, "
             "or comma-separated combination e.g. active,complete",
    )

    # projects register — manually add an existing project to the registry
    # (e.g. after restoring from archive on a new machine).
    p_proj_register = projects_sub.add_parser(
        "register",
        help="Add a project directory to ~/.casetrack/registry.json",
    )
    p_proj_register.add_argument(
        "--project-dir", required=True, help="Path to the casetrack project directory"
    )

    # projects deregister — remove a registry entry without touching the
    # project directory itself.
    p_proj_dereg = projects_sub.add_parser(
        "deregister",
        help="Remove a project_id from the registry (does not touch the directory)",
    )
    p_proj_dereg.add_argument(
        "project_id", help="The project_id slug to remove"
    )

    # ── add-metadata ──
    p_meta = subparsers.add_parser(
        "add-metadata",
        help="Bulk-add metadata to a manifest (flat) or a project level (v0.3 SQLite)",
    )
    g_meta_target = p_meta.add_mutually_exclusive_group(required=True)
    g_meta_target.add_argument("--manifest", help="[flat mode] Path to manifest TSV")
    g_meta_target.add_argument("--project-dir", help="[project mode] Casetrack project directory")
    p_meta.add_argument("--metadata", required=True,
                        help="Path to metadata TSV (must include level's key column)")
    p_meta.add_argument("--key", default="sample_id",
                        help="[flat mode] Key column to join on (default: sample_id)")
    p_meta.add_argument(
        "--level",
        choices=list(LEVEL_ORDER),
        help="[project mode] Target level (required in project mode)",
    )
    p_meta.add_argument("--fill-only", action="store_true",
                        help="On column collision, fill NaN cells only (default in project mode)")
    p_meta.add_argument("--overwrite", action="store_true",
                        help="On column collision, replace existing values")
    p_meta.add_argument("--allow-new", action="store_true",
                        help="Allow new keys not already in manifest/table (requires --yes)")
    p_meta.add_argument("--yes", action="store_true",
                        help="Confirm --allow-new additions non-interactively")
    p_meta.add_argument(
        "--force-archived",
        action="store_true",
        help="[v0.7] Allow mutations on an archived project (requires --yes)",
    )

    # ── dashboard ──
    p_dash = subparsers.add_parser(
        "dashboard",
        help="Generate a self-contained HTML dashboard (flat: samples table, "
             "v0.3: nested patients → specimens → assays)",
    )
    g_dash_target = p_dash.add_mutually_exclusive_group(required=True)
    g_dash_target.add_argument("--manifest", help="[flat mode] Path to manifest TSV")
    g_dash_target.add_argument("--project-dir", help="[project mode] Casetrack project directory")
    p_dash.add_argument("--output", required=True, help="Output HTML file path")
    p_dash.add_argument("--key", default="sample_id",
                        help="[flat mode] Key column (default: sample_id)")

    # ── rerun ──
    p_rerun = subparsers.add_parser(
        "rerun",
        help="Emit/submit sbatch commands for rows missing a given analysis",
    )
    g_rerun_target = p_rerun.add_mutually_exclusive_group(required=True)
    g_rerun_target.add_argument("--manifest", help="[flat mode] Path to manifest TSV")
    g_rerun_target.add_argument("--project-dir", help="[project mode] Casetrack project directory")
    p_rerun.add_argument("--analysis", required=True, help="Analysis whose _done column to check")
    p_rerun.add_argument("--script", help="sbatch script path (required unless --list-only)")
    p_rerun.add_argument("--key", default="sample_id", help="[flat mode] Key column (default: sample_id)")
    p_rerun.add_argument(
        "--level",
        choices=list(LEVEL_ORDER),
        help="[project mode] Which level's _done column to check (default: assay)",
    )
    p_rerun.add_argument("--submit", action="store_true", help="Actually invoke sbatch (default: dry-run)")
    p_rerun.add_argument("--list-only", action="store_true", help="Print bare IDs, not sbatch commands")
    p_rerun.add_argument("--extra", help="Extra args appended to each sbatch command")
    p_rerun.add_argument(
        "--force-censored", action="store_true",
        help="[v0.4] Include censored/consent-revoked entities (default: skip)",
    )
    p_rerun.add_argument(
        "--include-sources", action="store_true",
        help="[v0.6] Also emit rerun commands for source assays "
             "recorded in assay_sources (e.g. to re-basecall after model update)",
    )

    # ── export ──
    p_export = subparsers.add_parser(
        "export",
        help="Export a manifest (flat) or a v0.3 project to other formats",
    )
    g_export_target = p_export.add_mutually_exclusive_group(required=True)
    g_export_target.add_argument("--manifest", help="[flat mode] Path to manifest TSV")
    g_export_target.add_argument("--project-dir", help="[project mode] Casetrack project directory")
    p_export.add_argument("--output", required=True,
                          help="Output path — file (joined/single table) or directory (tables shape)")
    p_export.add_argument(
        "--shape",
        choices=["tables", "joined"],
        default=None,
        help="[project mode] tables = one file per level (default); joined = single denormalized file",
    )
    p_export.add_argument(
        "--tables",
        help="[project mode] With --shape tables, restrict to a subset, "
             "e.g. 'patients,assays'",
    )
    p_export.add_argument(
        "--sql",
        help="[project mode] Run an arbitrary SQL query (overrides --shape); "
             "the result is written to --output",
    )
    p_export.add_argument(
        "--include-censored", action="store_true",
        help="[v0.4] Include QC-failed/censored entities in export",
    )
    p_export.add_argument(
        "--include-consent-revoked", action="store_true",
        help="[v0.4] Include consent-revoked patients and their assays",
    )
    p_export.add_argument(
        "--include-lineage", action="store_true",
        help="[v0.6] Also export assay_sources and batches tables "
             "(auto-enabled for XLSX multi-sheet output)",
    )

    # ── register ──
    p_register = subparsers.add_parser(
        "register",
        help="[v0.3] Insert a single row at --level (patient/specimen/assay)",
    )
    p_register.add_argument("--project-dir", default=None,
                            help="Casetrack project directory (or use --project <id>)")
    p_register.add_argument(
        "--level",
        required=True,
        choices=list(LEVEL_ORDER),
        help="Which table to register into",
    )
    p_register.add_argument("--id", required=True, help="Primary key for the new row")
    p_register.add_argument(
        "--parent",
        help="Parent ID (required for specimen/assay, rejected for patient)",
    )
    p_register.add_argument(
        "--meta",
        help="Column values as 'key=value,key=value' (coerced per schema types)",
    )
    p_register.add_argument(
        "--allow-new-parent",
        action="store_true",
        help="Create the immediate parent row inline if it doesn't exist (requires --yes)",
    )
    p_register.add_argument(
        "--yes",
        action="store_true",
        help="Confirm --allow-new-parent creation non-interactively",
    )
    p_register.add_argument(
        "--force-archived",
        action="store_true",
        help="[v0.7] Allow mutations on an archived project (requires --yes)",
    )

    # ── doctor ──
    p_doctor = subparsers.add_parser(
        "doctor",
        help="[v0.3] Stress-test SQLite concurrency; [v0.6] --id-format scans hierarchy IDs",
    )
    p_doctor.add_argument("--project-dir", default=None,
                          help="Casetrack project directory (or use --project <id>)")
    p_doctor.add_argument("--workers", type=int, default=8,
                          help="Concurrent writer processes (default: 8)")
    p_doctor.add_argument("--writes", type=int, default=50,
                          help="INSERTs per worker (default: 50)")
    p_doctor.add_argument(
        "--id-format", action="store_true",
        help="[v0.6] Scan patient/specimen/assay IDs against the schema's "
             "format rules (proposal 0005 Part A). Read-only. Exits 0 if "
             "clean, 1 if any malformed IDs are found.",
    )
    p_doctor.add_argument(
        "--fmt", choices=["table", "tsv"], default="table",
        help="[v0.6] Output format for --id-format (default: table)",
    )

    # ── recover ──
    p_recover = subparsers.add_parser(
        "recover",
        help="[v0.3] Rebuild casetrack.db by replaying provenance.jsonl",
    )
    p_recover.add_argument("--project-dir", default=None,
                           help="Casetrack project directory (or use --project <id>)")
    p_recover.add_argument(
        "--from", dest="from_",
        help="Alternative provenance path (default: <project>/provenance.jsonl)",
    )
    p_recover.add_argument("--force", action="store_true",
                           help="Overwrite the existing casetrack.db before replay")
    p_recover.add_argument(
        "--permit-partial",
        action="store_true",
        help="Exit 0 even if some entries couldn't be replayed (default: exit 2)",
    )

    # ── migrate ──
    p_migrate = subparsers.add_parser(
        "migrate",
        help="Convert a v0.2 flat manifest TSV into a v0.3 casetrack project",
    )
    p_migrate.add_argument("--flat", required=True, help="Path to source flat manifest TSV")
    p_migrate.add_argument("--patient-col", required=True, help="Column name in --flat that holds patient_id")
    p_migrate.add_argument("--specimen-col", required=True, help="Column name in --flat that holds specimen_id")
    p_migrate.add_argument("--assay-col", required=True, help="Column name in --flat that holds assay_id")
    p_migrate.add_argument(
        "--metadata-map",
        help="Manual overrides, e.g. 'patient:age,brca_status;specimen:tissue_site'",
    )
    p_migrate.add_argument("--out-dir", required=True, help="Output project directory")
    p_migrate.add_argument(
        "--project-name",
        help="Project name written into casetrack.toml (default: out-dir basename)",
    )
    p_migrate.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing casetrack.db in --out-dir",
    )

    # ── migrate-project-id (v0.6 Part B beta — proposal 0005 §7) ──
    p_mpid = subparsers.add_parser(
        "migrate-project-id",
        help="[v0.6] Bring legacy projects into the v0.6 identity scheme: "
             "derive a project_id slug, write the project_meta row, register "
             "in ~/.casetrack/registry.json. Idempotent.",
    )
    g_mpid_target = p_mpid.add_mutually_exclusive_group()
    g_mpid_target.add_argument(
        "--project-dir", help="Single-project mode: migrate this directory."
    )
    g_mpid_target.add_argument(
        "--scan", help="Batch mode: walk this root and migrate every "
                       "casetrack project found.",
    )
    p_mpid.add_argument(
        "--project-id",
        help="Explicit slug (single-project mode only). Default: derived from "
             "[project] name or directory basename.",
    )
    p_mpid.add_argument(
        "--yes", action="store_true",
        help="Non-interactive mode — accept the auto-derived suggestion "
             "without prompting. Required for --scan; recommended for "
             "automation.",
    )

    # ── v0.4 QC subcommands (censor, uncensor, qc-history, migrate-qc) ──
    from casetrack_qc.cli import build_qc_subparsers as _build_qc_subparsers
    _build_qc_subparsers(subparsers)

    # ── v0.6 lineage subcommands (migrate-lineage, add-batch, link-sources) ──
    from casetrack_lineage.cli import build_lineage_subparsers as _build_lineage_subparsers
    _build_lineage_subparsers(subparsers)

    # ── v0.7 lifecycle subcommands (project set-status, project status, migrate-status) ──
    from casetrack_lifecycle.cli import build_lifecycle_subparsers as _build_lifecycle_subparsers
    _build_lifecycle_subparsers(subparsers)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "init": cmd_init,
        "append": cmd_append,
        "status": cmd_status,
        "validate": cmd_validate,
        "log": cmd_log,
        "schema": cmd_schema,
        "rerun": cmd_rerun,
        "dashboard": cmd_dashboard,
        "add-metadata": cmd_add_metadata,
        "projects": cmd_projects,
        "query": cmd_query,
        "export": cmd_export,
        "migrate": cmd_migrate,
        "migrate-project-id": cmd_migrate_project_id,
        "register": cmd_register,
        "doctor": cmd_doctor_project,
        "recover": cmd_recover_project,
    }

    # v0.4 QC dispatch merges in without touching existing entries.
    from casetrack_qc.cli import qc_command_dispatch as _qc_command_dispatch
    commands.update(_qc_command_dispatch())

    # v0.6 lineage dispatch merges in without touching existing entries.
    from casetrack_lineage.cli import lineage_command_dispatch as _lineage_command_dispatch
    commands.update(_lineage_command_dispatch())

    # v0.7 lifecycle dispatch merges in without touching existing entries.
    from casetrack_lifecycle.cli import lifecycle_command_dispatch as _lifecycle_command_dispatch
    commands.update(_lifecycle_command_dispatch())

    commands[args.command](args)


if __name__ == "__main__":
    main()
