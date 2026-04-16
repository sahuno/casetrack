#!/usr/bin/env bash
#SBATCH --job-name=sniffles
#SBATCH --account=greenbab
#SBATCH --partition=componc_cpu
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=04:00:00
#SBATCH --output=logs/sniffles_%A_%a.out
#SBATCH --error=logs/sniffles_%A_%a.err
#
# run_sniffles.sh — three-phase wrapper:
#   1) sniffles --input <BAM> --reference <FASTA> --vcf <VCF>
#   2) summarize VCF to per-assay TSV (scripts/summarize_sniffles.py)
#   3) casetrack append --project-dir ... --analysis sniffles
#
# Required env (set by submit_all.sh):
#   ASSAY_ID       — assay_id key value
#   BAM_PATH       — absolute path to the BAM (must be sorted + indexed)
#   PROJECT_DIR    — casetrack project directory
#   REF_FASTA      — reference genome FASTA (must have .fai index)
#   DEMO_SCRIPTS_DIR — path to the summarize_*.py scripts dir (set by submit_all.sh)
#
# Optional:
#   SNIFFLES_CONTAINER — apptainer image with sniffles (if not on PATH)
#   CASETRACK_BIN      — casetrack executable (default: casetrack on PATH)
#   SNIFFLES_EXTRA     — extra flags appended to the sniffles command
#                        (e.g., '--tandem-repeats hg38_repeats.bed --minsvlen 30')
#
# Notes:
#   * Sniffles2 >= 2.3 needs `--input` (not positional), and `--vcf` for output.
#   * For ONT reads, default threshold (--minsvlen 50) is usually reasonable.
#   * The produced VCF lands under ${PROJECT_DIR}/results/sniffles/${ASSAY_ID}/
#     so it's co-located with the summary TSV and the per-job log.

set -euo pipefail

: "${ASSAY_ID:?run_sniffles: ASSAY_ID is required}"
: "${BAM_PATH:?run_sniffles: BAM_PATH is required}"
: "${PROJECT_DIR:?run_sniffles: PROJECT_DIR is required}"
: "${REF_FASTA:?run_sniffles: REF_FASTA is required}"
: "${DEMO_SCRIPTS_DIR:?run_sniffles: DEMO_SCRIPTS_DIR is required (exported by submit_all.sh)}"

CASETRACK_BIN="${CASETRACK_BIN:-casetrack}"
SNIFFLES_CONTAINER="${SNIFFLES_CONTAINER:-}"
SNIFFLES_EXTRA="${SNIFFLES_EXTRA:-}"

HERE="$(cd "${DEMO_SCRIPTS_DIR}" && cd .. && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
RESULTS_DIR="${PROJECT_DIR}/results/sniffles/${ASSAY_ID}"
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${RESULTS_DIR}" "${LOG_DIR}"
LOG="${LOG_DIR}/sniffles_${ASSAY_ID}_${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === sniffles ${ASSAY_ID} ==="
echo "BAM_PATH=${BAM_PATH}"
echo "PROJECT_DIR=${PROJECT_DIR}"
echo "REF_FASTA=${REF_FASTA}"

# Container-wrapped or native sniffles.
if [[ -n "${SNIFFLES_CONTAINER}" ]]; then
    SNIFFLES=(apptainer exec --bind /data1/greenbab "${SNIFFLES_CONTAINER}" sniffles)
    echo "sniffles: apptainer ${SNIFFLES_CONTAINER}"
else
    SNIFFLES=(sniffles)
    echo "sniffles: $(sniffles --version 2>&1 | head -1 || true)"
fi

# ── Phase 1: sniffles pileup → VCF ────────────────────────────────────────────
VCF="${RESULTS_DIR}/${ASSAY_ID}.sniffles.vcf.gz"
"${SNIFFLES[@]}" \
    --input "${BAM_PATH}" \
    --reference "${REF_FASTA}" \
    --vcf "${VCF}" \
    --threads "${SLURM_CPUS_PER_TASK:-8}" \
    ${SNIFFLES_EXTRA}
echo "[Phase 1] sniffles → ${VCF}"

# ── Phase 2: summarize ────────────────────────────────────────────────────────
SUMMARY_TSV="${RESULTS_DIR}/summary.tsv"
python3 "${HERE}/scripts/summarize_sniffles.py" \
    --assay-id "${ASSAY_ID}" \
    --input "${VCF}" \
    --output "${SUMMARY_TSV}"
echo "[Phase 2] Summary: ${SUMMARY_TSV}"
head -2 "${SUMMARY_TSV}"

# ── Phase 3: casetrack append ─────────────────────────────────────────────────
"${CASETRACK_BIN}" append \
    --project-dir "${PROJECT_DIR}" \
    --analysis sniffles \
    --results "${SUMMARY_TSV}"
echo "[Phase 3] Appended to casetrack project."

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === DONE: sniffles ${ASSAY_ID} ==="
