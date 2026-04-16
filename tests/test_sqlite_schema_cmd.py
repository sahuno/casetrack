"""Tests for `casetrack schema {show,dump,check,apply}` in project mode.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

import casetrack


def _init_ns(project_dir: Path, template: str = "hgsoc") -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir), samples=None, key="sample_id",
        metadata=None, cols=None, from_template=template, project_name=None, force=False,
    )


def _schema_ns(project_dir: Path, action: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None, project_dir=str(project_dir),
        action=action, fmt="table",
    )


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(p, template="hgsoc"))
    return p


# ── show / dump ───────────────────────────────────────────────────────────────


def test_show_prints_toml_verbatim(proj: Path, capsys):
    casetrack.cmd_schema(_schema_ns(proj, "show"))
    out = capsys.readouterr().out
    assert "[project]" in out
    assert "[levels.patient]" in out
    assert "schema_v" in out


def test_show_defaults_to_show(proj: Path, capsys):
    """Omitting the positional action should still show the schema."""
    casetrack.cmd_schema(_schema_ns(proj, None))
    out = capsys.readouterr().out
    assert "[project]" in out


def test_dump_regenerates_valid_schema(proj: Path, capsys):
    casetrack.cmd_schema(_schema_ns(proj, "dump"))
    out = capsys.readouterr().out
    # The dump output should itself parse as a valid schema.
    dumped = proj / "dumped.toml"
    dumped.write_text(out)
    parsed = casetrack.load_schema(dumped)
    assert set(parsed["levels"]) == set(casetrack.LEVEL_ORDER)


# ── check / apply ─────────────────────────────────────────────────────────────


def test_check_clean_project(proj: Path, capsys):
    casetrack.cmd_schema(_schema_ns(proj, "check"))
    out = capsys.readouterr().out
    assert "Schema OK" in out


def test_check_detects_declared_but_missing_in_db(proj: Path, capsys):
    toml = (proj / "casetrack.toml").read_text()
    patched = toml.replace(
        "[levels.specimen]",
        'new_col = { type = "REAL" }\n\n[levels.specimen]',
        1,
    )
    (proj / "casetrack.toml").write_text(patched)

    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_schema(_schema_ns(proj, "check"))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "new_col" in err
    assert "schema apply" in err


def test_apply_adds_missing_columns_and_bumps_schema_v(proj: Path, capsys):
    toml_path = proj / "casetrack.toml"
    patched = toml_path.read_text().replace(
        "[levels.specimen]",
        'new_tmb = { type = "REAL" }\n\n[levels.specimen]',
        1,
    )
    toml_path.write_text(patched)

    casetrack.cmd_schema(_schema_ns(proj, "apply"))
    out = capsys.readouterr().out
    assert "schema_v 1 → 2" in out

    # Column now exists in DB.
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(patients)").fetchall()}
    finally:
        conn.close()
    assert "new_tmb" in cols

    # schema_v bumped in TOML.
    updated = toml_path.read_text()
    assert "schema_v = 2" in updated

    # check now passes.
    casetrack.cmd_schema(_schema_ns(proj, "check"))
    assert "Schema OK" in capsys.readouterr().out


def test_apply_noop_when_clean(proj: Path, capsys):
    casetrack.cmd_schema(_schema_ns(proj, "apply"))
    out = capsys.readouterr().out
    assert "nothing to apply" in out


def test_apply_logs_provenance(proj: Path):
    toml_path = proj / "casetrack.toml"
    patched = toml_path.read_text().replace(
        "[levels.specimen]",
        'new_tmb = { type = "REAL" }\n\n[levels.specimen]',
        1,
    )
    toml_path.write_text(patched)

    casetrack.cmd_schema(_schema_ns(proj, "apply"))

    entries = [json.loads(ln) for ln in (proj / "provenance.jsonl").read_text().splitlines()]
    apply_entry = next(e for e in entries if e["action"] == "schema_apply")
    assert apply_entry["schema_v_before"] == 1
    assert apply_entry["schema_v_after"] == 2
    assert any("new_tmb" in s for s in apply_entry["sql"])


def test_check_ignores_analysis_columns(proj: Path, tmp_path: Path, capsys):
    """Columns added by `casetrack append` live in the DB only — not an issue."""
    # Register an assay, then append a made-up analysis.
    casetrack.cmd_register(argparse.Namespace(
        project_dir=str(proj), level="patient", id="P1",
        parent=None, meta=None, allow_new_parent=False, yes=False,
    ))
    casetrack.cmd_register(argparse.Namespace(
        project_dir=str(proj), level="specimen", id="S1", parent="P1",
        meta="tissue_site=tumor", allow_new_parent=False, yes=False,
    ))
    casetrack.cmd_register(argparse.Namespace(
        project_dir=str(proj), level="assay", id="A1", parent="S1",
        meta="assay_type=WGS", allow_new_parent=False, yes=False,
    ))
    results = tmp_path / "r.tsv"
    results.write_text("assay_id\tmean_meth\nA1\t0.5\n")
    casetrack.cmd_append(argparse.Namespace(
        manifest=None, project_dir=str(proj), results=str(results),
        key="sample_id", analysis="modkit", level=None, col_type=None,
        overwrite=False, allow_new=False, yes=False,
    ))
    # mean_meth was added to the DB but not declared in TOML — should NOT flag.
    casetrack.cmd_schema(_schema_ns(proj, "check"))
    assert "Schema OK" in capsys.readouterr().out


def test_unknown_action_errors(proj: Path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        casetrack.cmd_schema(_schema_ns(proj, "bogus"))
    assert excinfo.value.code == 1
    assert "unknown schema action" in capsys.readouterr().err


# ── Flat-mode still works ─────────────────────────────────────────────────────


def test_flat_schema_still_works(initialized_manifest: Path, capsys):
    """cmd_schema must continue to read the JSON sidecar in flat mode."""
    # Flat schema shows "No schema file found" before any append, which is
    # a valid flat-mode path that must not accidentally route through project.
    ns = argparse.Namespace(
        manifest=str(initialized_manifest), project_dir=None,
        action=None, fmt="table",
    )
    with pytest.raises(SystemExit):
        casetrack.cmd_schema(ns)
    err = capsys.readouterr().err
    assert "No schema file found" in err
