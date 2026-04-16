#!/usr/bin/env python3
"""mock_modkit_summary.py — emit a fake modkit methylation summary TSV.

Produces plausible per-assay methylation metrics by hashing the assay_id
so the same assay always gets the same numbers. Used by the fast mock
demo path (no modkit run required).

Output columns: assay_id, n_cpg_sites, mean_meth, median_meth, pct_high_conf

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-16
"""
from __future__ import annotations

import argparse
import hashlib


def _mock(assay_id: str) -> dict:
    """Deterministic 'analysis' — same assay_id always → same values."""
    seed = int(hashlib.md5(assay_id.encode()).hexdigest()[:8], 16)
    rng = _LCG(seed)
    return {
        "n_cpg_sites": 300_000 + rng.next_int(80_000),
        "mean_meth": round(0.40 + 0.30 * rng.next_float(), 3),
        "median_meth": round(0.38 + 0.30 * rng.next_float(), 3),
        "pct_high_conf": round(90 + 8 * rng.next_float(), 2),
    }


class _LCG:
    """Deterministic pseudo-random sequence — stdlib's `random` suffices but
    this avoids importing a module we don't otherwise need."""

    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def next_int(self, bound: int) -> int:
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return self.state % bound

    def next_float(self) -> float:
        return self.next_int(10_000) / 10_000


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--assay-ids", required=True,
                    help="Comma-separated list of assay_ids")
    ap.add_argument("--output", required=True, help="TSV output path")
    args = ap.parse_args()

    assay_ids = [a.strip() for a in args.assay_ids.split(",") if a.strip()]
    cols = ["assay_id", "n_cpg_sites", "mean_meth", "median_meth", "pct_high_conf"]
    with open(args.output, "w") as f:
        f.write("\t".join(cols) + "\n")
        for aid in assay_ids:
            stats = _mock(aid)
            f.write("\t".join([aid] + [str(stats[c]) for c in cols[1:]]) + "\n")


if __name__ == "__main__":
    main()
