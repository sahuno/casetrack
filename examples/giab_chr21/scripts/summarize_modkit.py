#!/usr/bin/env python3
"""summarize_modkit.py — distill a modkit `bedMethyl` file into a casetrack
append-ready per-assay TSV.

Output columns: assay_id, n_cpg_sites, mean_meth, median_meth, pct_high_conf.

The bedMethyl format from `modkit pileup --cpg` is a BED9+ table with columns:
  chrom start end mod_code score strand start end color N_valid_cov frac_mod
  N_mod N_canonical N_other_mod N_delete N_fail N_diff N_no_call

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-16
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path


def _iter_mC_rows(path: Path):
    """Yield (frac_mod, n_valid_cov) tuples for every 5mC row in `path`."""
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 11:
                continue
            mod_code = parts[3]
            if mod_code != "m":  # only 5-methylcytosine rows
                continue
            try:
                frac_mod = float(parts[10])
            except ValueError:
                continue
            try:
                n_valid = int(parts[9])
            except ValueError:
                n_valid = 0
            yield frac_mod, n_valid


def summarize(path: Path, high_conf_cov: int = 5) -> dict:
    frac_mods = []
    n_high = 0
    for frac, cov in _iter_mC_rows(path):
        frac_mods.append(frac)
        if cov >= high_conf_cov:
            n_high += 1

    n = len(frac_mods)
    if not n:
        return {"n_cpg_sites": 0, "mean_meth": 0.0,
                "median_meth": 0.0, "pct_high_conf": 0.0}
    return {
        "n_cpg_sites": n,
        "mean_meth": round(sum(frac_mods) / (n * 100), 4),
        "median_meth": round(statistics.median(frac_mods) / 100, 4),
        "pct_high_conf": round(100.0 * n_high / n, 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--assay-id", required=True, help="assay_id key column value")
    ap.add_argument("--input", required=True, help="modkit bedMethyl file")
    ap.add_argument("--output", required=True, help="TSV output path")
    ap.add_argument("--high-conf-cov", type=int, default=5,
                    help="Min valid coverage for a site to count as high-confidence "
                         "(default: 5)")
    args = ap.parse_args()

    stats = summarize(Path(args.input), high_conf_cov=args.high_conf_cov)

    cols = ["assay_id", "n_cpg_sites", "mean_meth", "median_meth", "pct_high_conf"]
    with open(args.output, "w") as f:
        f.write("\t".join(cols) + "\n")
        f.write("\t".join([args.assay_id] + [str(stats[c]) for c in cols[1:]]) + "\n")
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
