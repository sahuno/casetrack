"""Tests for `casetrack dashboard`.

Confirms the generated HTML is self-contained (no external URLs), escapes
untrusted text, covers all the sections promised in the synopsis, and renders
cleanly when provenance/schema sidecars are absent.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from conftest import write_tsv


# ── helpers ────────────────────────────────────────────────────────────────────


def _append_modkit(initialized_manifest: Path, tmp_project: Path, ids):
    r = tmp_project / f"r_{'_'.join(ids)}.tsv"
    write_tsv(
        r,
        pd.DataFrame(
            {"sample_id": list(ids), "modkit_mean_meth": [0.5] * len(ids)}
        ),
    )
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(initialized_manifest), results=str(r),
        key="sample_id", analysis="modkit",
        overwrite=False, allow_new=False,
    ))


def _append_tldr(initialized_manifest: Path, tmp_project: Path, ids):
    r = tmp_project / f"t_{'_'.join(ids)}.tsv"
    write_tsv(
        r,
        pd.DataFrame({"sample_id": list(ids), "tldr_l1_count": [3] * len(ids)}),
    )
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(initialized_manifest), results=str(r),
        key="sample_id", analysis="tldr",
        overwrite=False, allow_new=False,
    ))


def _dash_ns(manifest: Path, output: Path, key="sample_id"):
    return argparse.Namespace(
        manifest=str(manifest), output=str(output), key=key
    )


class _WellFormedHTML(HTMLParser):
    """Light-weight check that tag open/close pairs balance for the tags we
    generate (`html`, `head`, `body`, `table`, `tr`). Not a full validator."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []
        self.void = {"meta", "br", "hr", "img", "input", "link"}

    def handle_starttag(self, tag, attrs):
        if tag not in self.void:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack:
            self.errors.append(f"unmatched </{tag}>")
            return
        if self.stack[-1] != tag:
            # Allow implicit closure of block tags if the closer matches something
            # deeper in the stack (browsers are lenient about this).
            if tag in self.stack:
                while self.stack and self.stack[-1] != tag:
                    self.stack.pop()
                self.stack.pop()
            else:
                self.errors.append(f"unmatched </{tag}> (top={self.stack[-1]})")
        else:
            self.stack.pop()


# ── Core behavior ──────────────────────────────────────────────────────────────


def test_dashboard_writes_file(initialized_manifest: Path, tmp_project: Path,
                               tmp_path: Path, capsys):
    _append_modkit(initialized_manifest, tmp_project, ["SAMPLE_01", "SAMPLE_02"])
    _append_tldr(initialized_manifest, tmp_project, ["SAMPLE_01", "SAMPLE_02", "SAMPLE_03"])
    capsys.readouterr()

    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(initialized_manifest, out))
    assert out.exists()
    html_doc = out.read_text()

    # Title + counts
    assert "casetrack dashboard" in html_doc
    assert ">5</div><div class=\"label\">Samples</div>" in html_doc
    assert ">2</div><div class=\"label\">Analyses</div>" in html_doc

    # Overall completion: (2 + 3) / (5 * 2) = 50%
    assert "50.0%</div>" in html_doc.replace("\n", "")

    # Each analysis shows up
    assert "modkit" in html_doc
    assert "tldr" in html_doc

    # Heatmap: one row per sample, one column per analysis
    assert html_doc.count('<tr><th>SAMPLE_01</th>') == 1
    assert html_doc.count('<tr><th>SAMPLE_05</th>') == 1


def test_dashboard_is_self_contained(initialized_manifest: Path, tmp_path: Path):
    _append_modkit(initialized_manifest, initialized_manifest.parent, ["SAMPLE_01"])
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(initialized_manifest, out))
    doc = out.read_text()

    # No external network references — safe for scp/offline HPC.
    forbidden = re.compile(
        r"""(https?://|src\s*=\s*["']//|<script[^>]+src=|<link[^>]+href\s*=\s*["']http)""",
        re.IGNORECASE,
    )
    assert not forbidden.search(doc), "dashboard references external resources"

    # No inline <script> at all (we deliberately ship zero JS for now).
    assert "<script" not in doc.lower()


def test_dashboard_html_structure_balanced(initialized_manifest: Path,
                                           tmp_project: Path, tmp_path: Path):
    _append_modkit(initialized_manifest, tmp_project, ["SAMPLE_01", "SAMPLE_02"])
    _append_tldr(initialized_manifest, tmp_project, ["SAMPLE_03"])
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(initialized_manifest, out))

    parser = _WellFormedHTML()
    parser.feed(out.read_text())
    # Stack should unwind once </html> is consumed.
    assert parser.stack == [], f"unclosed tags: {parser.stack}"
    assert not parser.errors, parser.errors


def test_dashboard_shows_missing_samples(initialized_manifest: Path,
                                        tmp_project: Path, tmp_path: Path):
    _append_modkit(initialized_manifest, tmp_project, ["SAMPLE_01"])
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(initialized_manifest, out))
    doc = out.read_text()

    # The 4 missing samples must be listed in the collapsible section.
    for sid in ("SAMPLE_02", "SAMPLE_03", "SAMPLE_04", "SAMPLE_05"):
        assert sid in doc


def test_dashboard_escapes_untrusted_text(tmp_project: Path, tmp_path: Path):
    """A sample ID containing HTML must be escaped — no injected markup."""
    samples = tmp_project / "s.txt"
    attack = "<script>alert(1)</script>"
    samples.write_text(f"{attack}\nSAMPLE_OK\n")
    manifest = tmp_project / "m.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    # Add an analysis so the attacking sample ID lands in the heatmap + missing list.
    r = tmp_project / "r.tsv"
    write_tsv(r, pd.DataFrame({"sample_id": ["SAMPLE_OK"], "modkit_mean_meth": [0.5]}))
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(manifest), results=str(r),
        key="sample_id", analysis="modkit",
        overwrite=False, allow_new=False,
    ))

    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(manifest, out))
    doc = out.read_text()

    # The raw <script>alert(1)</script> payload must NOT appear verbatim.
    # The dashboard itself ships zero <script> tags, so any occurrence would
    # have to come from the attack input.
    assert attack not in doc
    # But the escaped form must appear (in the heatmap row header and missing list).
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in doc


def test_dashboard_handles_empty_analyses(initialized_manifest: Path,
                                          tmp_path: Path):
    """Manifest with no appended analyses should still render cleanly."""
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(initialized_manifest, out))
    doc = out.read_text()
    assert "No analyses recorded yet" in doc
    # Heatmap section still present but empty.
    assert "Per-sample heatmap" in doc


def test_dashboard_handles_missing_sidecars(tmp_project: Path,
                                            samples_file: Path, tmp_path: Path):
    """If provenance + schema files don't exist, dashboard must not crash."""
    manifest = tmp_project / "m.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples_file),
        key="sample_id", metadata=None, cols=None, force=False,
    ))
    # Remove the provenance sidecar that cmd_init wrote.
    prov = Path(str(manifest) + casetrack.PROVENANCE_SUFFIX)
    if prov.exists():
        prov.unlink()

    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(manifest, out))
    doc = out.read_text()
    assert "No provenance log found." in doc


def test_dashboard_renders_provenance_timeline(initialized_manifest: Path,
                                               tmp_project: Path, tmp_path: Path):
    _append_modkit(initialized_manifest, tmp_project, ["SAMPLE_01"])
    _append_tldr(initialized_manifest, tmp_project, ["SAMPLE_01", "SAMPLE_02"])
    out = tmp_path / "dash.html"
    casetrack.cmd_dashboard(_dash_ns(initialized_manifest, out))
    doc = out.read_text()

    # Both actions should appear in the timeline.
    assert "APPEND" in doc
    assert "INIT" in doc
    # Reverse chronological — the most recent APPEND (tldr) should appear
    # before the INIT in the document text.
    assert doc.index("tldr") < doc.index(">INIT<")


def test_dashboard_missing_manifest_exits(tmp_project: Path, tmp_path: Path):
    ns = argparse.Namespace(
        manifest=str(tmp_project / "nope.tsv"),
        output=str(tmp_path / "x.html"),
        key="sample_id",
    )
    with pytest.raises(SystemExit):
        casetrack.cmd_dashboard(ns)


def test_dashboard_cli_smoke(tmp_project: Path, samples_file: Path, tmp_path: Path):
    """End-to-end: invoke via subprocess, open produced HTML."""
    manifest = tmp_project / "manifest.tsv"
    subprocess.run(
        [sys.executable, str(Path(casetrack.__file__)), "init",
         "--manifest", str(manifest), "--samples", str(samples_file)],
        check=True, capture_output=True, text=True,
    )

    out = tmp_path / "dash.html"
    res = subprocess.run(
        [sys.executable, str(Path(casetrack.__file__)), "dashboard",
         "--manifest", str(manifest), "--output", str(out)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert out.exists()
    assert "<html" in out.read_text()
