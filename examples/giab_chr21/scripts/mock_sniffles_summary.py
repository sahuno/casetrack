#!/usr/bin/env python3
"""mock_sniffles_summary.py — emit a fake Sniffles SV summary TSV.

Mirrors the shape of a real sniffles2 VCF summary: total SV count, broken
down by type. Output columns: assay_id, n_svs_total, n_ins, n_del, n_inv,
n_bnd, sv_size_median.

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-16
"""
from __future__ import annotations

import argparse
import hashlib


def _mock(assay_id: str) -> dict:
    seed = int(hashlib.md5((assay_id + "sniffles").encode()).hexdigest()[:8], 16)
    # Deterministic splits of a plausible chr21 SV count (500–1500).
    state = seed & 0xFFFFFFFF

    def nxt(bound: int) -> int:
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state % bound

    total = 500 + nxt(1000)
    n_ins = int(total * (0.35 + 0.05 * (nxt(100) / 100)))
    n_del = int(total * (0.40 + 0.05 * (nxt(100) / 100)))
    n_inv = int(total * 0.05)
    n_bnd = total - n_ins - n_del - n_inv
    sv_size_median = 100 + nxt(300)  # bp
    return {
        "n_svs_total": total,
        "n_ins": n_ins,
        "n_del": n_del,
        "n_inv": n_inv,
        "n_bnd": n_bnd,
        "sv_size_median": sv_size_median,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--assay-ids", required=True,
                    help="Comma-separated list of assay_ids")
    ap.add_argument("--output", required=True, help="TSV output path")
    args = ap.parse_args()

    assay_ids = [a.strip() for a in args.assay_ids.split(",") if a.strip()]
    cols = ["assay_id", "n_svs_total", "n_ins", "n_del", "n_inv",
            "n_bnd", "sv_size_median"]
    with open(args.output, "w") as f:
        f.write("\t".join(cols) + "\n")
        for aid in assay_ids:
            stats = _mock(aid)
            f.write("\t".join([aid] + [str(stats[c]) for c in cols[1:]]) + "\n")


if __name__ == "__main__":
    main()
