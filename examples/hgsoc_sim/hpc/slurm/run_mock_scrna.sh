#!/usr/bin/env bash
#SBATCH --job-name=hgsoc_mock_scrna
#SBATCH --account=greenbab
#SBATCH --partition=componc_cpu
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=00:10:00
#SBATCH --output=logs/mock_scrna_%A_%a.out
#SBATCH --error=logs/mock_scrna_%A_%a.err
#
# run_mock_scrna.sh — per-assay mock scRNA summary + casetrack append with
# --column-prefix scrna, so the 10x-Chromium-style metrics land as
# scrna_n_cells, scrna_pct_mito, etc. on the assays table and can't
# collide with any other analysis.
#
# Required env:
#   ASSAY_ID       — e.g. HGSOC_SIM_01_tumor-scRNA-RNA-R01
#   PROJECT_DIR    — casetrack project
#   SCRIPTS_DIR    — directory containing mock_scrna_summary.py
#
# Optional:
#   CASETRACK_BIN  — default: casetrack on PATH
#   OUT_DIR        — default: $PROJECT_DIR/results/mock_scrna/$ASSAY_ID

set -euo pipefail

: "${ASSAY_ID:?run_mock_scrna: ASSAY_ID required}"
: "${PROJECT_DIR:?run_mock_scrna: PROJECT_DIR required}"
: "${SCRIPTS_DIR:?run_mock_scrna: SCRIPTS_DIR required}"

CASETRACK_BIN="${CASETRACK_BIN:-casetrack}"
OUT_DIR="${OUT_DIR:-$PROJECT_DIR/results/mock_scrna/$ASSAY_ID}"
mkdir -p "$OUT_DIR" "$PROJECT_DIR/logs"

SUMMARY_TSV="$OUT_DIR/summary.tsv"
python3 "$SCRIPTS_DIR/mock_scrna_summary.py" \
    --assay-id "$ASSAY_ID" \
    --output "$SUMMARY_TSV"

# Column prefix `scrna_` on every analysis column — the v0.4.1 flag
# keeps mock scRNA metrics from ever colliding with other analyses on
# the assays table.
"$CASETRACK_BIN" append \
    --project-dir "$PROJECT_DIR" \
    --analysis mock_scrna \
    --column-prefix scrna \
    --results "$SUMMARY_TSV"

echo "[$(date '+%F %T')] appended mock_scrna for $ASSAY_ID"
