#!/usr/bin/env python3
"""04_summarize_mock.py — per-specimen mock summary TSVs with QC autoflag columns.

Walks the simulated BAMs under sandbox/hgsoc_sim/cohort/ and emits a per-
assay summary TSV with the following columns:

    assay_id            (required, first column — casetrack key)
    mock_mean_meth      (analysis column — placeholder methylation metric)
    mock_n_reads        (analysis column — mapped read count)
    qc_pass             (autoflag — False if coverage < MIN_COVERAGE)
    qc_fail_reason      (autoflag — populated only when qc_pass is False)

When HGSOC_SIM_02-normal's BAM has been downsampled by 03_break_*, its
read count falls below the threshold and qc_pass=False fires. The casetrack
append command consumes the autoflag columns inside its transaction
(§6 of proposal 0002).

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SANDBOX = REPO_ROOT / "sandbox" / "hgsoc_sim"

# Coverage threshold below which we declare the assay unusable. Tuned so that
# HGSOC_SIM_02-normal (truncated to ~8% of 25x → ~2x) fails, and everyone
# else passes.
MIN_READS = 5000


def _samtools_count(bam: Path) -> int:
    """Return the number of mapped reads in ``bam``. Shells out to samtools
    via the same runner fallback chain as the other scripts."""
    container_dir = Path.home() / "apps" / "containers"
    override = __import__("os").environ.get("CONTAINER_DIR")
    if override:
        container_dir = Path(override)
    sif = container_dir / "samtools_1.21.sif"

    def _run(cmd: list[str]) -> str:
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout

    if _has_native_samtools():
        return int(_run(["samtools", "view", "-c", "-F", "4", str(bam)]).strip())
    if sif.exists():
        return int(
            _run([
                "apptainer", "exec",
                "--bind", str(bam.parent),
                str(sif),
                "samtools", "view", "-c", "-F", "4", str(bam),
            ]).strip()
        )
    raise RuntimeError(
        "need samtools on PATH OR the samtools SIF in $CONTAINER_DIR "
        "(see examples/hgsoc_sim/containers/README.md)."
    )


def _has_native_samtools() -> bool:
    try:
        subprocess.run(
            ["samtools", "--version"], check=True, capture_output=True
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _mock_meth(bam: Path) -> float:
    """Deterministic pseudo-methylation based on the BAM path — we're not
    actually calling modkit, this just gives each assay a stable float so
    the demo dashboard looks populated."""
    digest = hashlib.md5(str(bam).encode()).hexdigest()
    return round((int(digest[:8], 16) % 10000) / 10000.0, 3)


def summarize(
    sandbox: Path, out_dir: Path, assay_type: str = "ONT"
) -> list[Path]:
    """Emit one TSV per (patient, specimen) under ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cohort_dir = sandbox / "cohort"
    emitted: list[Path] = []

    for patient_dir in sorted(cohort_dir.iterdir()):
        if not patient_dir.is_dir():
            continue
        patient = patient_dir.name
        for spec_dir in sorted(patient_dir.iterdir()):
            if not spec_dir.is_dir():
                continue
            specimen_suffix = spec_dir.name
            bam = spec_dir / "laser" / "sim.srt.bam"
            if not bam.exists():
                print(f"[04] skipping {patient}/{specimen_suffix}: no BAM",
                      file=sys.stderr)
                continue

            assay_id = f"{patient}-{specimen_suffix}-{assay_type}"
            n_reads = _samtools_count(bam)
            mean_meth = _mock_meth(bam)
            qc_pass = n_reads >= MIN_READS
            qc_fail_reason = (
                "" if qc_pass
                else f"low coverage: only {n_reads} mapped reads "
                     f"(need ≥ {MIN_READS})"
            )

            row = {
                "assay_id": assay_id,
                "mock_mean_meth": mean_meth,
                "mock_n_reads": n_reads,
                "qc_pass": qc_pass,
                "qc_fail_reason": qc_fail_reason,
            }
            tsv = out_dir / f"{assay_id}.summary.tsv"
            pd.DataFrame([row]).to_csv(tsv, sep="\t", index=False)
            emitted.append(tsv)

            status = "PASS" if qc_pass else "FAIL"
            print(
                f"[04] {assay_id:<36} reads={n_reads:>7}  "
                f"mock_meth={mean_meth:<5}  qc_pass={status}"
            )
            if not qc_pass:
                print(f"       reason: {qc_fail_reason}")

    return emitted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sandbox", default=str(DEFAULT_SANDBOX))
    ap.add_argument(
        "--out-dir",
        default=str(DEFAULT_SANDBOX / "summaries"),
        help="Directory to write per-assay summary TSVs",
    )
    ap.add_argument(
        "--assay-type", default="ONT",
        help="String used to build assay_id (default: ONT)",
    )
    args = ap.parse_args()

    emitted = summarize(
        Path(args.sandbox), Path(args.out_dir), assay_type=args.assay_type
    )
    print(f"[04] wrote {len(emitted)} summary TSV(s) to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
