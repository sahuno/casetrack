#!/usr/bin/env python3
"""01_prepare_visor_beds.py — expand config.yaml into per-patient VISOR BEDs.

Reads the cohort config and emits, for every (patient, specimen):

    sandbox/hgsoc_sim/cohort/<PATIENT>/<SPECIMEN>/
        haplotype1.hack.bed     # 6-col VISOR HACk BED for haplotype 1
        haplotype2.hack.bed     # 6-col VISOR HACk BED for haplotype 2
        regions.laser.bed       # 5-col VISOR LASeR BED (chr, start, end, cov, purity)

VISOR HACk places variants from the 6-col BED into a reference contig,
producing two haplotype FASTAs per specimen. We split variants between the
two haplotype BEDs deterministically: SNPs alternate (het by default),
structural variants land on haplotype 1 (simulating heterozygous SVs).

Germline variants go on both the normal and tumor specimens of a patient.
Somatic variants go only on tumor. Purity comes from the config.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print(
        "Error: pyyaml is required. Install with `pip install pyyaml --user`.",
        file=sys.stderr,
    )
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
DEFAULT_SANDBOX = REPO_ROOT / "sandbox" / "hgsoc_sim"


# ── HACk BED formatting ────────────────────────────────────────────────────────


def _hack_row(contig: str, pos: int, variant: dict) -> tuple[str, list[str]]:
    """Return (haplotype_key, 6-col HACk BED row) for a config variant.

    haplotype_key is "h1" or "h2" — lets the caller split variants across
    the two haplotype BEDs to simulate heterozygosity.
    """
    vtype = variant["type"]
    note = variant.get("note", "")
    if vtype == "SNP":
        alt = variant["alt"]
        start = pos
        end = pos + 1
        # VISOR HACk uses "SNP" with col5 = the alt base.
        return "h1", [contig, str(start), str(end), "SNP", alt, "0"]
    if vtype == "deletion":
        length = int(variant["length"])
        start = pos
        end = pos + length
        return "h1", [contig, str(start), str(end), "deletion", "None", "0"]
    if vtype == "insertion":
        alt_seq = variant["alt"]
        start = pos
        end = pos + 1
        return "h1", [contig, str(start), str(end), "insertion", alt_seq, "0"]
    if vtype == "inversion":
        length = int(variant["length"])
        return "h1", [contig, str(pos), str(pos + length), "inversion", "None", "0"]
    if vtype == "duplication":
        length = int(variant["length"])
        # VISOR uses "tandem duplication" for col4 on some versions; the
        # biocontainer 1.1.2.1 accepts "tandem duplication".
        return "h1", [contig, str(pos), str(pos + length), "tandem duplication", "2", "0"]
    raise ValueError(f"unknown variant type: {vtype!r}")


def _split_variants(contig: str, variants: list[dict]) -> dict[str, list[list[str]]]:
    """Split variants into h1/h2 BEDs.

    SNPs alternate between haplotypes so every other germline SNP is het on
    h1 and homozygous alt on h2 — good enough for demo purposes.
    SVs default to h1 only (heterozygous SVs are the common case).
    """
    per_hap: dict[str, list[list[str]]] = {"h1": [], "h2": []}
    snp_counter = 0
    for v in variants:
        hap, row = _hack_row(contig, int(v["pos"]), v)
        if v["type"] == "SNP":
            # Alternate SNPs between haplotypes for het realism.
            target = "h1" if snp_counter % 2 == 0 else "h2"
            snp_counter += 1
            per_hap[target].append(row)
        else:
            per_hap[hap].append(row)
    return per_hap


def _write_hack_beds(out_dir: Path, per_hap: dict[str, list[list[str]]]) -> None:
    """Emit haplotype1.hack.bed and haplotype2.hack.bed (always both — even if empty)."""
    for idx, hap_key in enumerate(("h1", "h2"), start=1):
        dest = out_dir / f"haplotype{idx}.hack.bed"
        with open(dest, "w") as f:
            for row in per_hap[hap_key]:
                f.write("\t".join(row) + "\n")


def _write_laser_bed(
    out_dir: Path, contig: str, region_len: int, coverage: int, purity: float
) -> None:
    """5-col LASeR BED: chrom, start, end, coverage, purity."""
    dest = out_dir / "regions.laser.bed"
    with open(dest, "w") as f:
        f.write(
            "\t".join(
                [contig, "0", str(region_len), str(coverage), str(purity)]
            )
            + "\n"
        )


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--sandbox", default=str(DEFAULT_SANDBOX))
    args = ap.parse_args()

    cfg_path = Path(args.config)
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    contig = cfg["reference"]["slice_contig"]
    region_len = int(cfg["reference"]["region_end"]) - int(
        cfg["reference"]["region_start"]
    )

    sandbox = Path(args.sandbox)
    cohort_dir = sandbox / "cohort"

    summary: list[str] = []
    for patient in cfg["cohort"]:
        pid = patient["patient_id"]
        germline = patient.get("germline", [])
        somatic = patient.get("somatic", [])
        for spec in patient["specimens"]:
            suffix = spec["id_suffix"]
            coverage = int(spec["coverage"])
            purity = float(spec["purity"])

            variants = list(germline)
            # Tumor specimens additionally carry the somatic set.
            if spec["tissue_site"] == "tumor":
                variants += list(somatic)

            out_dir = cohort_dir / pid / suffix
            out_dir.mkdir(parents=True, exist_ok=True)
            per_hap = _split_variants(contig, variants)
            _write_hack_beds(out_dir, per_hap)
            _write_laser_bed(out_dir, contig, region_len, coverage, purity)

            n_vars = sum(len(v) for v in per_hap.values())
            summary.append(
                f"  {pid}/{suffix}: {n_vars} variants → "
                f"cov={coverage}x, purity={purity}% → {out_dir}"
            )

    print(f"[01] emitted VISOR BEDs under {cohort_dir}")
    for line in summary:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
