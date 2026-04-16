#!/usr/bin/env bash
#SBATCH --job-name=modkit_pileup
#SBATCH --account=greenbab
#SBATCH --partition=componc_cpu
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=08:00:00
#SBATCH --output=logs/modkit_%A_%a.out
#SBATCH --error=logs/modkit_%A_%a.err
#
# run_modkit.sh — three-phase wrapper for modkit methylation calling:
#   1) modkit pileup <BAM> → bedMethyl
#   2) summarize bedMethyl to per-assay TSV
#   3) casetrack append --project-dir ... --analysis modkit
#
# Required env:
#   ASSAY_ID         — assay_id key value
#   BAM_PATH         — absolute path to the BAM (must have MM/ML tags)
#   PROJECT_DIR      — casetrack project directory
#   REF_FASTA        — reference genome FASTA matching the BAM alignment
#
# Optional:
#   MODKIT_CONTAINER — apptainer image with modkit (if not on PATH)
#   CASETRACK_BIN    — casetrack executable (default: casetrack on PATH)
#   MOD_CALL_STRING  — modkit `--combine-strands` / calls; default is
#                      CpG-only methylation for downstream plotting.
#   CHR_LIMIT        — restrict pileup to a chromosome (default: empty)
#                      Useful for the chr21 demo: `export CHR_LIMIT=chr21`.
#
# NOTE: modkit requires the BAM to carry MM/ML tags from dorado. A BAM
# that lacks them will produce an empty bedMethyl and the summarizer
# will write zeros.

set -euo pipefail

: "${ASSAY_ID:?run_modkit: ASSAY_ID is required}"
: "${BAM_PATH:?run_modkit: BAM_PATH is required}"
: "${PROJECT_DIR:?run_modkit: PROJECT_DIR is required}"
: "${REF_FASTA:?run_modkit: REF_FASTA is required}"

CASETRACK_BIN="${CASETRACK_BIN:-casetrack}"
MODKIT_CONTAINER="${MODKIT_CONTAINER:-}"
CHR_LIMIT="${CHR_LIMIT:-}"

# SLURM copies the submitted script; use the explicit scripts dir from
# submit_all.sh rather than resolving via BASH_SOURCE[0].
: "${DEMO_SCRIPTS_DIR:?run_modkit: DEMO_SCRIPTS_DIR is required (exported by submit_all.sh)}"
HERE="$(cd "${DEMO_SCRIPTS_DIR}" && cd .. && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
RESULTS_DIR="${PROJECT_DIR}/results/modkit/${ASSAY_ID}"
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${RESULTS_DIR}" "${LOG_DIR}"
LOG="${LOG_DIR}/modkit_${ASSAY_ID}_${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === modkit ${ASSAY_ID} ==="
echo "BAM_PATH=${BAM_PATH}"
echo "PROJECT_DIR=${PROJECT_DIR}"
echo "REF_FASTA=${REF_FASTA}"

# modkit invocation — either native binary on PATH or apptainer-wrapped.
if [[ -n "${MODKIT_CONTAINER}" ]]; then
    MODKIT="apptainer exec --bind /data1/greenbab ${MODKIT_CONTAINER} modkit"
else
    MODKIT="modkit"
fi
echo "modkit cmd: ${MODKIT}"

# ── Phase 1: modkit pileup → bedMethyl ────────────────────────────────────────
BEDMETHYL="${RESULTS_DIR}/${ASSAY_ID}.bedMethyl"
REGION_ARGS=""
if [[ -n "${CHR_LIMIT}" ]]; then
    REGION_ARGS="--region ${CHR_LIMIT}"
fi

MOD_BASES="${MOD_BASES:-5mC 5hmC}"
${MODKIT} pileup \
    ${BAM_PATH} \
    ${BEDMETHYL} \
    --reference ${REF_FASTA} \
    --modified-bases ${MOD_BASES} \
    --cpg \
    ${REGION_ARGS} \
    --threads ${SLURM_CPUS_PER_TASK:-8}
echo "[Phase 1] modkit pileup → ${BEDMETHYL}"

# ── Phase 2: summarize bedMethyl ──────────────────────────────────────────────
SUMMARY_TSV="${RESULTS_DIR}/summary.tsv"
python3 "${HERE}/scripts/summarize_modkit.py" \
    --assay-id "${ASSAY_ID}" \
    --input "${BEDMETHYL}" \
    --output "${SUMMARY_TSV}"
echo "[Phase 2] Summary: ${SUMMARY_TSV}"
head -2 "${SUMMARY_TSV}"

# ── Phase 3: casetrack append ─────────────────────────────────────────────────
"${CASETRACK_BIN}" append \
    --project-dir "${PROJECT_DIR}" \
    --analysis modkit \
    --results "${SUMMARY_TSV}"
echo "[Phase 3] Appended to casetrack project."

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === DONE: modkit ${ASSAY_ID} ==="
