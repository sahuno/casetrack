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

# Default pragma values for every SQLite connection casetrack opens.
SQLITE_BUSY_TIMEOUT_MS = 30000


# ── v0.3 TOML schema ───────────────────────────────────────────────────────────
#
# `casetrack.toml` declares the three-level schema (patient/specimen/assay).
# The DB is regenerable from TOML + provenance.jsonl (see §9.4 of proposal 0001),
# so TOML is the git-tracked source of schema truth.


class SchemaError(ValueError):
    """Raised when a casetrack.toml schema is malformed or internally inconsistent."""


def _blank_toml_template(project_name: str) -> str:
    """Minimal schema with just the primary keys and enforced parent FKs."""
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    return f"""[project]
name     = "{project_name}"
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
"""


def _hgsoc_toml_template(project_name: str) -> str:
    """Template matching the example in proposal 0001 §6."""
    now = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    return f"""[project]
name     = "{project_name}"
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
"""


TEMPLATES = {"blank": _blank_toml_template, "hgsoc": _hgsoc_toml_template}


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
    toml_text = TEMPLATES[template](project_name)
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

    conn = open_project_db(db_path)
    try:
        apply_schema(conn, schema)
    finally:
        conn.close()

    # Empty provenance log — touch to create.
    if not prov_path.exists():
        prov_path.touch()

    if not gitignore_path.exists():
        gitignore_path.write_text(_project_gitignore_contents())

    log_project_provenance(project_dir, {
        "action": "init_project",
        "transaction_id": _new_transaction_id(),
        "template": template,
        "project_name": project_name,
        "schema_v_before": 0,
        "schema_v_after": schema["project"]["schema_v"],
        "sql": schema_to_ddl(schema),
    })

    print(
        f"Initialized casetrack project at {project_dir}/\n"
        f"  - {PROJECT_TOML_NAME}  ({template} template)\n"
        f"  - {PROJECT_DB_NAME}    (three levels: {', '.join(LEVEL_ORDER)})\n"
        f"  - {PROJECT_PROVENANCE_NAME}\n"
        f"  - {PROJECT_GITIGNORE_NAME}"
    )


def _project_gitignore_contents() -> str:
    """Contents of `.gitignore` for a fresh project (proposal 0001 §5, §19 Q6)."""
    return (
        "# casetrack project .gitignore — proposal 0001 Q6\n"
        "# DB is binary and regenerable from casetrack.toml + provenance.jsonl.\n"
        f"{PROJECT_DB_NAME}\n"
        f"{PROJECT_DB_NAME}-wal\n"
        f"{PROJECT_DB_NAME}-shm\n"
        "exports/\n"
    )


def cmd_init_flat(args):
    """Initialize a new flat-manifest (v0.2 style)."""
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
    """Append analysis results as new columns to the manifest."""
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
    """Show completion status across analyses."""
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
    """Validate manifest integrity."""
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
    """Show provenance log entries."""
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
    """Show which analysis added which columns."""
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
        "name": manifest_path.parent.name,
        "path": str(manifest_path),
        "samples": n_samples,
        "analyses": len(done_cols),
        "completed_cells": completed,
        "total_cells": total_cells,
        "pct": pct,
    }


def cmd_projects(args):
    """Scan `--root` for manifests and summarize cross-project status."""
    root = Path(args.root)
    if not root.exists() or not root.is_dir():
        print(f"Error: root not found or not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    manifests = _find_project_manifests(root, args.pattern, args.max_depth)

    projects = []
    for mpath in manifests:
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
        print("project\tpath\tsamples\tanalyses\tcompleted_cells\ttotal_cells\tpct")
        for p in projects:
            print(
                f"{p['name']}\t{p['path']}\t{p['samples']}\t{p['analyses']}\t"
                f"{p['completed_cells']}\t{p['total_cells']}\t{p['pct']}"
            )
        return

    # table
    name_w = max(12, max(len(p["name"]) for p in projects))
    header = f"{'Project':<{name_w}}  {'Samples':>7}  {'Analyses':>8}  {'Complete':>9}"
    sep = "─" * (name_w + 32)
    print("")
    print(header)
    print(sep)
    for p in projects:
        print(
            f"{p['name']:<{name_w}}  {p['samples']:>7}  {p['analyses']:>8}  "
            f"{p['pct']:>7.1f}% "
            + ("█" * int(p["pct"] / 10) + "░" * (10 - int(p["pct"] / 10)))
        )
    print(sep)
    print(f"{len(projects)} project(s) under {root}")


def cmd_add_metadata(args):
    """Attach metadata columns to an existing manifest without the analysis
    append path: no `_done` timestamp, no schema entry.

    Default collision policy is strict — refuse to touch existing columns.
    Use `--fill-only` to fill NaN cells (smart merge) or `--overwrite` to
    replace existing columns wholesale.
    """
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
    """Run a SQL query against one manifest (--manifest) or a union of many
    (--root) via DuckDB. The manifest is exposed as table `_` by default."""
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
    """Generate a self-contained HTML dashboard from the manifest."""
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
    """Generate (or submit) SLURM commands for samples missing a given analysis."""
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
    """Export manifest to other formats."""
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
        "--force",
        action="store_true",
        help="Overwrite existing manifest (flat) or casetrack.db (project)",
    )

    # ── append ──
    p_append = subparsers.add_parser("append", help="Append analysis results to manifest")
    p_append.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_append.add_argument("--results", required=True, help="Path to results TSV")
    p_append.add_argument("--key", default="sample_id", help="Key column to join on (default: sample_id)")
    p_append.add_argument("--analysis", required=True, help="Name of this analysis (e.g. modkit_methylation)")
    p_append.add_argument("--overwrite", action="store_true", help="Overwrite existing columns")
    p_append.add_argument("--allow-new", action="store_true", help="Allow new sample IDs not in manifest (requires --yes to commit)")
    p_append.add_argument("--yes", action="store_true", help="Confirm --allow-new additions non-interactively")

    # ── status ──
    p_status = subparsers.add_parser("status", help="Show completion status")
    p_status.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_status.add_argument("--key", default="sample_id", help="Key column name")
    p_status.add_argument("--analysis", help="Filter to a specific analysis")
    p_status.add_argument("--fmt", choices=["table", "tsv", "json"], default="table", help="Output format")

    # ── validate ──
    p_validate = subparsers.add_parser("validate", help="Validate manifest integrity")
    p_validate.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_validate.add_argument("--key", default="sample_id", help="Key column name")

    # ── log ──
    p_log = subparsers.add_parser("log", help="Show provenance log")
    p_log.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_log.add_argument("--last", type=int, help="Show only the last N entries")

    # ── schema ──
    p_schema = subparsers.add_parser("schema", help="Show column-to-analysis mapping")
    p_schema.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_schema.add_argument("--fmt", choices=["table", "json"], default="table", help="Output format")

    # ── query ──
    p_query = subparsers.add_parser(
        "query",
        help="Run SQL over one manifest or a union of many (DuckDB-backed)",
    )
    g_target = p_query.add_mutually_exclusive_group(required=True)
    g_target.add_argument("--manifest", help="Path to a single manifest TSV")
    g_target.add_argument("--root", help="Scan a root for manifests (cross-project query)")
    p_query.add_argument("sql", help="SQL query; reference the manifest as `_` (or --as NAME)")
    p_query.add_argument("--as", dest="as_name", default=None,
                         help="SQL table/view alias for the manifest (default: _)")
    p_query.add_argument("--pattern", default="manifest.tsv",
                         help="With --root: manifest filename pattern (default: manifest.tsv)")
    p_query.add_argument("--max-depth", type=int, default=4,
                         help="With --root: maximum directory depth (default: 4)")
    p_query.add_argument("--fmt", choices=["table", "tsv", "csv", "json"], default="table",
                         help="Output format (default: table)")
    p_query.add_argument("--output", help="Write results to this file instead of stdout")

    # ── projects ──
    p_projects = subparsers.add_parser(
        "projects", help="Cross-project overview: scan a root for manifests"
    )
    p_projects.add_argument("--root", required=True, help="Root directory to scan")
    p_projects.add_argument("--pattern", default="manifest.tsv",
                            help="Manifest filename pattern (default: manifest.tsv)")
    p_projects.add_argument("--max-depth", type=int, default=4,
                            help="Maximum directory depth to scan (default: 4)")
    p_projects.add_argument("--key", default="sample_id", help="Key column name")
    p_projects.add_argument("--fmt", choices=["table", "tsv", "json"], default="table",
                            help="Output format")

    # ── add-metadata ──
    p_meta = subparsers.add_parser(
        "add-metadata",
        help="Add metadata columns to an existing manifest (no analysis _done column)",
    )
    p_meta.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_meta.add_argument("--metadata", required=True, help="Path to metadata TSV (must include key column)")
    p_meta.add_argument("--key", default="sample_id", help="Key column to join on (default: sample_id)")
    p_meta.add_argument("--fill-only", action="store_true", help="On column collision, fill NaN cells only (smart merge)")
    p_meta.add_argument("--overwrite", action="store_true", help="On column collision, replace existing columns")
    p_meta.add_argument("--allow-new", action="store_true", help="Allow new sample IDs not in manifest (requires --yes to commit)")
    p_meta.add_argument("--yes", action="store_true", help="Confirm --allow-new additions non-interactively")

    # ── dashboard ──
    p_dash = subparsers.add_parser(
        "dashboard", help="Generate a self-contained HTML dashboard"
    )
    p_dash.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_dash.add_argument("--output", required=True, help="Output HTML file path")
    p_dash.add_argument("--key", default="sample_id", help="Key column name (default: sample_id)")

    # ── rerun ──
    p_rerun = subparsers.add_parser(
        "rerun",
        help="Emit or submit sbatch commands for samples missing a given analysis",
    )
    p_rerun.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_rerun.add_argument("--analysis", required=True, help="Analysis whose _done column to check")
    p_rerun.add_argument("--script", required=True, help="sbatch script path (receives sample_id, manifest)")
    p_rerun.add_argument("--key", default="sample_id", help="Key column name (default: sample_id)")
    p_rerun.add_argument("--submit", action="store_true", help="Actually invoke sbatch (default: dry-run)")
    p_rerun.add_argument("--list-only", action="store_true", help="Print bare sample IDs, not sbatch commands")
    p_rerun.add_argument("--extra", help="Extra args appended to each sbatch command (quoted string)")

    # ── export ──
    p_export = subparsers.add_parser("export", help="Export manifest to other formats")
    p_export.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_export.add_argument("--output", required=True, help="Output path (.xlsx, .csv, .json, .parquet)")

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
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
