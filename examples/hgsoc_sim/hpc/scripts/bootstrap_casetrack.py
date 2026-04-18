#!/usr/bin/env python3
"""bootstrap_casetrack.py — register the HGSOC HPC cohort in casetrack.

Reads the parent `examples/hgsoc_sim/config.yaml` for the 3-patient / 5-
specimen definition, plus the HPC `config.yaml` for the per-specimen
flowcell-run count and multi-assay mix. Then registers:

    3 patients       — HGSOC_SIM_{01,02,03}
    5 specimens      — SIM_01/02 each tumor+normal; SIM_03 tumor only
    N assays         — one per (specimen × assay_type × flowcell_run)
                       (ONT_WGS × 2 runs + scRNA × 1 run per specimen)

Idempotent — re-running against an existing project is a no-op.

Author: Samuel Ahuno <ekwame001@gmail.com>
Date:   2026-04-18
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml


HPC_DIR = Path(__file__).resolve().parents[1]
DEMO_DIR = HPC_DIR.parent
REPO_ROOT = DEMO_DIR.parents[1]


def _load_configs():
    hpc_cfg = yaml.safe_load((HPC_DIR / "config.yaml").read_text())
    parent_cfg_path = HPC_DIR / hpc_cfg["parent_config"]
    parent_cfg = yaml.safe_load(parent_cfg_path.resolve().read_text())
    return hpc_cfg, parent_cfg


def _casetrack(*args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["casetrack", *args], check=check,
                          capture_output=True, text=True)


def _register_if_missing(project_dir: Path, level: str, row_id: str,
                          parent: str | None, meta: dict) -> bool:
    """Call `casetrack register`; treat UNIQUE/exists as no-op (idempotent)."""
    cmd = ["register", "--project-dir", str(project_dir),
           "--level", level, "--id", row_id]
    if parent:
        cmd += ["--parent", parent]
    meta_str = ",".join(f"{k}={v}" for k, v in meta.items()
                        if v is not None and v != "")
    if meta_str:
        cmd += ["--meta", meta_str]
    res = _casetrack(*cmd, check=False)
    if res.returncode == 0:
        return True
    if "UNIQUE constraint failed" in res.stderr or "already" in res.stderr.lower():
        return False
    sys.stderr.write(res.stdout); sys.stderr.write(res.stderr)
    sys.exit(res.returncode)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project-dir", default=None,
                    help="casetrack project dir (default: "
                         "$SANDBOX/project or paths.sandbox_default/project)")
    ap.add_argument("--template", default="hgsoc",
                    help="casetrack init --from-template (default: hgsoc)")
    ap.add_argument("--force", action="store_true",
                    help="Pass --force to casetrack init (overwrites existing DB)")
    args = ap.parse_args()

    hpc_cfg, parent_cfg = _load_configs()

    # Resolve project_dir: --project-dir > $SANDBOX/project > paths.sandbox_default
    if args.project_dir:
        project_dir = Path(args.project_dir)
    elif os.environ.get("SANDBOX"):
        project_dir = Path(os.environ["SANDBOX"]) / hpc_cfg["paths"]["project_subdir"]
    else:
        sandbox = Path(hpc_cfg["paths"]["sandbox_default"])
        project_dir = sandbox / hpc_cfg["paths"]["project_subdir"]

    if not (project_dir / "casetrack.db").exists() or args.force:
        force_flag = ["--force"] if args.force else []
        res = _casetrack("init", "--project-dir", str(project_dir),
                         "--from-template", args.template, *force_flag,
                         check=False)
        if res.returncode != 0:
            sys.stderr.write(res.stdout); sys.stderr.write(res.stderr)
            sys.exit(res.returncode)
        print(f"Initialized project at {project_dir}")
    else:
        print(f"Using existing project at {project_dir}")

    assay_plan = hpc_cfg["assays"]
    n_p = n_s = n_a = 0

    for patient in parent_cfg["cohort"]:
        pid = patient["patient_id"]
        if _register_if_missing(
            project_dir, "patient", pid, parent=None,
            meta={
                # hgsoc template declares: age, sex, diagnosis, brca_status,
                # neoadjuvant, pfs_months, os_months. Set what's known; the
                # rest can land later via `casetrack add-metadata`.
                "age": 58,
                "sex": "F",
                "brca_status": "brca1" if pid == "HGSOC_SIM_01" else "wt",
                "diagnosis": "HGSOC",
            },
        ):
            n_p += 1

        for spec in patient["specimens"]:
            specimen_id = f"{pid}_{spec['id_suffix']}"
            if _register_if_missing(
                project_dir, "specimen", specimen_id, parent=pid,
                meta={
                    "tissue_site": spec["tissue_site"],
                    "timepoint": "t0",
                },
            ):
                n_s += 1

            for assay_spec in assay_plan:
                runs = assay_spec["flowcell_runs"]
                for run_idx in range(1, runs + 1):
                    # assay_id encodes everything needed for unique identification
                    assay_id = (
                        f"{specimen_id}-{assay_spec['type']}-"
                        f"{assay_spec['subtype']}-R{run_idx:02d}"
                    )
                    if _register_if_missing(
                        project_dir, "assay", assay_id, parent=specimen_id,
                        meta={
                            "assay_type": assay_spec["type"],
                            "replicate": run_idx,
                        },
                    ):
                        n_a += 1

    print(f"Registered {n_p} patient(s), {n_s} specimen(s), {n_a} assay(s).")
    print(f"Project: {project_dir}")


if __name__ == "__main__":
    main()
