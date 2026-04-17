#!/usr/bin/env bash
#SBATCH --job-name=premerge_flagstat
#SBATCH --account=greenbab
#SBATCH --partition=componc_cpu
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:30:00
#SBATCH --output=logs/premerge_flagstat_%A_%a.out
#SBATCH --error=logs/premerge_flagstat_%A_%a.err
#
# run_premerge_flagstat.sh — per-assay (pre-merge) QC three-phase wrapper.
#   1) samtools flagstat on the pre-merge BAM
#   2) summarize to per-assay TSV with qc_pass / qc_fail_reason / qc_warn
#   3) casetrack append --analysis premerge_flagstat — auto-emits qc_events
#      with source='slurm' for any row where qc_pass=false
#
# Required env (set by submit_merge_pipeline.sh or by hand):
#   ASSAY_ID          — assay_id key value
#   BAM_PATH          — absolute path to the pre-merge BAM
#   PROJECT_DIR       — casetrack project directory
#   DEMO_SCRIPTS_DIR  — path to this patterns dir (for the summarizer script)
#
# Optional:
#   SAMTOOLS_BIN / SAMTOOLS_CONTAINER — same contract as run_flagstat.sh
#   CASETRACK_BIN                     — default: casetrack on PATH
#   MIN_TOTAL_READS                   — qc_pass threshold (default: 1000000)
#   MIN_MAPPED_PCT                    — qc_pass threshold (default: 95.0)

set -euo pipefail

: "${ASSAY_ID:?run_premerge_flagstat: ASSAY_ID is required}"
: "${BAM_PATH:?run_premerge_flagstat: BAM_PATH is required}"
: "${PROJECT_DIR:?run_premerge_flagstat: PROJECT_DIR is required}"
: "${DEMO_SCRIPTS_DIR:?run_premerge_flagstat: DEMO_SCRIPTS_DIR is required}"

SAMTOOLS_BIN="${SAMTOOLS_BIN:-samtools}"
SAMTOOLS_CONTAINER="${SAMTOOLS_CONTAINER:-}"
CASETRACK_BIN="${CASETRACK_BIN:-casetrack}"
MIN_TOTAL_READS="${MIN_TOTAL_READS:-1000000}"
MIN_MAPPED_PCT="${MIN_MAPPED_PCT:-95.0}"

STAMP="$(date +%Y%m%d_%H%M%S)"
RESULTS_DIR="${PROJECT_DIR}/results/premerge_flagstat/${ASSAY_ID}"
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${RESULTS_DIR}" "${LOG_DIR}"
LOG="${LOG_DIR}/premerge_flagstat_${ASSAY_ID}_${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === premerge_flagstat ${ASSAY_ID} ==="
echo "BAM_PATH=${BAM_PATH}"
echo "PROJECT_DIR=${PROJECT_DIR}"
echo "thresholds: min_total_reads=${MIN_TOTAL_READS}, min_mapped_pct=${MIN_MAPPED_PCT}"

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

# ── Phase 2: summarize with autoflag columns ──────────────────────────────────
SUMMARY_TSV="${RESULTS_DIR}/summary.tsv"
python3 "${DEMO_SCRIPTS_DIR}/summarize_premerge_flagstat.py" \
    --assay-id "${ASSAY_ID}" \
    --input "${FLAGSTAT_OUT}" \
    --output "${SUMMARY_TSV}" \
    --min-total-reads "${MIN_TOTAL_READS}" \
    --min-mapped-pct "${MIN_MAPPED_PCT}"
echo "[Phase 2] Summary: ${SUMMARY_TSV}"
cat "${SUMMARY_TSV}"

# ── Phase 3: casetrack append — autoflag emits qc_events in same txn ──────────
"${CASETRACK_BIN}" append \
    --project-dir "${PROJECT_DIR}" \
    --analysis premerge_flagstat \
    --results "${SUMMARY_TSV}"
echo "[Phase 3] Appended (any qc_pass=false → qc_events emitted with source='slurm')"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === DONE: premerge_flagstat ${ASSAY_ID} ==="
