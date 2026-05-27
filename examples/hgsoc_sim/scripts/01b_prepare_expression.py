#!/usr/bin/env python3
"""01b_prepare_expression.py — emit per-specimen NanoSim expression TSVs.

For every (patient, specimen) in config.yaml that has an opt-in `rna:` block,
generates:

    sandbox/hgsoc_sim/cohort/<PATIENT>/<SPECIMEN>/rna/expression.tsv

Format (NanoSim simulator.py transcriptome expects this exact header):

    target_id   est_counts   tpm

Model:
  1. Baseline TPM per transcript is log-normal, seeded on a fixed string so
     every run and every specimen sees the same baseline distribution.
     Transcript ordering = transcripts.tsv ordering -> stable.
  2. Per-specimen `rna.expression.gene_multipliers` in config.yaml scales
     baseline TPMs by the named gene (e.g. `BRCA1: 0.1` drops BRCA1
     transcripts 10x in a tumor with BRCA1 LOF).
  3. Scaled TPMs are normalized to sum to 1e6 (proper TPM), then est_counts
     are assigned proportionally to sum to the specimen's `rna.n_reads`.

The `rna:` sub-block is additive and opt-in. Specimens without an `rna:`
block are silently skipped. The parent cohort schema (scalar `assay_type`,
per-patient germline/somatic variant lists, etc.) is untouched.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import yaml
except ImportError:
    print("Error: pyyaml is required.", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
DEFAULT_SANDBOX = REPO_ROOT / "sandbox" / "hgsoc_sim"

RNA_LEAF = "rna"

# Baseline log-normal params. sigma=2 gives a fat-tailed "most genes low,
# a few very high" distribution that matches real RNA-seq well enough.
BASELINE_LOGNORMAL_MEAN = 0.0
BASELINE_LOGNORMAL_SIGMA = 2.0

BASELINE_SEED_STRING = "hgsoc_sim_baseline_v1"


def _seed_from(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


def build_baseline_tpm(transcripts: pd.DataFrame) -> np.ndarray:
    seed = _seed_from(BASELINE_SEED_STRING)
    rng = np.random.default_rng(seed)
    return rng.lognormal(
        mean=BASELINE_LOGNORMAL_MEAN,
        sigma=BASELINE_LOGNORMAL_SIGMA,
        size=len(transcripts),
    )


def apply_multipliers(
    transcripts: pd.DataFrame, baseline_tpm: np.ndarray, multipliers: dict
) -> tuple[np.ndarray, list[str]]:
    scaled = baseline_tpm.copy()
    warnings: list[str] = []
    genes = set(transcripts["gene_name"])
    for gene, mult in (multipliers or {}).items():
        if gene not in genes:
            warnings.append(
                f"gene {gene!r} not in transcriptome — multiplier ignored"
            )
            continue
        mask = (transcripts["gene_name"] == gene).to_numpy()
        scaled[mask] = scaled[mask] * float(mult)
    return scaled, warnings


def assign_counts(scaled_tpm: np.ndarray, n_reads: int) -> np.ndarray:
    if scaled_tpm.sum() <= 0:
        # All transcripts zeroed out — distribute uniformly so NanoSim doesn't explode.
        return np.full(len(scaled_tpm), n_reads // len(scaled_tpm), dtype=int)

    tpm = scaled_tpm * 1e6 / scaled_tpm.sum()
    raw = tpm * n_reads / 1e6
    counts = np.floor(raw).astype(int)
    delta = n_reads - counts.sum()
    if delta > 0:
        frac = raw - counts
        idx = np.argsort(frac)[::-1][: int(delta)]
        counts[idx] += 1
    elif delta < 0:
        idx = np.argsort(counts)[::-1][: int(-delta)]
        counts[idx] -= 1
    return counts


def _emit_for_specimen(
    out_dir: Path,
    transcripts: pd.DataFrame,
    baseline_tpm: np.ndarray,
    rna_cfg: dict,
) -> tuple[int, list[str]]:
    n_reads = int(rna_cfg.get("n_reads", 10000))
    multipliers = (rna_cfg.get("expression") or {}).get("gene_multipliers") or {}
    scaled, warns = apply_multipliers(transcripts, baseline_tpm, multipliers)
    counts = assign_counts(scaled, n_reads)
    if scaled.sum() > 0:
        tpm = scaled * 1e6 / scaled.sum()
    else:
        tpm = np.zeros_like(scaled)

    df = pd.DataFrame({
        "target_id": transcripts["transcript_id"].to_numpy(),
        "est_counts": counts.astype(int),
        "tpm": np.round(tpm, 3),
    })
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / "expression.tsv", sep="\t", index=False)
    return int(counts.sum()), warns


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--sandbox", default=str(DEFAULT_SANDBOX))
    args = ap.parse_args()

    sandbox = Path(args.sandbox)
    transcripts_tsv = sandbox / "ref" / "transcripts.tsv"
    if not transcripts_tsv.exists():
        print(
            f"Error: transcripts index not found at {transcripts_tsv} — "
            "run 00b_fetch_gencode.sh first.",
            file=sys.stderr,
        )
        sys.exit(1)

    transcripts = pd.read_csv(transcripts_tsv, sep="\t")
    if transcripts.empty:
        print(
            "Error: transcripts.tsv is empty. Check the GENCODE slice output.",
            file=sys.stderr,
        )
        sys.exit(1)

    cfg = yaml.safe_load(Path(args.config).read_text())

    baseline_tpm = build_baseline_tpm(transcripts)
    cohort_dir = sandbox / "cohort"
    summary: list[str] = []
    all_warns: list[str] = []

    for patient in cfg["cohort"]:
        pid = patient["patient_id"]
        for spec in patient["specimens"]:
            rna_cfg = spec.get("rna")
            if rna_cfg is None:
                continue
            suffix = spec["id_suffix"]
            out_dir = cohort_dir / pid / suffix / RNA_LEAF
            total_reads, warns = _emit_for_specimen(
                out_dir, transcripts, baseline_tpm, rna_cfg
            )
            mults = (rna_cfg.get("expression") or {}).get("gene_multipliers") or {}
            mult_desc = ", ".join(
                f"{g}×{m}" for g, m in mults.items()
            ) or "baseline only"
            summary.append(
                f"  {pid}/{suffix}/{RNA_LEAF}: {total_reads} reads, "
                f"{mult_desc} → {out_dir}/expression.tsv"
            )
            all_warns.extend(
                f"  {pid}/{suffix}/{RNA_LEAF}: {w}" for w in warns
            )

    if not summary:
        print("[01b] no specimens with an `rna:` block in config.yaml — nothing to do.")
        return 0

    print(f"[01b] emitted expression TSVs for {len(summary)} specimen(s):")
    for line in summary:
        print(line)
    if all_warns:
        print(f"[01b] warnings:", file=sys.stderr)
        for w in all_warns:
            print(w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
