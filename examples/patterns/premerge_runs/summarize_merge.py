#!/usr/bin/env python3
"""summarize_merge.py — emit the specimen-level summary TSV that
`casetrack append --level specimen --analysis merge` consumes.

Reads a samtools flagstat run on the merged BAM (the merge's own QC)
plus the list of input BAM paths. Output columns:
  specimen_id, merged_bam_path, n_input_bams, total_reads, mapped_reads,
  mapped_pct, qc_pass, qc_fail_reason, qc_warn

qc_pass is false if the merged BAM looks catastrophically broken
(zero reads, mapped_pct below threshold) — same autoflag semantics as
the pre-merge summarizer.

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-17
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Share the flagstat parser + autoflag helper with the pre-merge summarizer.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from summarize_premerge_flagstat import parse_flagstat, autoflag  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--specimen-id", required=True)
    ap.add_argument("--merged-bam-path", required=True,
                    help="Absolute path to the merged BAM (stored verbatim)")
    ap.add_argument("--input-list", required=True,
                    help="Path to a file containing one input BAM path per line")
    ap.add_argument("--flagstat-on-merged", required=True,
                    help="samtools flagstat output run on the merged BAM")
    ap.add_argument("--output", required=True)
    ap.add_argument("--min-total-reads", type=int, default=1_000_000)
    ap.add_argument("--min-mapped-pct", type=float, default=95.0)
    args = ap.parse_args()

    inputs = [ln.strip() for ln in Path(args.input_list).read_text().splitlines()
              if ln.strip()]
    stats = parse_flagstat(Path(args.flagstat_on_merged).read_text())
    flags = autoflag(stats, min_total_reads=args.min_total_reads,
                     min_mapped_pct=args.min_mapped_pct)

    cols = ["specimen_id", "merged_bam_path", "n_input_bams",
            "total_reads", "mapped_reads", "mapped_pct",
            "qc_pass", "qc_fail_reason", "qc_warn"]
    row = {
        "specimen_id": args.specimen_id,
        "merged_bam_path": args.merged_bam_path,
        "n_input_bams": len(inputs),
        "total_reads": stats["total_reads"],
        "mapped_reads": stats["mapped_reads"],
        "mapped_pct": stats["mapped_pct"],
        "qc_pass": flags["qc_pass"],
        "qc_fail_reason": flags["qc_fail_reason"],
        "qc_warn": flags["qc_warn"],
    }
    with open(args.output, "w") as f:
        f.write("\t".join(cols) + "\n")
        f.write("\t".join(str(row[c]) for c in cols) + "\n")
    print(f"Wrote {args.output} (n_inputs={len(inputs)}, qc_pass={flags['qc_pass']})",
          file=sys.stderr)


if __name__ == "__main__":
    main()
