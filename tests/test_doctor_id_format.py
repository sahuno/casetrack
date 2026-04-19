"""Tests for `casetrack doctor --id-format` (proposal 0005 Part A, §7).

Scan-only hierarchy ID health check. Exits 0 if all IDs conform to the
schema's format rules, 1 if any malformed IDs are found. No mutations.

Covers:
- Clean project → exit 0, no violations reported.
- Legacy malformed IDs (inserted directly via SQL, bypassing the v0.6
  register validator) → exit 1, each violation reported.
- Rename-suggestion heuristic produces a valid slug for auto-cleanable
  cases (whitespace, shell metas); None for others (e.g. non-ASCII when
  unicode opt-in is off).
- --fmt tsv emits a parsable header + one row per violation.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-19
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

import casetrack


# ── fixtures ──────────────────────────────────────────────────────────────────


def _init_ns(project_dir: Path, template: str = "hgsoc") -> argparse.Namespace:
    return argparse.Namespace(
        manifest=None,
        project_dir=str(project_dir),
        samples=None,
        key="sample_id",
        metadata=None,
        cols=None,
        from_template=template,
        project_name=None,
        force=False,
        bare=False,
    )


def _doctor_ns(project_dir: Path, *, id_format: bool = True,
               fmt: str = "table") -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir),
        workers=None,
        writes=None,
        id_format=id_format,
        fmt=fmt,
    )


@pytest.fixture
def hgsoc_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="hgsoc"))
    return proj


def _insert_raw(project_dir: Path, table: str, col: str, value: str) -> None:
    """Bypass the register validator to simulate a legacy malformed row."""
    conn = sqlite3.connect(str(project_dir / "casetrack.db"))
    try:
        conn.execute(
            f"INSERT INTO {table} ({col}) VALUES (?)", (value,)
        )
        conn.commit()
    finally:
        conn.close()


# ── _suggest_clean_id (pure function) ─────────────────────────────────────────


@pytest.mark.parametrize("bad,expected", [
    ("HG 006",        "HG_006"),
    ("P 01",          "P_01"),
    ("HG006 ",        "HG006"),
    (" HG006",        "HG006"),
    ("P01;rm",        "P01_rm"),
    ("P01/v2",        "P01_v2"),
    ("P01'x",         "P01_x"),
    ("P@01",          "P_01"),
    ("-P01",          "P01"),
    (".hidden",       "hidden"),
    ("P01\tfoo",      "P01_foo"),
    ("P01\nfoo",      "P01_foo"),
])
def test_suggest_clean_id_recovers_valid_slug(bad: str, expected: str):
    assert casetrack._suggest_clean_id(bad) == expected


@pytest.mark.parametrize("bad", [
    "",                # collapses to empty
    "   ",             # all whitespace → empty after strip
    "αβγ",             # non-ASCII — cleaner replaces whole string with _
    "...",             # reserved literal-like
    "---",             # all separators → empty after strip
])
def test_suggest_clean_id_returns_none_when_unsafe(bad: str):
    assert casetrack._suggest_clean_id(bad) is None


def test_suggest_clean_id_truncates_to_64():
    bad = "A B C " * 20  # 100+ chars with whitespace
    cleaned = casetrack._suggest_clean_id(bad)
    assert cleaned is not None
    assert len(cleaned) <= 64


# ── integration: clean project → exit 0 ───────────────────────────────────────


def test_clean_project_exits_zero(hgsoc_project: Path, capsys):
    # Register a few valid IDs.
    casetrack.cmd_register(argparse.Namespace(
        project_dir=str(hgsoc_project), level="patient", id="HG006",
        parent=None, meta=None, allow_new_parent=False, yes=False,
    ))
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_doctor_project(_doctor_ns(hgsoc_project))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "conform" in out


# ── integration: malformed legacy IDs → exit 1 + report ──────────────────────


def test_malformed_legacy_ids_exit_one(hgsoc_project: Path, capsys):
    # Simulate legacy IDs from a pre-v0.6 DB that slipped past validation.
    _insert_raw(hgsoc_project, "patients", "patient_id", "HG 006")
    _insert_raw(hgsoc_project, "patients", "patient_id", "P01;rm")
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_doctor_project(_doctor_ns(hgsoc_project))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "HG 006" in out
    assert "P01;rm" in out
    # Both have auto-safe suggestions.
    assert "HG_006" in out
    assert "P01_rm" in out
    # Guidance appears once at the end.
    assert "migration TSV" in out or "manual rename" in out


def test_malformed_id_unsafe_suggestion(hgsoc_project: Path, capsys):
    # Non-ASCII without allow_unicode_ids — cleaner collapses to nothing
    # usable, so the report shows "no safe suggestion".
    _insert_raw(hgsoc_project, "patients", "patient_id", "αβγ")
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_doctor_project(_doctor_ns(hgsoc_project))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "αβγ" in out
    assert "no safe suggestion" in out


# ── --fmt tsv output ─────────────────────────────────────────────────────────


def test_tsv_format_is_parsable(hgsoc_project: Path, capsys):
    _insert_raw(hgsoc_project, "patients", "patient_id", "HG 006")
    _insert_raw(hgsoc_project, "patients", "patient_id", "αβγ")
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_doctor_project(_doctor_ns(hgsoc_project, fmt="tsv"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0] == "level\tid\tsuggestion\trule"
    assert len(lines) == 3  # header + 2 rows
    rows = [line.split("\t") for line in lines[1:]]
    ids = {r[1] for r in rows}
    assert ids == {"HG 006", "αβγ"}
    # HG 006 has a suggestion, αβγ does not.
    suggestions = {r[1]: r[2] for r in rows}
    assert suggestions["HG 006"] == "HG_006"
    assert suggestions["αβγ"] == ""


def test_tsv_clean_project_exits_zero(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_doctor_project(_doctor_ns(hgsoc_project, fmt="tsv"))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.strip().splitlines() if line.strip()]
    # Header only, no data rows.
    assert lines == ["level\tid\tsuggestion\trule"]


# ── scan covers all three levels ─────────────────────────────────────────────


def test_scan_covers_all_three_levels(tmp_path: Path, capsys):
    # Use the blank template — minimal NOT NULL constraints so we can
    # insert bare hierarchy rows for a multi-level scan.
    proj = tmp_path / "proj"
    casetrack.cmd_init(_init_ns(proj, template="blank"))
    casetrack.cmd_register(argparse.Namespace(
        project_dir=str(proj), level="patient", id="HG006",
        parent=None, meta=None, allow_new_parent=False, yes=False,
    ))
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        conn.execute(
            "INSERT INTO specimens (specimen_id, patient_id) VALUES (?, ?)",
            ("SPEC A", "HG006"),
        )
        conn.execute(
            "INSERT INTO assays (assay_id, specimen_id, assay_type) "
            "VALUES (?, ?, ?)",
            ("ASSAY/1", "SPEC A", "ONT"),
        )
        conn.commit()
    finally:
        conn.close()
    # Drain any stdout emitted by init / register before we capture the
    # doctor report — otherwise their banners get prepended to the TSV.
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_doctor_project(_doctor_ns(proj, fmt="tsv"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    levels = {line.split("\t")[0] for line in out.strip().splitlines()[1:]}
    assert levels == {"specimen", "assay"}


# ── invalid --fmt value ──────────────────────────────────────────────────────


def test_invalid_fmt_exits_two(hgsoc_project: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        casetrack.cmd_doctor_project(_doctor_ns(hgsoc_project, fmt="json"))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "table|tsv" in err
