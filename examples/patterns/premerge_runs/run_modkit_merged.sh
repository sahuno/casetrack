#!/usr/bin/env bash
#SBATCH --job-name=modkit_merged
#SBATCH --account=greenbab
#SBATCH --partition=componc_cpu
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=08:00:00
#SBATCH --output=logs/modkit_merged_%A_%a.out
#SBATCH --error=logs/modkit_merged_%A_%a.err
#
# run_modkit_merged.sh — modkit pileup against specimens.merged_bam_path
# (the post-merge BAM), writing analysis columns at specimen level.
#
# This is the specimen-level variant of examples/giab_chr21/slurm/run_modkit.sh.
# The BAM to process is pulled from casetrack — not from env — so censored
# specimens or unfinished merges automatically short-circuit.
#
# Required env:
#   SPECIMEN_ID       — specimen_id key value
#   PROJECT_DIR       — casetrack project directory
#   REF_FASTA         — reference genome FASTA (must be indexed)
#   DEMO_SCRIPTS_DIR  — path to this patterns dir
#
# Optional:
#   MODKIT_CONTAINER  — apptainer image with modkit
#   CASETRACK_BIN     — default: casetrack on PATH
#   MOD_BASES         — default: "5mC 5hmC"
#   CHR_LIMIT         — e.g. "chr21" — passes --region to modkit; empty = WG
#   BAM_COL           — specimens column holding the BAM path to run modkit on
#                       (default: merged_bam_path). Set to chr17_bam_path to
#                       run against the output of run_subset_chr.sh CHR=chr17.
#   OUT_SUBDIR        — override the results subdirectory name
#                       (default: modkit_merged, or modkit_{BAM_COL stem}).
#   ANALYSIS_NAME     — casetrack analysis name (default: modkit_merged, or
#                       modkit_{BAM_COL stem}).

set -euo pipefail

: "${SPECIMEN_ID:?run_modkit_merged: SPECIMEN_ID is required}"
: "${PROJECT_DIR:?run_modkit_merged: PROJECT_DIR is required}"
: "${REF_FASTA:?run_modkit_merged: REF_FASTA is required}"
: "${DEMO_SCRIPTS_DIR:?run_modkit_merged: DEMO_SCRIPTS_DIR is required}"

CASETRACK_BIN="${CASETRACK_BIN:-casetrack}"
MODKIT_CONTAINER="${MODKIT_CONTAINER:-}"
MOD_BASES="${MOD_BASES:-5mC 5hmC}"
CHR_LIMIT="${CHR_LIMIT:-}"
BAM_COL="${BAM_COL:-merged_bam_path}"

# Derive the default subdir + analysis name from BAM_COL so chr-subset runs
# don't collide with the full-merged run on disk or in the casetrack schema.
# Examples: BAM_COL=merged_bam_path → modkit_merged
#           BAM_COL=chr17_bam_path   → modkit_chr17
_col_stem="${BAM_COL%_bam_path}"
if [[ "${_col_stem}" == "merged" ]]; then
    _default_analysis="modkit_merged"
else
    _default_analysis="modkit_${_col_stem}"
fi
ANALYSIS_NAME="${ANALYSIS_NAME:-${_default_analysis}}"
OUT_SUBDIR="${OUT_SUBDIR:-${ANALYSIS_NAME}}"

STAMP="$(date +%Y%m%d_%H%M%S)"
RESULTS_DIR="${PROJECT_DIR}/results/${OUT_SUBDIR}/${SPECIMEN_ID}"
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${RESULTS_DIR}" "${LOG_DIR}"
LOG="${LOG_DIR}/${ANALYSIS_NAME}_${SPECIMEN_ID}_${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === ${ANALYSIS_NAME} ${SPECIMEN_ID} ==="

# ── Look up the BAM path from the specimen row ────────────────────────────────
BAM_PATH="$("${CASETRACK_BIN}" query --project-dir "${PROJECT_DIR}" --fmt tsv \
    "SELECT ${BAM_COL} FROM specimens WHERE specimen_id = '${SPECIMEN_ID}'" \
    | tail -n +2 | head -1 || true)"
if [[ -z "${BAM_PATH}" || "${BAM_PATH}" == "None" ]]; then
    echo "Error: specimens.${SPECIMEN_ID}.${BAM_COL} is not set — run the upstream analysis first" >&2
    exit 1
fi
echo "BAM_PATH (from specimens.${BAM_COL}): ${BAM_PATH}"
echo "REF_FASTA=${REF_FASTA}"

if [[ -n "${MODKIT_CONTAINER}" ]]; then
    MODKIT="apptainer exec --bind /data1/greenbab ${MODKIT_CONTAINER} modkit"
else
    MODKIT="modkit"
fi

# ── Phase 1: modkit pileup ────────────────────────────────────────────────────
BEDMETHYL="${RESULTS_DIR}/${SPECIMEN_ID}.bedMethyl"
REGION_ARGS=""
[[ -n "${CHR_LIMIT}" ]] && REGION_ARGS="--region ${CHR_LIMIT}"

${MODKIT} pileup \
    "${BAM_PATH}" \
    "${BEDMETHYL}" \
    --reference "${REF_FASTA}" \
    --modified-bases ${MOD_BASES} \
    --cpg \
    ${REGION_ARGS} \
    --threads "${SLURM_CPUS_PER_TASK:-8}"
echo "[Phase 1] modkit pileup → ${BEDMETHYL}"

# ── Phase 2: summarize ────────────────────────────────────────────────────────
# Reuse the giab_chr21 summarizer — it reads specimen_id from --assay-id (we
# rename the column via --key-col in the TSV header).
SUMMARY_TSV="${RESULTS_DIR}/summary.tsv"
python3 - "${BEDMETHYL}" "${SPECIMEN_ID}" "${SUMMARY_TSV}" <<'PY'
import statistics, sys
from pathlib import Path
bed, spec, out = sys.argv[1], sys.argv[2], sys.argv[3]
fracs, n_high = [], 0
for line in Path(bed).read_text().splitlines():
    if not line or line.startswith("#"):
        continue
    parts = line.split("\t")
    if len(parts) < 11:
        continue
    mod, cov, frac = parts[3], parts[9], parts[10]
    if mod != "m":
        continue
    try:
        f = float(frac); c = int(cov)
    except ValueError:
        continue
    fracs.append(f)
    if c >= 5:
        n_high += 1
n = len(fracs)
mean = round(sum(fracs) / (n * 100), 4) if n else 0.0
med = round(statistics.median(fracs) / 100, 4) if n else 0.0
pct_high = round(100.0 * n_high / n, 2) if n else 0.0
with open(out, "w") as f:
    f.write("specimen_id\tn_cpg_sites\tmean_meth\tmedian_meth\tpct_high_conf\n")
    f.write(f"{spec}\t{n}\t{mean}\t{med}\t{pct_high}\n")
print(f"Wrote {out} (n_cpg={n}, mean_meth={mean})")
PY
cat "${SUMMARY_TSV}"

# ── Phase 3: append at specimen level ─────────────────────────────────────────
"${CASETRACK_BIN}" append \
    --project-dir "${PROJECT_DIR}" \
    --level specimen \
    --analysis "${ANALYSIS_NAME}" \
    --results "${SUMMARY_TSV}"
echo "[Phase 3] Appended at specimen level."

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === DONE: modkit_merged ${SPECIMEN_ID} ==="
