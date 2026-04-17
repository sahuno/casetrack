#!/usr/bin/env bash
#SBATCH --job-name=subset_chr
#SBATCH --account=greenbab
#SBATCH --partition=componc_cpu
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=01:30:00
#SBATCH --output=logs/subset_chr_%A_%a.out
#SBATCH --error=logs/subset_chr_%A_%a.err
#
# run_subset_chr.sh — subset a specimen's merged BAM to a single chromosome,
# keeping it sorted + indexed. Writes the chr-BAM path back to the specimen
# row as {CHR}_bam_path so downstream tools (modkit, sniffles, …) can target
# the small BAM in parallel — e.g. modkit on chr17 drops from ~8h on the
# full merged BAM to ~20 min on the subset.
#
# Required env:
#   SPECIMEN_ID       — specimen_id key value
#   PROJECT_DIR       — casetrack project directory
#   CHR               — target chromosome (e.g. chr17)
#   DEMO_SCRIPTS_DIR  — path to this patterns dir (kept for consistency; unused here)
#
# Optional:
#   SAMTOOLS_CONTAINER — apptainer image (if not on PATH)
#   SAMTOOLS_BIN       — default: samtools on PATH
#   CASETRACK_BIN      — default: casetrack on PATH
#
# Outputs (on the specimens table, via append at --level specimen):
#   {CHR}_bam_path       — absolute path to the sorted+indexed chr BAM
#   {CHR}_total_reads    — flagstat total
#   {CHR}_mapped_reads   — flagstat mapped
#   subset_{CHR}_done    — completion timestamp

set -euo pipefail

: "${SPECIMEN_ID:?run_subset_chr: SPECIMEN_ID is required}"
: "${PROJECT_DIR:?run_subset_chr: PROJECT_DIR is required}"
: "${CHR:?run_subset_chr: CHR is required (e.g. chr17)}"

SAMTOOLS_BIN="${SAMTOOLS_BIN:-samtools}"
SAMTOOLS_CONTAINER="${SAMTOOLS_CONTAINER:-}"
CASETRACK_BIN="${CASETRACK_BIN:-casetrack}"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${PROJECT_DIR}/results/subset_${CHR}/${SPECIMEN_ID}"
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${OUT_DIR}" "${LOG_DIR}"
LOG="${LOG_DIR}/subset_${CHR}_${SPECIMEN_ID}_${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === subset_${CHR} ${SPECIMEN_ID} ==="

# ── Look up the merged BAM from the specimen row ──────────────────────────────
MERGED_BAM="$("${CASETRACK_BIN}" query --project-dir "${PROJECT_DIR}" --fmt tsv \
    "SELECT merged_bam_path FROM specimens WHERE specimen_id = '${SPECIMEN_ID}'" \
    | tail -n +2 | head -1 || true)"
if [[ -z "${MERGED_BAM}" || "${MERGED_BAM}" == "None" ]]; then
    echo "Error: specimens.${SPECIMEN_ID}.merged_bam_path not set — run run_merge.sh first" >&2
    exit 1
fi
echo "MERGED_BAM=${MERGED_BAM}"
echo "CHR=${CHR}"

if [[ -n "${SAMTOOLS_CONTAINER}" ]]; then
    SAMTOOLS_CMD=(apptainer exec --bind /data1/greenbab "${SAMTOOLS_CONTAINER}" samtools)
    echo "samtools: apptainer ${SAMTOOLS_CONTAINER}"
else
    SAMTOOLS_CMD=("${SAMTOOLS_BIN}")
    echo "samtools: $(${SAMTOOLS_BIN} --version | head -1 || true)"
fi

CHR_BAM="${OUT_DIR}/${SPECIMEN_ID}_${CHR}.bam"

# ── Phase 1: extract the chromosome's reads ───────────────────────────────────
# --write-index produces a .csi companion so downstream tools can region-query.
# -b = BAM output; requires the merged BAM to be indexed (run_merge.sh does this).
"${SAMTOOLS_CMD[@]}" view -b \
    -@ "${SLURM_CPUS_PER_TASK:-4}" \
    --write-index \
    -o "${CHR_BAM}##idx##${CHR_BAM}.csi" \
    "${MERGED_BAM}" "${CHR}"
echo "[Phase 1] subset+index → ${CHR_BAM}"
ls -lh "${CHR_BAM}"*

# ── Phase 2: flagstat for the summary ─────────────────────────────────────────
FLAGSTAT="${OUT_DIR}/flagstat.txt"
"${SAMTOOLS_CMD[@]}" flagstat "${CHR_BAM}" > "${FLAGSTAT}"
echo "[Phase 2] flagstat → ${FLAGSTAT}"

# ── Phase 3: write specimen-level summary TSV + casetrack append ──────────────
SUMMARY_TSV="${OUT_DIR}/summary.tsv"
python3 - "${SPECIMEN_ID}" "${CHR}" "${CHR_BAM}" "${FLAGSTAT}" "${SUMMARY_TSV}" <<'PY'
import re, sys
spec, chrm, bam, fs_path, out = sys.argv[1:]
lines = [ln for ln in open(fs_path).read().splitlines() if ln.strip()]

def first_int(keyword):
    for ln in lines:
        if keyword in ln:
            m = re.match(r"^(\d+)", ln.strip())
            return int(m.group(1)) if m else 0
    return 0

total = first_int("in total")
mapped = first_int("mapped (")
cols = ["specimen_id", f"{chrm}_bam_path", f"{chrm}_total_reads", f"{chrm}_mapped_reads"]
with open(out, "w") as f:
    f.write("\t".join(cols) + "\n")
    f.write(f"{spec}\t{bam}\t{total}\t{mapped}\n")
print(f"wrote {out} (total={total:,}, mapped={mapped:,})")
PY
cat "${SUMMARY_TSV}"

"${CASETRACK_BIN}" append \
    --project-dir "${PROJECT_DIR}" \
    --level specimen \
    --analysis "subset_${CHR}" \
    --results "${SUMMARY_TSV}"
echo "[Phase 3] appended — specimens.${CHR}_bam_path now set on ${SPECIMEN_ID}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === DONE: subset_${CHR} ${SPECIMEN_ID} ==="
