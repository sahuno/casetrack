#!/bin/bash
# 03_break_hgsoc_sim_02.sh — deliberately truncate HGSOC_SIM_02's normal BAM.
#
# The v0.4 QC workflow needs a motivating "broken" specimen to exercise the
# autoflag + strict-refuse paths. We downsample the normal BAM from ~25x to
# ~2x; the summarizer (04_summarize_mock.py) turns anything under a coverage
# threshold into qc_pass=False, which casetrack append converts into a
# qc_events row.
#
# This is the pipeline analogue of HGSOC002's failed library prep in
# proposal 0002 §4.5 — the failure surfaces at summarize time, which is
# exactly where real pipelines notice it.
#
# Author: Samuel Ahuno (ekwame001@gmail.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

SANDBOX="${SANDBOX:-$REPO_ROOT/sandbox/hgsoc_sim}"
TARGET_BAM="$SANDBOX/cohort/HGSOC_SIM_02/normal/laser/sim.srt.bam"
BACKUP_BAM="$SANDBOX/cohort/HGSOC_SIM_02/normal/laser/sim.srt.full.bam"

if [[ ! -s "$TARGET_BAM" ]]; then
    echo "Error: $TARGET_BAM not found — run 02_run_visor.sh first." >&2
    exit 1
fi

# Keep the full BAM as a sibling in case someone wants to re-run downstream
# scripts without rebuilding from scratch. Idempotent: if the backup already
# exists we treat it as the source of truth.
if [[ ! -s "$BACKUP_BAM" ]]; then
    cp "$TARGET_BAM" "$BACKUP_BAM"
fi

# ── Runner resolution for samtools (mirrors 02_run_visor.sh) ──────────────────

CONTAINER_DIR="${CONTAINER_DIR:-$HOME/apps/containers}"
SAMTOOLS_SIF="$CONTAINER_DIR/samtools_1.21.sif"

samtools_run() {
    if command -v samtools >/dev/null 2>&1; then
        samtools "$@"
    elif [[ -s "$SAMTOOLS_SIF" ]]; then
        apptainer exec --bind "$SANDBOX" "$SAMTOOLS_SIF" samtools "$@"
    else
        echo "Error: need samtools on PATH OR SIF at $SAMTOOLS_SIF" >&2
        exit 1
    fi
}

# Downsample to ~8% of reads → ~2× for a 25× source. Seed pinned for
# reproducibility.
DOWN_FRAC="0.08"
SEED="42"

echo "[03] downsampling $TARGET_BAM to fraction=$DOWN_FRAC (seed=$SEED)"
TMP_BAM="$TARGET_BAM.tmp.bam"
samtools_run view -bh -s "${SEED}.${DOWN_FRAC#0.}" "$BACKUP_BAM" -o "$TMP_BAM"
mv "$TMP_BAM" "$TARGET_BAM"
samtools_run index "$TARGET_BAM"

printf "[03] truncated → "
samtools_run view -c "$TARGET_BAM"
printf "[03] (backup kept at $BACKUP_BAM with "
samtools_run view -c "$BACKUP_BAM"
printf " reads)\n"
