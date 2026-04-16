#!/usr/bin/env python3
"""bootstrap.py — populate a casetrack project from the GIAB chr21 sample sheet.

Reads the TSV and registers, for each row:
  - patient (once per unique patient_id)
  - specimen (one gDNA specimen per patient, derived as `{patient_id}_gDNA`)
  - assay (one ONT flowcell run per sample_id)

Idempotent: re-running against an existing project is a no-op for rows that
already exist (strict FK check short-circuits the INSERT).

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-16
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd


# The cell-line identifiers GIAB uses for the coriell samples, inferred from
# the patient_id. Only used to populate the `cell_line` metadata column —
# the demo doesn't depend on it being exhaustive.
CELL_LINES = {
    "HG001": "NA12878",
    "HG002": "GM24385",
    "HG003": "GM24149",
    "HG004": "GM24143",
    "HG005": "GM24631",
    "HG006": "GM24694",
    "HG007": "GM24695",
}

TRIO_ROLES = {
    "HG002": "proband", "HG003": "father", "HG004": "mother",
    "HG005": "proband", "HG006": "father", "HG007": "mother",
}


def _casetrack(*args, check=True) -> subprocess.CompletedProcess:
    """Thin wrapper over the casetrack CLI."""
    cmd = ["casetrack", *args]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _register_if_missing(project_dir: Path, level: str, row_id: str,
                          parent: str | None, meta: dict) -> bool:
    """Call `casetrack register`; treat PK-conflict (exit 1, IntegrityError)
    as a no-op so this script is idempotent. Returns True if we actually
    inserted, False if the row was already there."""
    args = [
        "register",
        "--project-dir", str(project_dir),
        "--level", level,
        "--id", row_id,
    ]
    if parent:
        args += ["--parent", parent]
    if meta:
        args += ["--meta", ",".join(f"{k}={v}" for k, v in meta.items() if v is not None)]

    res = _casetrack(*args, check=False)
    if res.returncode == 0:
        return True
    stderr = res.stderr
    if "UNIQUE constraint failed" in stderr or "already" in stderr.lower():
        return False
    # Any other error is real — surface it.
    sys.stderr.write(res.stdout)
    sys.stderr.write(stderr)
    sys.exit(res.returncode)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample-sheet", required=True,
                    help="TSV with patient_id, sample_id, condition, assay_type, bam_path")
    ap.add_argument("--project-dir", required=True,
                    help="Target casetrack project directory (created if absent)")
    ap.add_argument("--template", default="giab_ont",
                    help="casetrack init --from-template value (default: giab_ont)")
    ap.add_argument("--force", action="store_true",
                    help="Pass --force to casetrack init (overwrites an existing DB)")
    args = ap.parse_args()

    sheet = pd.read_csv(args.sample_sheet, sep="\t")
    required = {"patient_id", "sample_id", "assay_type", "bam_path"}
    missing = required - set(sheet.columns)
    if missing:
        print(f"Error: sample sheet missing required columns: {sorted(missing)}",
              file=sys.stderr)
        sys.exit(1)

    project_dir = Path(args.project_dir)
    if not (project_dir / "casetrack.db").exists() or args.force:
        force_flag = ["--force"] if args.force else []
        _casetrack("init",
                   "--project-dir", str(project_dir),
                   "--from-template", args.template,
                   *force_flag)
        print(f"Initialized project at {project_dir}")
    else:
        print(f"Using existing project at {project_dir}")

    n_patients = n_specimens = n_assays = 0
    for patient_id, pgroup in sheet.groupby("patient_id"):
        if _register_if_missing(
            project_dir, "patient", patient_id, parent=None,
            meta={
                "sex": "M",  # GIAB samples in this sheet are all male-origin lineages
                "reference_source": "GIAB",
                "cohort": "giab_chr21_demo",
                "trio_role": TRIO_ROLES.get(patient_id, "unrelated"),
            },
        ):
            n_patients += 1

        specimen_id = f"{patient_id}_gDNA"
        if _register_if_missing(
            project_dir, "specimen", specimen_id, parent=patient_id,
            meta={
                "specimen_type": "whole_genome_dna",
                "cell_line": CELL_LINES.get(patient_id, ""),
                "source": "Coriell/NIST",
            },
        ):
            n_specimens += 1

        for _, row in pgroup.iterrows():
            if _register_if_missing(
                project_dir, "assay", row["sample_id"], parent=specimen_id,
                meta={
                    "assay_type": row["assay_type"],
                    "flowcell_id": row["sample_id"],
                    "chemistry": "R10.4.1",
                    "basecaller_model": "dorado_sup",
                    "bam_path": row["bam_path"],
                    "condition": row.get("condition", ""),
                },
            ):
                n_assays += 1

    print(f"Registered {n_patients} patient(s), {n_specimens} specimen(s), "
          f"{n_assays} assay(s).")


if __name__ == "__main__":
    main()
