"""Tests for proposal 0006 steps 4–6.

Step 4: casetrack rerun --include-sources
Step 5: casetrack status --show-lineage + validate lineage invariants
Step 6: casetrack export --include-lineage

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


# ── shared fixture ─────────────────────────────────────────────────────────────

def _run(args, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "casetrack"] + args,
        capture_output=True, text=True, cwd=cwd,
    )


@pytest.fixture()
def proj(tmp_path):
    """Initialised project with lineage schema + 2 run assays → 1 specimen."""
    r = _run(["init", "--project-dir", str(tmp_path), "--from-template", "hgsoc", "--bare"])
    assert r.returncode == 0, r.stderr

    # Register patient → specimen → 2 run assays
    for reg_args in [
        ["register", "--project-dir", str(tmp_path), "--level", "patient",
         "--id", "P01", "--meta", "age=50,sex=F"],
        ["register", "--project-dir", str(tmp_path), "--level", "specimen",
         "--id", "P01_tumor", "--parent", "P01", "--meta", "tissue_site=tumor"],
        ["register", "--project-dir", str(tmp_path), "--level", "assay",
         "--id", "P01_run1", "--parent", "P01_tumor", "--meta", "assay_type=ONT"],
        ["register", "--project-dir", str(tmp_path), "--level", "assay",
         "--id", "P01_run2", "--parent", "P01_tumor", "--meta", "assay_type=ONT"],
    ]:
        rr = _run(reg_args)
        assert rr.returncode == 0, f"register failed: {rr.stderr}"

    # Migrate lineage schema
    r2 = _run(["migrate-lineage", "--project-dir", str(tmp_path)])
    assert r2.returncode == 0, r2.stderr

    # Link sources: both run assays → specimen (Mode B)
    r3 = _run(["link-sources", "--project-dir", str(tmp_path),
               "--sources", "P01_run1,P01_run2", "--specimen", "P01_tumor"])
    assert r3.returncode == 0, r3.stderr

    return tmp_path


# ── Step 4: rerun --include-sources ───────────────────────────────────────────

def test_rerun_include_sources_list_only(proj):
    """--include-sources with --list-only prints source assay IDs below main IDs."""
    # Make a fake results TSV so we can append a done timestamp for the run assays.
    results = proj / "results.tsv"
    results.write_text("specimen_id\tmodkit_done\nP01_tumor\t2026-04-20T00:00:00\n")
    # Don't append yet — specimen still has NULL modkit_done.
    r = _run([
        "rerun",
        "--project-dir", str(proj),
        "--level", "specimen",
        "--analysis", "modkit",
        "--list-only",
        "--include-sources",
    ])
    assert r.returncode == 0, r.stderr
    output = r.stdout
    assert "P01_tumor" in output
    assert "P01_run1" in output
    assert "P01_run2" in output
    assert "source assays" in output.lower() or "source" in output.lower()


def test_rerun_include_sources_no_lineage_table(tmp_path):
    """--include-sources gracefully skips if assay_sources doesn't exist."""
    _run(["init", "--project-dir", str(tmp_path), "--from-template", "hgsoc", "--bare"])
    _run(["register", "--project-dir", str(tmp_path), "--level", "patient",
          "--id", "P01", "--meta", "age=50,sex=F"])
    _run(["register", "--project-dir", str(tmp_path), "--level", "specimen",
          "--id", "P01_tumor", "--parent", "P01", "--meta", "tissue_site=tumor"])
    r = _run([
        "rerun",
        "--project-dir", str(tmp_path),
        "--level", "specimen",
        "--analysis", "modkit",
        "--list-only",
        "--include-sources",
    ])
    # Should not crash — either lists P01_tumor (missing) or says none missing.
    assert r.returncode == 0, r.stderr
    assert "P01_tumor" in r.stdout or "No specimen" in r.stdout


def test_rerun_no_missing_no_sources(proj, tmp_path):
    """When all items are done and no sources, print the 'no missing' message."""
    # Append modkit_done for the specimen.
    results = tmp_path / "results.tsv"
    results.write_text("specimen_id\tmodkit_done\nP01_tumor\t2026-04-20T00:00:00\n")
    _run(["append", "--project-dir", str(proj),
          "--level", "specimen", "--analysis", "modkit", "--results", str(results)])
    r = _run([
        "rerun",
        "--project-dir", str(proj),
        "--level", "specimen",
        "--analysis", "modkit",
        "--list-only",
        "--include-sources",
    ])
    assert r.returncode == 0
    assert "No specimen" in r.stdout or "no" in r.stdout.lower()


# ── Step 5: status --show-lineage ─────────────────────────────────────────────

def test_status_show_lineage(proj):
    """--show-lineage prints a lineage section with source assay tree."""
    r = _run([
        "status",
        "--project-dir", str(proj),
        "--show-lineage",
    ])
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "P01_tumor" in out
    assert "P01_run1" in out
    assert "P01_run2" in out
    assert "Mode B" in out or "specimen" in out.lower()


def test_status_show_lineage_no_table(tmp_path):
    """--show-lineage with no assay_sources table prints a helpful message."""
    _run(["init", "--project-dir", str(tmp_path), "--from-template", "hgsoc", "--bare"])
    r = _run(["status", "--project-dir", str(tmp_path), "--show-lineage"])
    assert r.returncode == 0
    assert "migrate-lineage" in r.stdout or "not found" in r.stdout


def test_status_show_lineage_empty_table(tmp_path):
    """--show-lineage with empty assay_sources prints no-links message."""
    _run(["init", "--project-dir", str(tmp_path), "--from-template", "hgsoc", "--bare"])
    _run(["migrate-lineage", "--project-dir", str(tmp_path)])
    r = _run(["status", "--project-dir", str(tmp_path), "--show-lineage"])
    assert r.returncode == 0
    assert "link-sources" in r.stdout or "no source" in r.stdout.lower()


# ── Step 5: validate lineage invariants ───────────────────────────────────────

def test_validate_clean_lineage(proj):
    """Validate passes when assay_sources is internally consistent."""
    r = _run(["validate", "--project-dir", str(proj)])
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate_orphan_source_assay(proj):
    """Validate fails when assay_sources references a non-existent source assay."""
    db = proj / "casetrack.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO assay_sources (source_assay_id, consumer_specimen_id) "
        "VALUES ('GHOST_ASSAY', 'P01_tumor')"
    )
    conn.commit()
    conn.close()
    r = _run(["validate", "--project-dir", str(proj)])
    assert r.returncode != 0
    assert "GHOST_ASSAY" in r.stdout or "GHOST_ASSAY" in r.stderr


def test_validate_orphan_batch_id(proj):
    """Validate fails when assays.batch_id references a non-existent batch."""
    db = proj / "casetrack.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE assays SET batch_id = 'NONEXISTENT_BATCH' WHERE assay_id = 'P01_run1'"
    )
    conn.commit()
    conn.close()
    r = _run(["validate", "--project-dir", str(proj)])
    assert r.returncode != 0
    assert "NONEXISTENT_BATCH" in r.stdout or "NONEXISTENT_BATCH" in r.stderr


# ── Step 6: export --include-lineage ──────────────────────────────────────────

def test_export_include_lineage_directory(proj, tmp_path):
    """--include-lineage exports assay_sources.tsv and batches.tsv alongside levels."""
    out_dir = tmp_path / "export"
    r = _run([
        "export",
        "--project-dir", str(proj),
        "--output", str(out_dir),
        "--include-lineage",
    ])
    assert r.returncode == 0, r.stderr
    assert (out_dir / "assay_sources.tsv").exists()
    assert (out_dir / "batches.tsv").exists()
    assert (out_dir / "assays.tsv").exists()


def test_export_without_lineage_flag_no_extra_files(proj, tmp_path):
    """Without --include-lineage, assay_sources.tsv is NOT written."""
    out_dir = tmp_path / "export"
    r = _run([
        "export",
        "--project-dir", str(proj),
        "--output", str(out_dir),
    ])
    assert r.returncode == 0, r.stderr
    assert not (out_dir / "assay_sources.tsv").exists()


def test_export_include_lineage_content(proj, tmp_path):
    """Exported assay_sources.tsv contains the expected link rows."""
    import csv

    out_dir = tmp_path / "export"
    _run([
        "export", "--project-dir", str(proj),
        "--output", str(out_dir), "--include-lineage",
    ])
    src_path = out_dir / "assay_sources.tsv"
    with open(src_path) as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    sources = {r["source_assay_id"] for r in rows}
    assert "P01_run1" in sources
    assert "P01_run2" in sources
    consumers = {r["consumer_specimen_id"] for r in rows}
    assert "P01_tumor" in consumers
