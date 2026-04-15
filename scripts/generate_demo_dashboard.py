#!/usr/bin/env python3
"""Build a realistic casetrack demo project, render its dashboard, and
emit the HTML to `docs/examples/dashboard_demo.html`.

Reproducible: deterministic RNG seed + fixed sample list + fixed analyses.
Safe to re-run — it works in a tmp dir and only writes the final HTML.

Usage:
    python3 scripts/generate_demo_dashboard.py [--output PATH]

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-15
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import casetrack  # noqa: E402


def _init(manifest: Path, sample_ids: list[str]) -> None:
    samples = manifest.parent / "samples.txt"
    samples.write_text("\n".join(sample_ids) + "\n")
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples),
        key="sample_id", metadata=None, cols=None, force=False,
    ))


def _append(manifest: Path, analysis: str, df: pd.DataFrame) -> None:
    r = manifest.parent / f"r_{analysis}.tsv"
    df.to_csv(r, sep="\t", index=False)
    casetrack.cmd_append(argparse.Namespace(
        manifest=str(manifest), results=str(r),
        key="sample_id", analysis=analysis,
        overwrite=False, allow_new=False, yes=False,
    ))


def build_demo(workdir: Path) -> Path:
    rng = np.random.default_rng(seed=42)

    # 24 ONT samples, two cohorts — realistic shape for an L1 / methylation project.
    ids = [f"MC_TUMOR_{i:03d}"  for i in range(1, 13)] + \
          [f"MC_NORMAL_{i:03d}" for i in range(1, 13)]

    manifest = workdir / "manifest.tsv"
    _init(manifest, ids)

    # A realistic completion pattern: earlier analyses more complete
    # than later, a handful of samples lagging across the board.
    analyses = [
        ("dorado_basecalling",   24, "mean_qscore",         lambda n: rng.uniform(15, 22, n).round(2)),
        ("minimap2_alignment",   24, "mapped_frac",         lambda n: rng.uniform(0.92, 0.99, n).round(3)),
        ("modkit_methylation",   22, "modkit_mean_meth",    lambda n: rng.uniform(0.55, 0.85, n).round(3)),
        ("tldr_insertions",      20, "tldr_l1_count",       lambda n: rng.integers(0, 40, n)),
        ("xtea_somatic_l1",      14, "xtea_events",         lambda n: rng.integers(0, 12, n)),
        ("qc_metrics",           24, "qc_pass",             lambda n: np.full(n, True)),
    ]

    for analysis, n_done, col, gen in analyses:
        df = pd.DataFrame({
            "sample_id": ids[:n_done],
            col:         gen(n_done),
        })
        _append(manifest, analysis, df)

    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--output", type=Path,
        default=REPO_ROOT / "docs" / "examples" / "dashboard_demo.html",
        help="Where to write the rendered HTML.",
    )
    args = ap.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    # Work in a temp dir so no state pollutes the repo.
    with tempfile.TemporaryDirectory() as d:
        workdir = Path(d)
        manifest = build_demo(workdir)

        tmp_html = workdir / "dashboard_demo.html"
        casetrack.cmd_dashboard(argparse.Namespace(
            manifest=str(manifest), output=str(tmp_html), key="sample_id",
        ))

        shutil.copy(tmp_html, args.output)

    size_kb = os.path.getsize(args.output) / 1024
    print(f"Wrote {args.output} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
