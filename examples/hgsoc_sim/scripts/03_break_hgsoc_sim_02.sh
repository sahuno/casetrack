#!/bin/bash
# 03_break_hgsoc_sim_02.sh — deliberately truncate HGSOC_SIM_02's normal RNA BAM.
#
# The v0.4 QC workflow needs a motivating "broken" specimen to exercise the
# autoflag + strict-refuse paths. We target the RNA BAM (not DNA) because:
#   1. RNA-Seq libraries fail more often than DNA in practice — polyA capture
#      is finicky, RNA degrades faster, shorter fragments survive prep worse.
#   2. This mirrors proposal 0002 §4.5 exactly: HGSOC002's normal ONT-RNA
#      failed library prep while the normal DNA was fine.
#   3. On the same specimen, two different assays passing/failing is the
#      interesting casetrack QC story — per-assay QC, not per-specimen.
#
# Downsamples HGSOC_SIM_02-normal-ONT-RNA from ~8000 reads to ~250, which
# 04_summarize_mock.py turns into qc_pass=False (MIN_READS=5000).
#
# Author: Samuel Ahuno (ekwame001@gmail.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

SANDBOX="${SANDBOX:-$REPO_ROOT/sandbox/hgsoc_sim}"
TARGET_BAM="$SANDBOX/cohort/HGSOC_SIM_02/normal/ONT-RNA/sim.srt.bam"
BACKUP_BAM="$SANDBOX/cohort/HGSOC_SIM_02/normal/ONT-RNA/sim.srt.full.bam"

if [[ ! -s "$TARGET_BAM" ]]; then
    echo "Error: $TARGET_BAM not found — run 02b_run_nanosim.sh first." >&2
    exit 1
fi

# Keep the full BAM as backup so rerunning downstream steps doesn't require
# re-simulating.
if [[ ! -s "$BACKUP_BAM" ]]; then
    cp "$TARGET_BAM" "$BACKUP_BAM"
fi

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

# Downsample to ~3% of reads → ~240 for an 8000-read source. Seed pinned.
DOWN_FRAC="0.03"
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
