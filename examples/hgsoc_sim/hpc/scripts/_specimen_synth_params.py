#!/usr/bin/env python3
"""_specimen_synth_params.py — resolve per-(specimen × run) synth parameters.

Reads the parent cohort config and hpc config, splits the specimen's total
coverage across its flowcell-runs, and emits:

    COVERAGE PURITY SEED

as three whitespace-separated values on stdout. submit_pipeline.sh's
phase_synth() reads them via `read -r COVERAGE PURITY SEED < <(...)`.

The seed is deterministic per (specimen × run) via MD5, so re-runs of the
same (specimen, run) produce identical Badread output.

Author: Samuel Ahuno <ekwame001@gmail.com>
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import yaml


HPC_DIR = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--patient", required=True)      # HGSOC_SIM_01
    ap.add_argument("--specimen", required=True)     # HGSOC_SIM_01_tumor
    ap.add_argument("--run-id", required=True)       # R01 / R02
    args = ap.parse_args()

    hpc_cfg = yaml.safe_load((HPC_DIR / "config.yaml").read_text())
    parent_cfg = yaml.safe_load((HPC_DIR / hpc_cfg["parent_config"]).resolve().read_text())

    # Find the specimen row in the parent config.
    patient_row = next((p for p in parent_cfg["cohort"]
                        if p["patient_id"] == args.patient), None)
    if patient_row is None:
        print(f"patient {args.patient} not in parent cohort config", file=sys.stderr)
        sys.exit(1)
    suffix = args.specimen[len(args.patient) + 1:]   # strip "HGSOC_SIM_01_"
    spec_row = next((s for s in patient_row["specimens"]
                     if s["id_suffix"] == suffix), None)
    if spec_row is None:
        print(f"specimen {args.specimen} not in parent config", file=sys.stderr)
        sys.exit(1)

    runs = hpc_cfg["flowcell_runs_per_specimen"]
    per_run_cov = round(spec_row["coverage"] / runs, 2)
    purity = spec_row["purity"]

    seed_material = f"{args.specimen}|{args.run_id}".encode()
    # Take the first 8 hex chars → 32 bits → fits a typical seed arg.
    seed = int(hashlib.md5(seed_material).hexdigest()[:8], 16) % (2**31)

    print(f"{per_run_cov} {purity} {seed}")


if __name__ == "__main__":
    main()
