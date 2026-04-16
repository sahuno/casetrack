#!/usr/bin/env bash
# submit_all.sh — fan out one SLURM job per (analysis × assay) from the
# GIAB chr21 sample sheet. Reads the sheet, bootstraps the casetrack
# project, and emits sbatch commands for each run_<analysis>.sh.
#
# Usage:
#   PROJECT_DIR=/path/to/proj bash submit_all.sh             # dry-run (prints commands)
#   PROJECT_DIR=/path/to/proj bash submit_all.sh --submit    # actually sbatch
#
# Optional env:
#   ANALYSES="flagstat modkit"   (default: flagstat)
#   REF_FASTA=/path/to/hg38.fa   (required if modkit is in ANALYSES)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
SHEET="${ROOT}/sample_sheet.tsv"

# Exported to the compute-node job so it can find the summarizer scripts —
# SLURM copies the run_*.sh scripts to /var/spool/slurmd/scripts/, which
# breaks $(dirname "${BASH_SOURCE[0]}") as a way to locate siblings.
export DEMO_SCRIPTS_DIR="${ROOT}/scripts"

SUBMIT=""
if [[ "${1:-}" == "--submit" ]]; then
    SUBMIT=1
fi

: "${PROJECT_DIR:?submit_all: set PROJECT_DIR}"
ANALYSES="${ANALYSES:-flagstat}"

# ── 1. Bootstrap the project (idempotent) ─────────────────────────────────────
python3 "${ROOT}/bootstrap.py" \
    --sample-sheet "${SHEET}" \
    --project-dir "${PROJECT_DIR}"

# ── 2. Fan out per-analysis sbatch commands ───────────────────────────────────
# skip header (NR>1), emit (assay_id, bam_path) tuples.
while IFS=$'\t' read -r patient_id assay_id condition assay_type bam_path; do
    [[ -z "${assay_id}" ]] && continue
    for analysis in ${ANALYSES}; do
        script="${HERE}/run_${analysis}.sh"
        if [[ ! -x "${script}" ]]; then
            echo "Skipping ${analysis}: ${script} not found/executable" >&2
            continue
        fi
        exports="ASSAY_ID=${assay_id},BAM_PATH=${bam_path},PROJECT_DIR=${PROJECT_DIR}"
        if [[ "${analysis}" == "modkit" ]]; then
            if [[ -z "${REF_FASTA:-}" ]]; then
                echo "Skipping modkit for ${assay_id}: REF_FASTA not set" >&2
                continue
            fi
            exports+=",REF_FASTA=${REF_FASTA}"
            # chr21 restriction for the demo BAMs.
            exports+=",CHR_LIMIT=chr21"
        fi
        # --chdir so the SBATCH #output/logs/ path lands inside the project dir.
        mkdir -p "${PROJECT_DIR}/logs"
        cmd="sbatch --chdir=${PROJECT_DIR} --export=ALL,${exports} ${script}"
        if [[ -n "${SUBMIT}" ]]; then
            eval "${cmd}"
        else
            echo "${cmd}"
        fi
    done
done < <(tail -n +2 "${SHEET}")

if [[ -z "${SUBMIT}" ]]; then
    echo
    echo "Dry-run complete. Re-run with --submit to actually dispatch."
fi
