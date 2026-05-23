#!/usr/bin/env python3
"""bootstrap.py — populate a casetrack project from Project_demo's
pre-merge BAM sample sheet.

Sheet schema:
    patient   — biological subject (p_demo_N)
    sample    — opaque per-flowcell sample ID (s_demo_...), one row per BAM
    condition — tumor / normal (used to synthesize specimen_id)
    path      — absolute pre-merge BAM path
    genome    — e.g. hg38

Maps to:
    patient_id  = sheet.patient
    specimen_id = f"{patient_id}_{condition}"
    assay_id    = sheet.sample
    assay.bam_path      = sheet.path
    assay.assay_type    = "ONT_WGS"
    specimen.tissue_site = condition      (so cohort --pair-by condition works)

Idempotent — re-running against an existing project is a no-op for rows
that already exist (strict FK / UNIQUE short-circuits the INSERT).

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-17
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd


def _casetrack(*args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["casetrack", *args], check=check,
                          capture_output=True, text=True)


def _register_if_missing(project_dir: Path, level: str, row_id: str,
                          parent: str | None, meta: dict) -> bool:
    args = ["register", "--project-dir", str(project_dir),
            "--level", level, "--id", row_id]
    if parent:
        args += ["--parent", parent]
    meta_str = ",".join(f"{k}={v}" for k, v in meta.items() if v is not None and v != "")
    if meta_str:
        args += ["--meta", meta_str]

    res = _casetrack(*args, check=False)
    if res.returncode == 0:
        return True
    if "UNIQUE constraint failed" in res.stderr or "already" in res.stderr.lower():
        return False
    sys.stderr.write(res.stdout); sys.stderr.write(res.stderr)
    sys.exit(res.returncode)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample-sheet", required=True,
                    help="TSV with patient / sample / condition / path / genome")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--template", default="giab_ont",
                    help="casetrack init --from-template (default: giab_ont)")
    ap.add_argument("--force", action="store_true",
                    help="Pass --force to casetrack init (overwrites existing DB)")
    args = ap.parse_args()

    sheet = pd.read_csv(args.sample_sheet, sep="\t")
    required = {"patient", "sample", "condition", "path"}
    missing = required - set(sheet.columns)
    if missing:
        print(f"Error: sample sheet missing required columns: {sorted(missing)}",
              file=sys.stderr)
        sys.exit(1)

    project_dir = Path(args.project_dir)
    if not (project_dir / "casetrack.db").exists() or args.force:
        force_flag = ["--force"] if args.force else []
        _casetrack("init", "--project-dir", str(project_dir),
                   "--from-template", args.template, *force_flag)
        print(f"Initialized project at {project_dir}")
    else:
        print(f"Using existing project at {project_dir}")

    n_p = n_s = n_a = 0
    for (patient_id, condition), group in sheet.groupby(["patient", "condition"]):
        if _register_if_missing(
            project_dir, "patient", patient_id, parent=None,
            meta={"reference_source": "MSKCC", "cohort": "project_demo"},
        ):
            n_p += 1

        specimen_id = f"{patient_id}_{condition}"
        # tissue_site mirrors condition so cohort --pair-by tissue_site (or
        # --pair-by condition) surfaces matched pairs when normals land.
        if _register_if_missing(
            project_dir, "specimen", specimen_id, parent=patient_id,
            meta={
                "specimen_type": "whole_genome_dna",
                "source": "MSKCC",
            },
        ):
            n_s += 1

        for _, row in group.iterrows():
            if _register_if_missing(
                project_dir, "assay", row["sample"], parent=specimen_id,
                meta={
                    "assay_type": "ONT_WGS",
                    "flowcell_id": row["sample"],
                    "chemistry": "R10.4.1",
                    "basecaller_model": "dorado_sup",
                    "bam_path": row["path"],
                    "condition": condition,
                },
            ):
                n_a += 1

    print(f"Registered {n_p} patient(s), {n_s} specimen(s), {n_a} assay(s).")


if __name__ == "__main__":
    main()
