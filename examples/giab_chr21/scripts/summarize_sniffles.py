#!/usr/bin/env python3
"""summarize_sniffles.py — distill a Sniffles2 VCF (plain or .gz) into a
casetrack append-ready per-assay TSV.

Output columns: assay_id, n_svs_total, n_ins, n_del, n_inv, n_bnd,
sv_size_median.

The mock summarizer produces the same schema, so this real parser is a
drop-in replacement once a sniffles VCF is available.

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-16
"""
from __future__ import annotations

import argparse
import gzip
import re
import statistics
import sys
from pathlib import Path


_SVTYPE_RE = re.compile(r"(?:^|;)SVTYPE=([^;]+)")
_SVLEN_RE = re.compile(r"(?:^|;)SVLEN=(-?\d+)")
_KNOWN = {"INS", "DEL", "INV", "DUP", "BND"}


def _open(path: Path):
    return gzip.open(path, "rt") if str(path).endswith(".gz") else open(path)


def summarize(path: Path) -> dict:
    counts = {"INS": 0, "DEL": 0, "INV": 0, "BND": 0, "DUP": 0}
    sizes: list[int] = []

    with _open(path) as f:
        for line in f:
            if not line or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 8:
                continue
            info = cols[7]

            m = _SVTYPE_RE.search(info)
            if not m:
                continue
            svtype = m.group(1)
            # Classify BND / TRA / other types as BND for the summary.
            if svtype not in _KNOWN:
                svtype = "BND"
            counts[svtype] += 1

            m = _SVLEN_RE.search(info)
            if m:
                try:
                    sizes.append(abs(int(m.group(1))))
                except ValueError:
                    pass

    total = sum(counts.values())
    return {
        "n_svs_total": total,
        "n_ins": counts["INS"],
        "n_del": counts["DEL"],
        "n_inv": counts["INV"],
        # n_bnd merges BND + DUP so the 4 subtype columns sum to total (matches
        # the mock summarizer's schema expectation used in tests).
        "n_bnd": counts["BND"] + counts["DUP"],
        "sv_size_median": int(statistics.median(sizes)) if sizes else 0,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--assay-id", required=True, help="assay_id key column value")
    ap.add_argument("--input", required=True, help="Sniffles VCF (plain or .vcf.gz)")
    ap.add_argument("--output", required=True, help="TSV output path")
    args = ap.parse_args()

    stats = summarize(Path(args.input))

    cols = ["assay_id", "n_svs_total", "n_ins", "n_del", "n_inv",
            "n_bnd", "sv_size_median"]
    with open(args.output, "w") as f:
        f.write("\t".join(cols) + "\n")
        f.write("\t".join([args.assay_id] + [str(stats[c]) for c in cols[1:]]) + "\n")
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
