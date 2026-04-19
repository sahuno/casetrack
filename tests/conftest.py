"""Shared pytest fixtures for casetrack tests.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import pytest

# Make `casetrack.py` importable as a module when tests run from repo root or elsewhere.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import casetrack  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_casetrack_registry(tmp_path_factory, monkeypatch):
    """Auto-isolate the v0.6 registry per test so tests never touch
    ~/.casetrack/registry.json. casetrack.cmd_init writes a registry entry
    on every project init, and concurrent tests would clobber each other
    via the shared default path. Routed via the CASETRACK_REGISTRY env var
    that _registry_path() honors.
    """
    reg_dir = tmp_path_factory.mktemp("registry")
    monkeypatch.setenv("CASETRACK_REGISTRY", str(reg_dir / "registry.json"))


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Empty project directory."""
    return tmp_path


@pytest.fixture
def samples_file(tmp_project: Path) -> Path:
    """samples.txt with 5 sample IDs + a blank line and a comment line."""
    p = tmp_project / "samples.txt"
    p.write_text(
        "# comment line to be skipped\n"
        "SAMPLE_01\n"
        "SAMPLE_02\n"
        "SAMPLE_03\n"
        "\n"
        "SAMPLE_04\n"
        "SAMPLE_05\n"
    )
    return p


@pytest.fixture
def metadata_file(tmp_project: Path) -> Path:
    """Optional metadata TSV keyed on sample_id."""
    p = tmp_project / "metadata.tsv"
    pd.DataFrame(
        {
            "sample_id": ["SAMPLE_01", "SAMPLE_02", "SAMPLE_03", "SAMPLE_04", "SAMPLE_05"],
            "tissue": ["tumor", "normal", "tumor", "normal", "tumor"],
            "batch": [1, 1, 2, 2, 2],
        }
    ).to_csv(p, sep="\t", index=False)
    return p


@pytest.fixture
def init_args_factory(tmp_project: Path, samples_file: Path):
    """Factory returning an argparse.Namespace suitable for cmd_init."""

    def _factory(**overrides):
        defaults = dict(
            manifest=str(tmp_project / "manifest.tsv"),
            samples=str(samples_file),
            key="sample_id",
            metadata=None,
            cols=None,
            force=False,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    return _factory


@pytest.fixture
def initialized_manifest(tmp_project: Path, samples_file: Path) -> Path:
    """A manifest pre-initialized via cmd_init."""
    ns = argparse.Namespace(
        manifest=str(tmp_project / "manifest.tsv"),
        samples=str(samples_file),
        key="sample_id",
        metadata=None,
        cols=None,
        force=False,
    )
    casetrack.cmd_init(ns)
    return tmp_project / "manifest.tsv"


@pytest.fixture
def append_args_factory(tmp_project: Path):
    """Factory returning an argparse.Namespace suitable for cmd_append."""

    def _factory(**overrides):
        defaults = dict(
            manifest=str(tmp_project / "manifest.tsv"),
            results=str(tmp_project / "results.tsv"),
            key="sample_id",
            analysis="modkit",
            overwrite=False,
            allow_new=False,
            yes=False,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    return _factory


def write_tsv(path: Path, df: pd.DataFrame) -> Path:
    """Helper to write a TSV and return the path."""
    df.to_csv(path, sep="\t", index=False)
    return path
