#!/usr/bin/env python3
"""
Summarize modkit pileup output into a manifest-ready TSV.

This is the "Phase 2" script — it takes raw tool output and distills it
into a small number of columns suitable for the manifest. The full results
stay in the per-sample results directory.

Usage:
    python summarize_modkit.py \
        --input results/modkit/SAMPLE_001/pileup.bed \
        --sample SAMPLE_001 \
        --output results/modkit/SAMPLE_001/summary.tsv

Output TSV format:
    sample_id    modkit_total_cpg    modkit_mean_meth    modkit_median_meth    modkit_meth_above_50pct
"""

import argparse
import sys
import pandas as pd
import numpy as np
from pathlib import Path


def summarize_pileup(bed_path: str) -> dict:
    """
    Parse modkit pileup BED and compute summary statistics.

    Modkit pileup BED columns (standard):
        chrom, start, end, mod_code, score, strand,
        start2, end2, color, n_valid, pct_modified, ...

    Adjust column indices if your modkit version differs.
    """
    try:
        df = pd.read_csv(
            bed_path,
            sep="\t",
            header=None,
            comment="#",
            usecols=[0, 1, 2, 3, 4, 5, 9, 10],
            names=["chrom", "start", "end", "mod_code", "score", "strand", "n_valid", "pct_modified"],
            dtype={"chrom": str},
        )
    except Exception as e:
        print(f"Error reading pileup BED: {e}", file=sys.stderr)
        sys.exit(1)

    # Filter to CpG modifications (5mC)
    cpg = df[df["mod_code"].str.contains("m", case=False, na=False)].copy()

    if cpg.empty:
        return {
            "modkit_total_cpg": 0,
            "modkit_mean_meth": np.nan,
            "modkit_median_meth": np.nan,
            "modkit_meth_above_50pct": np.nan,
            "modkit_mean_coverage": np.nan,
        }

    pct = cpg["pct_modified"].astype(float)
    cov = cpg["n_valid"].astype(float)

    return {
        "modkit_total_cpg": len(cpg),
        "modkit_mean_meth": round(pct.mean() / 100, 4),
        "modkit_median_meth": round(pct.median() / 100, 4),
        "modkit_meth_above_50pct": round((pct > 50).mean(), 4),
        "modkit_mean_coverage": round(cov.mean(), 1),
    }


def main():
    parser = argparse.ArgumentParser(description="Summarize modkit pileup for casetrack manifest")
    parser.add_argument("--input", required=True, help="Path to modkit pileup BED file")
    parser.add_argument("--sample", required=True, help="Sample ID")
    parser.add_argument("--output", required=True, help="Output TSV path")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"Error: input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    # Summarize
    stats = summarize_pileup(args.input)

    # Build single-row dataframe
    row = {"sample_id": args.sample, **stats}
    df = pd.DataFrame([row])

    # Write
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, sep="\t", index=False)

    print(f"Summary for {args.sample}: {stats}")


if __name__ == "__main__":
    main()
