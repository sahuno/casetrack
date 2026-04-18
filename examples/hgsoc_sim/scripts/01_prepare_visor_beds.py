#!/usr/bin/env python3
"""01_prepare_visor_beds.py — expand config.yaml into per-assay VISOR BEDs.

Emits, for every (patient, specimen, DNA assay):

    sandbox/hgsoc_sim/cohort/<PATIENT>/<SPECIMEN>/<ASSAY_TYPE>/
        haplotype1.hack.bed     # 6-col VISOR HACk BED for haplotype 1
        haplotype2.hack.bed     # 6-col VISOR HACk BED for haplotype 2
        regions.laser.bed       # 5-col VISOR LASeR BED (one row per slice)

RNA assays (e.g. ONT-RNA) are silently skipped here — they have their own
prep path in phase f (transcript extraction + expression vectors).

VISOR HACk places variants from the 6-col BED into a reference contig,
producing two haplotype FASTAs per assay. We split variants between the
two haplotype BEDs deterministically: SNPs alternate (het by default),
structural variants land on haplotype 1 (simulating heterozygous SVs).

Germline variants go on both the normal and tumor specimens of a patient.
Somatic variants go only on tumor. Coverage and purity come from the
specimen's DNA assay entry.

Multi-assay layout (v0.4.2): every specimen has a `assays: [...]` list.
DNA lives at {PATIENT}/{SPECIMEN}/ONT-DNA/; RNA at {PATIENT}/{SPECIMEN}/ONT-RNA/.

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

# Which assay types this script handles. RNA flows through a different
# prep script (phase f) that uses transcript FASTAs, not genomic slices.
DNA_ASSAY_TYPES: tuple[str, ...] = ("ONT-DNA",)


# ── HACk BED formatting ────────────────────────────────────────────────────────


def _hack_row(contig: str, pos: int, variant: dict) -> tuple[str, list[str]]:
    """Return (haplotype_hint, 6-col HACk BED row) for a config variant."""
    vtype = variant["type"]
    if vtype == "SNP":
        alt = variant["alt"]
        return "h1", [contig, str(pos), str(pos + 1), "SNP", alt, "0"]
    if vtype == "deletion":
        length = int(variant["length"])
        return "h1", [contig, str(pos), str(pos + length), "deletion", "None", "0"]
    if vtype == "insertion":
        alt_seq = variant["alt"]
        return "h1", [contig, str(pos), str(pos + 1), "insertion", alt_seq, "0"]
    if vtype == "inversion":
        length = int(variant["length"])
        return "h1", [contig, str(pos), str(pos + length), "inversion", "None", "0"]
    if vtype == "duplication":
        length = int(variant["length"])
        return "h1", [
            contig, str(pos), str(pos + length),
            "tandem duplication", "2", "0",
        ]
    raise ValueError(f"unknown variant type: {vtype!r}")


def _split_variants(variants: list[dict]) -> dict[str, list[list[str]]]:
    """Split variants into h1/h2 BEDs with heterozygous-SNP alternation."""
    per_hap: dict[str, list[list[str]]] = {"h1": [], "h2": []}
    snp_counter = 0
    for v in variants:
        contig = v.get("chrom")
        if contig is None:
            raise ValueError(
                f"variant {v} is missing the required `chrom` field"
            )
        _, row = _hack_row(contig, int(v["pos"]), v)
        if v["type"] == "SNP":
            target = "h1" if snp_counter % 2 == 0 else "h2"
            snp_counter += 1
            per_hap[target].append(row)
        else:
            per_hap["h1"].append(row)
    # VISOR HACk wants rows sorted by contig then start.
    for hap in per_hap.values():
        hap.sort(key=lambda r: (r[0], int(r[1])))
    return per_hap


def _write_hack_beds(
    out_dir: Path, per_hap: dict[str, list[list[str]]]
) -> None:
    """Emit haplotype1.hack.bed and haplotype2.hack.bed — always both."""
    for idx, hap_key in enumerate(("h1", "h2"), start=1):
        dest = out_dir / f"haplotype{idx}.hack.bed"
        with open(dest, "w") as f:
            for row in per_hap[hap_key]:
                f.write("\t".join(row) + "\n")


def _write_laser_bed(
    out_dir: Path,
    slices: list[dict],
    coverage: int,
    purity: float,
) -> None:
    """5-col LASeR BED: chrom, start, end, coverage, purity — one row per slice."""
    dest = out_dir / "regions.laser.bed"
    with open(dest, "w") as f:
        for sl in slices:
            slice_len = int(sl["end"]) - int(sl["start"])
            f.write(
                "\t".join([
                    sl["name"], "0", str(slice_len),
                    str(coverage), str(purity),
                ]) + "\n"
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

    ref_slices = cfg["reference"]["slices"]
    valid_contigs = {s["name"] for s in ref_slices}

    sandbox = Path(args.sandbox)
    cohort_dir = sandbox / "cohort"

    summary: list[str] = []
    skipped: list[str] = []

    for patient in cfg["cohort"]:
        pid = patient["patient_id"]
        germline = patient.get("germline", [])
        somatic = patient.get("somatic", [])

        # Early-fail on any variant referring to a slice we didn't declare.
        for v in [*germline, *somatic]:
            if v.get("chrom") not in valid_contigs:
                raise SystemExit(
                    f"Error: variant {v} targets unknown contig "
                    f"{v.get('chrom')!r} (valid: {sorted(valid_contigs)})"
                )

        for spec in patient["specimens"]:
            suffix = spec["id_suffix"]
            assays = spec.get("assays") or []
            if not assays:
                raise SystemExit(
                    f"Error: {pid}/{suffix} has no assays declared"
                )

            variants = list(germline)
            if spec["tissue_site"] == "tumor":
                variants += list(somatic)

            for assay in assays:
                atype = assay["type"]
                if atype not in DNA_ASSAY_TYPES:
                    # RNA assays are prepped elsewhere (phase f).
                    skipped.append(f"{pid}/{suffix}/{atype}")
                    continue

                coverage = int(assay["coverage"])
                purity = float(assay["purity"])

                out_dir = cohort_dir / pid / suffix / atype
                out_dir.mkdir(parents=True, exist_ok=True)
                per_hap = _split_variants(variants)
                _write_hack_beds(out_dir, per_hap)
                _write_laser_bed(out_dir, ref_slices, coverage, purity)

                n_vars = sum(len(v) for v in per_hap.values())
                summary.append(
                    f"  {pid}/{suffix}/{atype}: {n_vars} variants across "
                    f"{len(ref_slices)} slice(s) → cov={coverage}x, "
                    f"purity={purity}% → {out_dir}"
                )

    print(f"[01] emitted VISOR BEDs under {cohort_dir}")
    for line in summary:
        print(line)
    if skipped:
        print(f"[01] (skipped {len(skipped)} non-DNA assay(s) — handled in phase f):")
        for s in skipped:
            print(f"       {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
