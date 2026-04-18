#!/usr/bin/env python3
"""mock_scrna_summary.py — deterministic scRNA summary per specimen.

Reads an assay_id from the CLI, hashes it to a stable RNG seed, and emits
a casetrack-append-ready TSV with plausible 10x Chromium-style metrics:

    assay_id, n_cells, median_umis_per_cell, median_genes_per_cell,
    pct_mito, pct_ribo, doublet_rate, qc_pass, qc_fail_reason, qc_warn

qc_pass is autoflag-driven: fails if n_cells < 1000 or pct_mito > 20%.
We bias HGSOC_SIM_02-normal's metrics so it also fails at the scRNA
level — gives the broken-pair story a cross-assay dimension.

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-18
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def _mock_metrics(assay_id: str) -> dict:
    """Hash-deterministic metrics — same assay_id always gets the same numbers."""
    seed = int(hashlib.md5(assay_id.encode()).hexdigest()[:8], 16)
    rng = _LCG(seed)

    # HGSOC_SIM_02-normal is the deliberate cross-assay failure: 200 cells
    # (below 1000 threshold) + slightly elevated mito. Other specimens get
    # "healthy" ranges.
    if "HGSOC_SIM_02_normal" in assay_id:
        n_cells = 150 + rng.next_int(80)            # 150-230, below 1000 → fails
        pct_mito = 18.0 + rng.next_float() * 4      # 18-22, borderline warn/fail
    else:
        n_cells = 6000 + rng.next_int(3000)          # healthy 6-9k cells
        pct_mito = 4.5 + rng.next_float() * 6        # 4.5-10.5, healthy
    median_umis = 2500 + rng.next_int(1500)
    median_genes = int(median_umis * (0.55 + rng.next_float() * 0.15))
    pct_ribo = 12.0 + rng.next_float() * 8
    doublet_rate = 0.04 + rng.next_float() * 0.04
    return {
        "n_cells": n_cells,
        "median_umis_per_cell": median_umis,
        "median_genes_per_cell": median_genes,
        "pct_mito": round(pct_mito, 2),
        "pct_ribo": round(pct_ribo, 2),
        "doublet_rate": round(doublet_rate, 4),
    }


def _autoflag(stats: dict) -> dict:
    reasons = []
    if stats["n_cells"] < 1000:
        reasons.append(f"n_cells={stats['n_cells']} < 1000")
    if stats["pct_mito"] > 20.0:
        reasons.append(f"pct_mito={stats['pct_mito']:.1f}% > 20 (high mitochondrial)")
    if reasons:
        return {"qc_pass": "false", "qc_fail_reason": "; ".join(reasons), "qc_warn": ""}
    warn = ""
    if stats["pct_mito"] > 15.0:
        warn = f"pct_mito={stats['pct_mito']:.1f}% within 5pp of fail threshold"
    return {"qc_pass": "true", "qc_fail_reason": "", "qc_warn": warn}


class _LCG:
    """Tiny deterministic PRNG so we don't pull in `random`."""
    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def next_int(self, bound: int) -> int:
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return self.state % max(1, bound)

    def next_float(self) -> float:
        return self.next_int(10_000) / 10_000


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--assay-id", required=True,
                    help="scRNA assay_id (e.g. HGSOC_SIM_01_tumor-scRNA-RNA-R01)")
    ap.add_argument("--output", required=True, help="per-assay summary TSV")
    args = ap.parse_args()

    stats = _mock_metrics(args.assay_id)
    flags = _autoflag(stats)
    stats.update(flags)

    cols = ["assay_id", "n_cells", "median_umis_per_cell", "median_genes_per_cell",
            "pct_mito", "pct_ribo", "doublet_rate",
            "qc_pass", "qc_fail_reason", "qc_warn"]
    with open(args.output, "w") as f:
        f.write("\t".join(cols) + "\n")
        f.write("\t".join([args.assay_id] + [str(stats[c]) for c in cols[1:]]) + "\n")
    print(f"Wrote {args.output} (n_cells={stats['n_cells']}, qc_pass={stats['qc_pass']})",
          file=sys.stderr)


if __name__ == "__main__":
    main()
