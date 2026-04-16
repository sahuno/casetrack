#!/usr/bin/env bash
#SBATCH --job-name=flagstat
#SBATCH --account=greenbab
#SBATCH --partition=componc_cpu
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:30:00
#SBATCH --output=logs/flagstat_%A_%a.out
#SBATCH --error=logs/flagstat_%A_%a.err
#
# run_flagstat.sh — three-phase wrapper:
#   1) samtools flagstat <BAM>
#   2) summarize to per-assay TSV (scripts/summarize_flagstat.py)
#   3) casetrack append --project-dir ... --analysis flagstat
#
# Required env (set by submit_all.sh):
#   ASSAY_ID      — assay_id key value (== sample_id in the GIAB sheet)
#   BAM_PATH      — absolute path to the BAM
#   PROJECT_DIR   — casetrack project directory
#
# Optional:
#   SAMTOOLS_BIN       — samtools executable path (default: samtools on PATH)
#   SAMTOOLS_CONTAINER — apptainer image to wrap samtools with (overrides BIN)
#                        e.g. /data1/greenbab/software/images/onttools_v3.10.sif
#   CASETRACK_BIN      — casetrack executable (default: casetrack on PATH)
#
# If SAMTOOLS_CONTAINER is set, the script runs
#   apptainer exec --bind /data1/greenbab <CONTAINER> samtools flagstat ...
# otherwise it falls back to the native SAMTOOLS_BIN path.

set -euo pipefail

: "${ASSAY_ID:?run_flagstat: ASSAY_ID is required}"
: "${BAM_PATH:?run_flagstat: BAM_PATH is required}"
: "${PROJECT_DIR:?run_flagstat: PROJECT_DIR is required}"

SAMTOOLS_BIN="${SAMTOOLS_BIN:-samtools}"
SAMTOOLS_CONTAINER="${SAMTOOLS_CONTAINER:-}"
CASETRACK_BIN="${CASETRACK_BIN:-casetrack}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && cd .. && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
RESULTS_DIR="${PROJECT_DIR}/results/flagstat/${ASSAY_ID}"
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${RESULTS_DIR}" "${LOG_DIR}"
LOG="${LOG_DIR}/flagstat_${ASSAY_ID}_${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === flagstat ${ASSAY_ID} ==="
echo "BAM_PATH=${BAM_PATH}"
echo "PROJECT_DIR=${PROJECT_DIR}"

# Build the samtools invocation — container-wrapped or native.
if [[ -n "${SAMTOOLS_CONTAINER}" ]]; then
    SAMTOOLS_CMD=(apptainer exec --bind /data1/greenbab "${SAMTOOLS_CONTAINER}" samtools)
    echo "samtools: apptainer ${SAMTOOLS_CONTAINER}"
else
    SAMTOOLS_CMD=("${SAMTOOLS_BIN}")
    echo "samtools: $(${SAMTOOLS_BIN} --version | head -1 || true)"
fi

# ── Phase 1: flagstat ─────────────────────────────────────────────────────────
FLAGSTAT_OUT="${RESULTS_DIR}/flagstat.txt"
"${SAMTOOLS_CMD[@]}" flagstat "${BAM_PATH}" > "${FLAGSTAT_OUT}"
echo "[Phase 1] samtools flagstat → ${FLAGSTAT_OUT}"

# ── Phase 2: summarize to TSV ─────────────────────────────────────────────────
SUMMARY_TSV="${RESULTS_DIR}/summary.tsv"
python3 "${HERE}/scripts/summarize_flagstat.py" \
    --assay-id "${ASSAY_ID}" \
    --input "${FLAGSTAT_OUT}" \
    --output "${SUMMARY_TSV}"
echo "[Phase 2] Summary: ${SUMMARY_TSV}"
head -2 "${SUMMARY_TSV}"

# ── Phase 3: casetrack append ─────────────────────────────────────────────────
"${CASETRACK_BIN}" append \
    --project-dir "${PROJECT_DIR}" \
    --analysis flagstat \
    --results "${SUMMARY_TSV}"
echo "[Phase 3] Appended to casetrack project."

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === DONE: flagstat ${ASSAY_ID} ==="
