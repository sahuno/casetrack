#!/usr/bin/env python3
"""summarize_premerge_flagstat.py — parse `samtools flagstat` into a per-assay
TSV with QC-autoflag columns that `casetrack append` consumes.

Output columns:
  assay_id, total_reads, mapped_reads, mapped_pct, duplicates_reads,
  supplementary_reads, qc_pass, qc_fail_reason, qc_warn

If total_reads < --min-total-reads OR mapped_pct < --min-mapped-pct,
qc_pass = false and qc_fail_reason is populated. These thresholds are the
universal "the flowcell at least produced something meaningful" gates;
tune per project.

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-17
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
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("empty flagstat output")

    def _find(keyword: str) -> int:
        for ln in lines:
            if keyword in ln:
                return _first_int(ln)
        raise ValueError(f"flagstat: keyword {keyword!r} not found")

    total = _find("in total")
    mapped = _find("mapped (")
    duplicates = _find("duplicates")
    supplementary = _find("supplementary")
    return {
        "total_reads": total,
        "mapped_reads": mapped,
        "mapped_pct": round(100.0 * mapped / total, 2) if total else 0.0,
        "duplicates_reads": duplicates,
        "supplementary_reads": supplementary,
    }


def autoflag(stats: dict, *, min_total_reads: int, min_mapped_pct: float) -> dict:
    """Decide qc_pass / qc_fail_reason / qc_warn from the parsed stats."""
    reasons: list[str] = []
    if stats["total_reads"] < min_total_reads:
        reasons.append(
            f"total_reads={stats['total_reads']:,} < {min_total_reads:,} (min)"
        )
    if stats["mapped_pct"] < min_mapped_pct:
        reasons.append(
            f"mapped_pct={stats['mapped_pct']:.2f} < {min_mapped_pct} (min)"
        )
    if reasons:
        return {"qc_pass": "false", "qc_fail_reason": "; ".join(reasons), "qc_warn": ""}
    warn = ""
    # Soft warning for low-ish mapped_pct (90-95 range by default).
    warn_threshold = min_mapped_pct + 3.0
    if stats["mapped_pct"] < warn_threshold:
        warn = f"mapped_pct={stats['mapped_pct']:.2f} within 3pp of fail threshold"
    return {"qc_pass": "true", "qc_fail_reason": "", "qc_warn": warn}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--assay-id", required=True)
    ap.add_argument("--input", required=True, help="samtools flagstat text")
    ap.add_argument("--output", required=True)
    ap.add_argument("--min-total-reads", type=int, default=1_000_000,
                    help="Min total_reads for qc_pass (default: 1_000_000)")
    ap.add_argument("--min-mapped-pct", type=float, default=95.0,
                    help="Min mapped_pct for qc_pass (default: 95.0)")
    args = ap.parse_args()

    stats = parse_flagstat(Path(args.input).read_text())
    flags = autoflag(stats, min_total_reads=args.min_total_reads,
                     min_mapped_pct=args.min_mapped_pct)
    stats.update(flags)

    cols = ["assay_id", "total_reads", "mapped_reads", "mapped_pct",
            "duplicates_reads", "supplementary_reads",
            "qc_pass", "qc_fail_reason", "qc_warn"]
    with open(args.output, "w") as f:
        f.write("\t".join(cols) + "\n")
        f.write("\t".join([args.assay_id] + [str(stats[c]) for c in cols[1:]]) + "\n")
    print(f"Wrote {args.output} (qc_pass={stats['qc_pass']})", file=sys.stderr)


if __name__ == "__main__":
    main()
