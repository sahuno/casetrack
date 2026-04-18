#!/usr/bin/env python3
"""05_bootstrap_casetrack.py — wire the simulated cohort into a casetrack project.

Creates:
    sandbox/hgsoc_sim/project/
        casetrack.toml
        casetrack.db
        provenance.jsonl

Registers each patient / specimen / assay from config.yaml, then appends
the per-assay summary TSVs emitted by 04_summarize_mock.py. Appends use
`--column-prefix` to namespace per-assay-type metrics — so DNA's
`mock_mean_meth` lands as `ont_dna_mock_mean_meth` on the assays table,
and RNA's lands as `ont_rna_mock_mean_meth`. This is the convention v0.4.1
introduced for per-analysis scoping (see CHANGELOG).

Autoflag kicks in on any assay whose summary has `qc_pass=False`,
producing a `qc_events` row inside the same transaction as the append —
the v0.4 QC path being demonstrated.

Idempotent on second invocation: strict-FK re-registration is a no-op for
rows that already exist, and `append` fills NaN-only cells on re-run.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: pyyaml is required. `pip install pyyaml --user`.", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
DEFAULT_SANDBOX = REPO_ROOT / "sandbox" / "hgsoc_sim"


# Column-prefix per assay type. Keeps DNA and RNA metrics in separate
# columns on the shared `assays` table so they never silently clobber
# each other (the v0.4.1 --column-prefix use case).
COLUMN_PREFIX_BY_ASSAY: dict[str, str] = {
    "ONT-DNA": "ont_dna",
    "ONT-RNA": "ont_rna",
}


def _ct(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Thin wrapper over `casetrack`."""
    return subprocess.run(
        ["casetrack", *args], check=check, capture_output=True, text=True
    )


def _register_if_missing(
    project_dir: Path,
    level: str,
    row_id: str,
    parent: str | None = None,
    meta: str | None = None,
) -> None:
    """Call `casetrack register`, tolerating 'already exists' collisions."""
    args: list[str] = [
        "register", "--project-dir", str(project_dir),
        "--level", level, "--id", row_id,
    ]
    if parent is not None:
        args += ["--parent", parent]
    if meta is not None:
        args += ["--meta", meta]
    result = _ct(*args, check=False)
    if result.returncode == 0:
        print(f"[05] registered {level:<8} {row_id}")
        return
    if (
        "UNIQUE constraint" in result.stderr
        or "already exists" in result.stderr.lower()
    ):
        print(f"[05] {level:<8} {row_id} already registered")
        return
    sys.stderr.write(result.stderr)
    result.check_returncode()


def bootstrap(config: Path, sandbox: Path) -> Path:
    cfg = yaml.safe_load(config.read_text())
    project_dir = sandbox / "project"
    summary_dir = sandbox / "summaries"
    if not summary_dir.exists():
        print(
            f"Error: summary dir {summary_dir} missing — run 04_summarize_mock.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 1. Init the project (once).
    if not (project_dir / "casetrack.toml").exists():
        project_dir.mkdir(parents=True, exist_ok=True)
        _ct("init", "--project-dir", str(project_dir), "--from-template", "hgsoc")
        print(f"[05] initialized project at {project_dir}")
    else:
        print(f"[05] reusing existing project at {project_dir}")

    # 2. Register everything.
    for patient in cfg["cohort"]:
        pid = patient["patient_id"]
        _register_if_missing(project_dir, "patient", pid)
        for spec in patient["specimens"]:
            suffix = spec["id_suffix"]
            specimen_id = f"{pid}-{suffix}"
            _register_if_missing(
                project_dir, "specimen", specimen_id,
                parent=pid,
                meta=f"tissue_site={spec['tissue_site']}",
            )
            for assay in spec.get("assays") or []:
                atype = assay["type"]
                assay_id = f"{specimen_id}-{atype}"
                _register_if_missing(
                    project_dir, "assay", assay_id,
                    parent=specimen_id,
                    meta=f"assay_type={atype}",
                )

    # 3. Append each assay's mock summary. Column-prefix is keyed on assay
    # type so DNA and RNA columns never collide on the shared assays table.
    # Autoflag will fire on any summary with qc_pass=False.
    for patient in cfg["cohort"]:
        pid = patient["patient_id"]
        for spec in patient["specimens"]:
            suffix = spec["id_suffix"]
            for assay in spec.get("assays") or []:
                atype = assay["type"]
                assay_id = f"{pid}-{suffix}-{atype}"
                tsv = summary_dir / f"{assay_id}.summary.tsv"
                if not tsv.exists():
                    print(f"[05] WARN: missing summary for {assay_id} — skipping",
                          file=sys.stderr)
                    continue

                prefix = COLUMN_PREFIX_BY_ASSAY.get(atype)
                append_args = [
                    "append",
                    "--project-dir", str(project_dir),
                    "--results", str(tsv),
                    "--analysis", f"mock_summary_{atype.lower().replace('-', '_')}",
                ]
                if prefix:
                    append_args += ["--column-prefix", prefix]

                result = _ct(*append_args, check=False)
                if result.returncode != 0:
                    sys.stderr.write(result.stderr)
                    result.check_returncode()
                for line in result.stdout.strip().splitlines():
                    print(f"[05]   {line}")

    print()
    print(f"[05] project ready: {project_dir}")
    print(f"[05] try:")
    print(f"     casetrack status  --project-dir {project_dir} --usable")
    print(f"     casetrack cohort  --project-dir {project_dir} "
          f"--assay-type ONT-DNA --pair-by tissue_site")
    print(f"     casetrack cohort  --project-dir {project_dir} "
          f"--assay-type ONT-RNA --pair-by tissue_site   # after phase f")
    return project_dir


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--sandbox", default=str(DEFAULT_SANDBOX))
    args = ap.parse_args()
    bootstrap(Path(args.config), Path(args.sandbox))
    return 0


if __name__ == "__main__":
    sys.exit(main())
