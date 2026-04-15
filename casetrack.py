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
    casetrack dashboard --manifest manifest.tsv --output dashboard.html
    casetrack export    --manifest manifest.tsv --output out.xlsx

Author: Samuel Ahuno (sahuno)
"""

import argparse
import datetime
import fcntl
import html
import json
import os
import sys
import hashlib
from pathlib import Path

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

def log_provenance(manifest_path: str, entry: dict):
    """Append a provenance record as a JSONL line."""
    log_path = manifest_path + PROVENANCE_SUFFIX
    entry["timestamp"] = datetime.datetime.now().strftime(TIMESTAMP_FMT)
    entry["user"] = os.environ.get("USER", "unknown")
    entry["hostname"] = os.environ.get("HOSTNAME", "unknown")
    entry["slurm_job_id"] = os.environ.get("SLURM_JOB_ID", None)
    entry["slurm_array_task_id"] = os.environ.get("SLURM_ARRAY_TASK_ID", None)

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
            merged.loc[mask, col] = new_values.loc[mask]
    return merged


# ── Core Commands ──────────────────────────────────────────────────────────────

def cmd_init(args):
    """Initialize a new manifest from a sample list."""
    manifest_path = args.manifest

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
                f"Use --allow-new to add them as new rows.",
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
            timeline_html.append(
                f'<li>{esc(ts)} · <b>{esc(action)}</b>{analysis_str} · {esc(user)}'
                f'{job_str}{detail}</li>'
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
    p_init = subparsers.add_parser("init", help="Initialize a new manifest")
    p_init.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_init.add_argument("--samples", required=True, help="Text file with one sample_id per line")
    p_init.add_argument("--key", default="sample_id", help="Key column name (default: sample_id)")
    p_init.add_argument("--metadata", help="Optional TSV with additional sample metadata")
    p_init.add_argument("--cols", help="Comma-separated list of empty columns to pre-create")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing manifest")

    # ── append ──
    p_append = subparsers.add_parser("append", help="Append analysis results to manifest")
    p_append.add_argument("--manifest", required=True, help="Path to manifest TSV")
    p_append.add_argument("--results", required=True, help="Path to results TSV")
    p_append.add_argument("--key", default="sample_id", help="Key column to join on (default: sample_id)")
    p_append.add_argument("--analysis", required=True, help="Name of this analysis (e.g. modkit_methylation)")
    p_append.add_argument("--overwrite", action="store_true", help="Overwrite existing columns")
    p_append.add_argument("--allow-new", action="store_true", help="Allow new sample IDs not in manifest")

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
        "export": cmd_export,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
