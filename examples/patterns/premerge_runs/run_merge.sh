#!/usr/bin/env bash
#SBATCH --job-name=merge_bams
#SBATCH --account=greenbab
#SBATCH --partition=componc_cpu
#SBATCH --cpus-per-task=8
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --output=logs/merge_%A_%a.out
#SBATCH --error=logs/merge_%A_%a.err
#
# run_merge.sh — per-specimen three-phase wrapper for samtools merge.
#   1) query casetrack's _active view for this specimen's non-censored assay
#      BAM paths (pre-merge QC failures are automatically excluded)
#   2) samtools merge → specimens.merged_bam_path
#   3) flagstat on merged BAM + summarize + casetrack append --level specimen
#
# The casetrack.db gets `merged_bam_path` auto-added to the `specimens`
# table via ALTER TABLE ADD COLUMN on the first append. No schema edit.
#
# Required env:
#   SPECIMEN_ID       — specimen_id key value
#   PROJECT_DIR       — casetrack project directory
#   DEMO_SCRIPTS_DIR  — path to this patterns dir (summarize_merge.py, etc.)
#
# Optional:
#   SAMTOOLS_BIN / SAMTOOLS_CONTAINER — same as run_premerge_flagstat.sh
#   CASETRACK_BIN                     — default: casetrack on PATH
#   MERGE_OUT_BASE                    — override base dir for merged BAMs
#                                       (default: ${PROJECT_DIR}/results/merge_bams)
#   SUFFIX                            — appended to the filename before
#                                       _merged.bam (e.g. "hg38" →
#                                       <specimen>_hg38_merged.bam)
#   MIN_TOTAL_READS                   — qc_pass threshold for MERGED BAM
#   MIN_MAPPED_PCT                    — qc_pass threshold for MERGED BAM

set -euo pipefail

: "${SPECIMEN_ID:?run_merge: SPECIMEN_ID is required}"
: "${PROJECT_DIR:?run_merge: PROJECT_DIR is required}"
: "${DEMO_SCRIPTS_DIR:?run_merge: DEMO_SCRIPTS_DIR is required}"

SAMTOOLS_BIN="${SAMTOOLS_BIN:-samtools}"
SAMTOOLS_CONTAINER="${SAMTOOLS_CONTAINER:-}"
CASETRACK_BIN="${CASETRACK_BIN:-casetrack}"
MERGE_OUT_BASE="${MERGE_OUT_BASE:-${PROJECT_DIR}/results/merge_bams}"
SUFFIX="${SUFFIX:-}"
MIN_TOTAL_READS="${MIN_TOTAL_READS:-1000000}"
MIN_MAPPED_PCT="${MIN_MAPPED_PCT:-95.0}"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${MERGE_OUT_BASE}/${SPECIMEN_ID}"
if [[ -n "${SUFFIX}" ]]; then
    MERGED_BAM="${OUT_DIR}/${SPECIMEN_ID}_${SUFFIX}_merged.bam"
else
    MERGED_BAM="${OUT_DIR}/${SPECIMEN_ID}_merged.bam"
fi
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${OUT_DIR}" "${LOG_DIR}"
LOG="${LOG_DIR}/merge_${SPECIMEN_ID}_${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === merge ${SPECIMEN_ID} ==="
echo "PROJECT_DIR=${PROJECT_DIR}"
echo "MERGED_BAM=${MERGED_BAM}"

# Build the samtools invocation.
if [[ -n "${SAMTOOLS_CONTAINER}" ]]; then
    SAMTOOLS_CMD=(apptainer exec --bind /data1/greenbab "${SAMTOOLS_CONTAINER}" samtools)
    echo "samtools: apptainer ${SAMTOOLS_CONTAINER}"
else
    SAMTOOLS_CMD=("${SAMTOOLS_BIN}")
    echo "samtools: $(${SAMTOOLS_BIN} --version | head -1 || true)"
fi

# ── Phase 1: query _active for this specimen's non-censored assay BAMs ────────
INPUT_LIST="${OUT_DIR}/input_bams.txt"
"${CASETRACK_BIN}" query --project-dir "${PROJECT_DIR}" --fmt tsv \
    "SELECT bam_path FROM _active WHERE specimen_id = '${SPECIMEN_ID}' AND bam_path IS NOT NULL ORDER BY assay_id" \
    | tail -n +2 > "${INPUT_LIST}"

N_INPUTS=$(wc -l < "${INPUT_LIST}")
if [[ "${N_INPUTS}" -eq 0 ]]; then
    echo "[Phase 1] No active (non-censored) BAMs for specimen ${SPECIMEN_ID}" >&2
    exit 1
fi
echo "[Phase 1] ${N_INPUTS} active pre-merge BAM(s) for ${SPECIMEN_ID}:"
cat "${INPUT_LIST}"

# ── Phase 2: samtools merge ───────────────────────────────────────────────────
# -f forces overwrite of an existing merged BAM (idempotent re-run)
# --write-index produces the .bam.csi companion
"${SAMTOOLS_CMD[@]}" merge \
    -f \
    -@ "${SLURM_CPUS_PER_TASK:-8}" \
    --write-index \
    "${MERGED_BAM}" \
    $(cat "${INPUT_LIST}")
echo "[Phase 2] merged → ${MERGED_BAM}"
ls -lh "${MERGED_BAM}"*

# flagstat on the merged result — also sanity-check for the summarizer
FLAGSTAT_MERGED="${OUT_DIR}/flagstat_merged.txt"
"${SAMTOOLS_CMD[@]}" flagstat "${MERGED_BAM}" > "${FLAGSTAT_MERGED}"

# ── Phase 3: summarize + append at specimen level ─────────────────────────────
SUMMARY_TSV="${OUT_DIR}/summary.tsv"
python3 "${DEMO_SCRIPTS_DIR}/summarize_merge.py" \
    --specimen-id "${SPECIMEN_ID}" \
    --merged-bam-path "${MERGED_BAM}" \
    --input-list "${INPUT_LIST}" \
    --flagstat-on-merged "${FLAGSTAT_MERGED}" \
    --output "${SUMMARY_TSV}" \
    --min-total-reads "${MIN_TOTAL_READS}" \
    --min-mapped-pct "${MIN_MAPPED_PCT}"
echo "[Phase 3] summary:"
cat "${SUMMARY_TSV}"

"${CASETRACK_BIN}" append \
    --project-dir "${PROJECT_DIR}" \
    --level specimen \
    --analysis merge \
    --results "${SUMMARY_TSV}"
echo "[Phase 3] Appended at specimen level — merged_bam_path now set on ${SPECIMEN_ID}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === DONE: merge ${SPECIMEN_ID} ==="
