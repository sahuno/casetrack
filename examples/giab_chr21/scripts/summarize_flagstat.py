#!/usr/bin/env python3
"""summarize_flagstat.py — parse `samtools flagstat` output into a casetrack
append-ready per-assay TSV.

Contract:
  Input  : the text output of `samtools flagstat <bam>` (or a saved .flagstat)
  Output : TSV with columns: assay_id, total_reads, mapped_reads, mapped_pct,
           properly_paired_reads, duplicates_reads, supplementary_reads

Usage:
  samtools flagstat sample.bam > sample.flagstat
  python3 summarize_flagstat.py --assay-id A001 \\
      --input sample.flagstat --output A001_flagstat.tsv

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-16
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def _first_int(line: str) -> int:
    m = re.match(r"^(\d+)", line.strip())
    if not m:
        raise ValueError(f"no leading integer in flagstat line: {line!r}")
    return int(m.group(1))


def parse_flagstat(text: str) -> dict:
    """Parse the canonical samtools flagstat output. Returns a dict of ints."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("empty flagstat output")
    # The first N lines all begin with "<int> + <int> <metric>". Grab each.
    def _find(keyword: str) -> int:
        for ln in lines:
            if keyword in ln:
                return _first_int(ln)
        raise ValueError(f"flagstat: keyword {keyword!r} not found")

    total = _find("in total")
    mapped = _find("mapped (")
    properly_paired = _find("properly paired")
    duplicates = _find("duplicates")
    supplementary = _find("supplementary")

    return {
        "total_reads": total,
        "mapped_reads": mapped,
        "mapped_pct": round(100.0 * mapped / total, 2) if total else 0.0,
        "properly_paired_reads": properly_paired,
        "duplicates_reads": duplicates,
        "supplementary_reads": supplementary,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--assay-id", required=True, help="assay_id key column value")
    ap.add_argument("--input", required=True, help="samtools flagstat output file")
    ap.add_argument("--output", required=True, help="TSV output path")
    args = ap.parse_args()

    text = Path(args.input).read_text()
    stats = parse_flagstat(text)

    cols = ["assay_id", "total_reads", "mapped_reads", "mapped_pct",
            "properly_paired_reads", "duplicates_reads", "supplementary_reads"]
    with open(args.output, "w") as f:
        f.write("\t".join(cols) + "\n")
        f.write("\t".join([args.assay_id] + [str(stats[c]) for c in cols[1:]]) + "\n")
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
