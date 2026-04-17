"""End-to-end ethics-override gate.

Proposal 0002 §7.2 #4. Reversing a consent_revoked event requires
``--ethics-override --yes`` AND a reason referencing an IRB ref / re-consent /
ISO date. The resulting provenance entry is tagged ``action='ethics_override'``
with ``ethics: true``.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-17
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

import casetrack


CASETRACK_BIN = [sys.executable, str(Path(__file__).resolve().parent.parent / "casetrack.py")]


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        CASETRACK_BIN + args, check=check, capture_output=True, text=True
    )


@pytest.fixture
def revoked(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    ns = argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="test", force=False,
    )
    casetrack.cmd_init(ns)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.execute("INSERT INTO patients (patient_id) VALUES ('HGSOC099')")
    finally:
        conn.close()
    _run([
        "censor", "--project-dir", str(proj),
        "--level", "patient", "--id", "HGSOC099",
        "--kind", "consent_revoked", "--reason", "withdrew",
        "--withdrawal-date", "2026-03-15",
    ])
    return proj


def test_ethics_override_blocks_without_gate(revoked: Path):
    res = _run([
        "uncensor", "--project-dir", str(revoked),
        "--level", "patient", "--id", "HGSOC099",
        "--reason", "IRB ref 2026-042",
    ], check=False)
    assert res.returncode == 2
    assert "ethics-override" in res.stderr


def test_ethics_override_blocks_without_valid_reason(revoked: Path):
    res = _run([
        "uncensor", "--project-dir", str(revoked),
        "--level", "patient", "--id", "HGSOC099",
        "--ethics-override", "--yes",
        "--reason", "patient said so",
    ], check=False)
    assert res.returncode == 2


def test_ethics_override_succeeds_with_irb_and_gate(revoked: Path):
    res = _run([
        "uncensor", "--project-dir", str(revoked),
        "--level", "patient", "--id", "HGSOC099",
        "--ethics-override", "--yes",
        "--reason", "re-consent signed; IRB ref 2026-042",
    ])
    assert res.returncode == 0

    lines = (revoked / "provenance.jsonl").read_text().splitlines()
    entries = [json.loads(l) for l in lines]
    ethics_entry = next(e for e in entries if e.get("action") == "ethics_override")
    assert ethics_entry["ethics"] is True
    assert ethics_entry["level"] == "patient"
    assert ethics_entry["entity_id"] == "HGSOC099"


def test_ethics_override_accepts_reason_with_iso_date(revoked: Path):
    res = _run([
        "uncensor", "--project-dir", str(revoked),
        "--level", "patient", "--id", "HGSOC099",
        "--ethics-override", "--yes",
        "--reason", "resolved on 2026-05-01 per committee decision",
    ])
    assert res.returncode == 0
