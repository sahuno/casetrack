#!/usr/bin/env python3
"""
inject_methyl_tags.py — stamp per-read MM/ML methylation tags on CpG sites.

Rewrites an aligned BAM, adding MM:Z:C+m?,...; and ML:B:C,... tags whose
per-CpG methylation rate reflects the specimen type (tumor vs normal).
Gives the modkit-pileup demo a non-trivial signal without swapping in a
real methylation-aware simulator.

Not a substitute for real data — the per-CpG rate is drawn from a deterministic
seeded beta distribution, not a learned model. But relative tumor-vs-normal
contrast is tunable and reproducible, so the DMR step produces meaningful
output end-to-end.

Author: Samuel Ahuno
"""
from __future__ import annotations

import argparse
import array
import hashlib
import logging
import os
import random
import sys
from pathlib import Path

import numpy as np
import pysam


LOGGER_NAME = "inject_methyl_tags"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.strip(), formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--in-bam", required=True, help="Input aligned sorted BAM (no MM/ML tags expected).")
    p.add_argument("--out-bam", required=True, help="Output BAM with MM/ML tags stamped.")
    p.add_argument("--reference", required=True, help="Reference FASTA (must be indexed).")
    p.add_argument("--specimen-type", required=True, choices=["tumor", "normal"],
                   help="Drives the per-CpG target-rate distribution.")
    p.add_argument("--tumor-target-rate", type=float, default=0.50,
                   help="Mean methylation rate for tumor specimens (default 0.50).")
    p.add_argument("--normal-target-rate", type=float, default=0.80,
                   help="Mean methylation rate for normal specimens (default 0.80).")
    p.add_argument("--beta-concentration", type=float, default=20.0,
                   help="Beta concentration (higher = less per-CpG variance).")
    p.add_argument("--meth-prob-byte", type=int, default=230,
                   help="ML byte emitted when a read-site is called methylated (~0.90).")
    p.add_argument("--canon-prob-byte", type=int, default=25,
                   help="ML byte emitted when a read-site is called canonical (~0.10).")
    p.add_argument("--seed", type=int, default=42, help="RNG seed (deterministic across runs).")
    return p.parse_args()


def build_cpg_table(reference: Path, target_rate: float, concentration: float, seed: int):
    """Return dict[contig] -> dict[cpg_c_pos] -> true_rate. Deterministic via seed."""
    rng = np.random.default_rng(seed)
    alpha = target_rate * concentration
    beta = (1.0 - target_rate) * concentration
    table: dict[str, dict[int, float]] = {}
    logger = logging.getLogger(LOGGER_NAME)
    with pysam.FastaFile(str(reference)) as fa:
        for contig in fa.references:
            seq = fa.fetch(contig).upper()
            sites: dict[int, float] = {}
            for i in range(len(seq) - 1):
                if seq[i] == "C" and seq[i + 1] == "G":
                    sites[i] = float(rng.beta(alpha, beta))
            table[contig] = sites
            logger.info(f"CpG table: {contig} length={len(seq):,}  n_cpg={len(sites):,}")
    return table


def find_methylation_sites(read: pysam.AlignedSegment, ref_seq: str, cpg_rates_for_contig: dict[int, float]):
    """
    Walk aligned_pairs and return [(pos_in_original_read, cpg_ref_pos, target_rate), ...].

    FWD-mapped read: the CpG-C on the forward reference (ref[rpos]='C', ref[rpos+1]='G')
    is also a C at qpos in the original read (original == query_sequence for fwd reads).

    REV-mapped read: the C in the *original* read (5'→3' on the reverse strand) sits at
    the forward-reference G position (ref[rpos]='G', ref[rpos-1]='C'). The forward-view
    qpos_fv from pysam is flipped to qpos_orig = query_length - 1 - qpos_fv to index
    into get_forward_sequence().
    """
    is_rev = read.is_reverse
    read_len = read.query_length or 0
    out: list[tuple[int, int, float]] = []
    for qpos_fv, rpos in read.get_aligned_pairs(matches_only=True):
        if qpos_fv is None or rpos is None:
            continue
        if not is_rev:
            rate = cpg_rates_for_contig.get(rpos)
            if rate is not None:
                out.append((qpos_fv, rpos, rate))
        else:
            cpg_c_pos = rpos - 1
            if cpg_c_pos < 0:
                continue
            rate = cpg_rates_for_contig.get(cpg_c_pos)
            if rate is None:
                continue
            if ref_seq[cpg_c_pos] != "C" or ref_seq[rpos] != "G":
                continue
            qpos_orig = read_len - 1 - qpos_fv
            out.append((qpos_orig, cpg_c_pos, rate))
    return out


def build_mm_ml_tags(original_seq: str, meth_sites, per_read_rng: random.Random,
                     meth_byte: int, canon_byte: int):
    """Return (mm_string, ml_bytes) or (None, None) when no usable sites."""
    if not original_seq:
        return None, None
    c_positions = [i for i, base in enumerate(original_seq) if base == "C"]
    if not c_positions:
        return None, None
    pos_to_rank = {p: r for r, p in enumerate(c_positions)}

    filtered: list[tuple[int, float]] = []
    seen: set[int] = set()
    for pos, _ref_pos, rate in meth_sites:
        if pos in seen:
            continue
        if not (0 <= pos < len(original_seq)) or original_seq[pos] != "C":
            continue
        seen.add(pos)
        filtered.append((pos, rate))
    if not filtered:
        return None, None
    filtered.sort(key=lambda x: x[0])

    skips: list[int] = []
    probs: list[int] = []
    prev_rank = -1
    for pos, rate in filtered:
        rank = pos_to_rank[pos]
        skips.append(rank - prev_rank - 1)
        probs.append(meth_byte if per_read_rng.random() < rate else canon_byte)
        prev_rank = rank
    mm_tag = "C+m?," + ",".join(str(s) for s in skips) + ";"
    ml_tag = array.array("B", probs)
    return mm_tag, ml_tag


def _seed_for_read(master_seed: int, read_name: str) -> int:
    h = hashlib.sha256(f"{master_seed}|{read_name}".encode()).digest()
    return int.from_bytes(h[:8], "big")


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stderr,
    )
    logger = logging.getLogger(LOGGER_NAME)

    in_bam = Path(args.in_bam)
    out_bam = Path(args.out_bam)
    ref_fa = Path(args.reference)
    target_rate = args.tumor_target_rate if args.specimen_type == "tumor" else args.normal_target_rate

    logger.info(f"in_bam={in_bam}")
    logger.info(f"out_bam={out_bam}")
    logger.info(f"reference={ref_fa}")
    logger.info(f"specimen_type={args.specimen_type}  target_rate={target_rate}")
    logger.info(f"seed={args.seed}  beta_concentration={args.beta_concentration}")
    logger.info(f"meth_byte={args.meth_prob_byte}  canon_byte={args.canon_prob_byte}")

    cpg_table = build_cpg_table(ref_fa, target_rate, args.beta_concentration, args.seed)
    n_cpg_total = sum(len(v) for v in cpg_table.values())
    logger.info(f"CpG table: {n_cpg_total:,} sites across {len(cpg_table)} contigs")

    with pysam.FastaFile(str(ref_fa)) as fa:
        ref_seqs = {name: fa.fetch(name).upper() for name in fa.references}

    out_bam.parent.mkdir(parents=True, exist_ok=True)
    out_tmp = out_bam.with_name(out_bam.name + ".tmp")

    n_reads = 0
    n_skipped_unmapped = 0
    n_skipped_secondary = 0
    n_reads_tagged = 0
    n_sites_emitted = 0
    with pysam.AlignmentFile(str(in_bam), "rb") as inp:
        with pysam.AlignmentFile(str(out_tmp), "wb", template=inp) as outp:
            for read in inp:
                n_reads += 1
                if read.is_unmapped:
                    n_skipped_unmapped += 1
                    outp.write(read)
                    continue
                if read.is_secondary or read.is_supplementary:
                    n_skipped_secondary += 1
                    outp.write(read)
                    continue
                contig = read.reference_name
                rates_for_contig = cpg_table.get(contig)
                if not rates_for_contig:
                    outp.write(read)
                    continue
                original_seq = read.get_forward_sequence()
                if not original_seq:
                    outp.write(read)
                    continue
                sites = find_methylation_sites(read, ref_seqs[contig], rates_for_contig)
                per_rng = random.Random(_seed_for_read(args.seed, read.query_name))
                mm_tag, ml_tag = build_mm_ml_tags(
                    original_seq, sites, per_rng,
                    args.meth_prob_byte, args.canon_prob_byte,
                )
                if mm_tag is not None:
                    read.set_tag("MM", mm_tag, value_type="Z")
                    read.set_tag("ML", ml_tag)
                    n_reads_tagged += 1
                    n_sites_emitted += len(ml_tag)
                outp.write(read)

    logger.info(f"Indexing {out_tmp}")
    pysam.index(str(out_tmp))

    os.replace(out_tmp, out_bam)
    os.replace(str(out_tmp) + ".bai", str(out_bam) + ".bai")

    logger.info(f"reads read              : {n_reads:,}")
    logger.info(f"reads skipped unmapped  : {n_skipped_unmapped:,}")
    logger.info(f"reads skipped sec/supp  : {n_skipped_secondary:,}")
    logger.info(f"reads tagged            : {n_reads_tagged:,}")
    logger.info(f"CpG calls emitted       : {n_sites_emitted:,}")
    if n_reads_tagged:
        logger.info(f"mean sites per tagged read: {n_sites_emitted / n_reads_tagged:.1f}")
    logger.info(f"wrote {out_bam}  ({out_bam.stat().st_size / 1024 / 1024:.1f} MB)")
    logger.info(f"=== DONE: inject_methyl_tags.py ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
