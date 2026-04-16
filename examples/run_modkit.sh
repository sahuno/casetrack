#!/bin/bash
#SBATCH --job-name=modkit_case
#SBATCH --output=logs/modkit_%A_%a.out
#SBATCH --error=logs/modkit_%A_%a.err
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=4:00:00
#
# Usage:
#   # Single sample
#   sbatch run_modkit.sh SAMPLE_001 /path/to/bam manifest.tsv
#
#   # Array job (reads from samples.txt)
#   sbatch --array=0-49 run_modkit.sh __ARRAY__ /path/to/bams manifest.tsv
#
# ─────────────────────────────────────────────────────────

set -euo pipefail

# ── Resolve sample ID ──────────────────────────────────
if [ "$1" == "__ARRAY__" ]; then
    # Array mode: read sample ID from samples.txt
    SAMPLES_FILE="samples.txt"
    readarray -t SAMPLES < "${SAMPLES_FILE}"
    SAMPLE_ID="${SAMPLES[$SLURM_ARRAY_TASK_ID]}"
    BAM_DIR="$2"
    BAM="${BAM_DIR}/${SAMPLE_ID}.sorted.bam"
else
    SAMPLE_ID="$1"
    BAM="$2"
fi

MANIFEST="${3:-manifest.tsv}"
# v0.3: set PROJECT_DIR to a casetrack project directory (containing
# casetrack.toml + casetrack.db) to use the new SQLite backend. If set,
# takes precedence over MANIFEST.
PROJECT_DIR="${PROJECT_DIR:-}"
LEVEL="${LEVEL:-assay}"
RESULTS_DIR="results/modkit"
REF="ref/mm10.fa"

echo "=========================================="
echo "  casetrack: modkit_methylation"
echo "  Sample:    ${SAMPLE_ID}"
echo "  BAM:       ${BAM}"
echo "  Manifest:  ${MANIFEST}"
echo "  Job ID:    ${SLURM_JOB_ID:-local}"
echo "  Started:   $(date)"
echo "=========================================="

# ── Phase 1: Run analysis ──────────────────────────────
mkdir -p "${RESULTS_DIR}/${SAMPLE_ID}"

apptainer exec containers/modkit.sif modkit pileup \
    "${BAM}" \
    "${RESULTS_DIR}/${SAMPLE_ID}/pileup.bed" \
    --cpg \
    --ref "${REF}" \
    --threads "${SLURM_CPUS_PER_TASK:-8}"

echo "[Phase 1] modkit pileup complete."

# ── Phase 2: Summarize to per-sample TSV ───────────────
# The summary script distills raw output into manifest-ready columns.
# Contract: must produce a TSV with sample_id as the first column.
python3 scripts/summarize_modkit.py \
    --input "${RESULTS_DIR}/${SAMPLE_ID}/pileup.bed" \
    --sample "${SAMPLE_ID}" \
    --output "${RESULTS_DIR}/${SAMPLE_ID}/summary.tsv"

echo "[Phase 2] Summary TSV written."
echo "  Columns: $(head -1 ${RESULTS_DIR}/${SAMPLE_ID}/summary.tsv)"

# ── Phase 3: Append to manifest / project ───────────────────────
# casetrack handles file locking (v0.2) or WAL + BEGIN IMMEDIATE (v0.3),
# provenance logging, and column merging.
if [[ -n "$PROJECT_DIR" ]]; then
    casetrack append \
        --project-dir "${PROJECT_DIR}" \
        --level "${LEVEL}" \
        --results "${RESULTS_DIR}/${SAMPLE_ID}/summary.tsv" \
        --analysis modkit_methylation
else
    # v0.2 flat-manifest path (deprecated — migrate with `casetrack migrate`)
    casetrack append \
        --manifest "${MANIFEST}" \
        --results "${RESULTS_DIR}/${SAMPLE_ID}/summary.tsv" \
        --key sample_id \
        --analysis modkit_methylation
fi

echo "[Phase 3] Manifest updated."
echo "=========================================="
echo "  Completed: $(date)"
echo "=========================================="
