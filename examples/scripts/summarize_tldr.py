#!/usr/bin/env python3
"""
Summarize TLDR output into a manifest-ready TSV.

TLDR detects non-reference transposable element insertions from ONT long reads.
This script distills the VCF/table output into key metrics for the manifest.

Usage:
    python summarize_tldr.py \
        --input results/tldr/SAMPLE_001/SAMPLE_001.table.txt \
        --sample SAMPLE_001 \
        --output results/tldr/SAMPLE_001/summary.tsv

Output TSV format:
    sample_id    tldr_total_insertions    tldr_l1_insertions    tldr_full_length    tldr_mean_support
"""

import argparse
import sys
import pandas as pd
import numpy as np
from pathlib import Path


def summarize_tldr(table_path: str) -> dict:
    """
    Parse TLDR table output and compute summary statistics.

    TLDR .table.txt columns include:
        Chrom, Start, End, UUID, Family, Subfamily, ...
        NumReads, SupportReads, ...
    """
    try:
        df = pd.read_csv(table_path, sep="\t", comment="#")
    except Exception as e:
        print(f"Error reading TLDR table: {e}", file=sys.stderr)
        sys.exit(1)

    # Normalize column names (TLDR versions vary)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    total = len(df)

    # Count L1 insertions
    l1_mask = pd.Series([False] * total)
    for col in ["family", "subfamily", "te_family"]:
        if col in df.columns:
            l1_mask = l1_mask | df[col].astype(str).str.contains("L1|LINE", case=False, na=False)
            break

    l1_count = int(l1_mask.sum())

    # Count full-length L1 (>6kb)
    full_length = 0
    for col in ["te_len", "insert_len", "te_length"]:
        if col in df.columns:
            lengths = pd.to_numeric(df.loc[l1_mask, col], errors="coerce")
            full_length = int((lengths > 6000).sum())
            break

    # Mean read support
    mean_support = np.nan
    for col in ["numreads", "supportreads", "num_reads", "support"]:
        if col in df.columns:
            mean_support = round(pd.to_numeric(df[col], errors="coerce").mean(), 1)
            break

    return {
        "tldr_total_insertions": total,
        "tldr_l1_insertions": l1_count,
        "tldr_full_length": full_length,
        "tldr_mean_support": mean_support,
    }


def main():
    parser = argparse.ArgumentParser(description="Summarize TLDR output for casetrack manifest")
    parser.add_argument("--input", required=True, help="Path to TLDR .table.txt file")
    parser.add_argument("--sample", required=True, help="Sample ID")
    parser.add_argument("--output", required=True, help="Output TSV path")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"Error: input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    stats = summarize_tldr(args.input)
    row = {"sample_id": args.sample, **stats}
    df = pd.DataFrame([row])

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, sep="\t", index=False)

    print(f"Summary for {args.sample}: {stats}")


if __name__ == "__main__":
    main()
