#!/usr/bin/env bash
# run_attach_bams.sh — route each synth_align's bam_path back onto the
# assays table so premerge_flagstat / merge / modkit can read it.
#
# Walks $SANDBOX/synth/<specimen>/<run>/metadata.tsv (written by
# run_synth_align.sh) and runs `casetrack append --analysis attach_bams`
# to set assays.bam_path. Required columns on the TSV:
#
#     assay_id    bam_path
#
# Not parallel-per-assay — appends are cheap and this runs after all
# synths complete.

set -euo pipefail

: "${SANDBOX:?run_attach_bams: SANDBOX required}"
: "${PROJECT_DIR:?run_attach_bams: PROJECT_DIR required}"
: "${CASETRACK_BIN:?run_attach_bams: CASETRACK_BIN required}"

TSV=$(mktemp)

printf "assay_id\tbam_path\n" > "$TSV"

for meta in "$SANDBOX"/synth/*/*/metadata.tsv; do
    [[ -s "$meta" ]] || continue
    tail -n +2 "$meta" >> "$TSV"
done

n=$(($(wc -l < "$TSV") - 1))
if [[ "$n" -le 0 ]]; then
    echo "ERROR: no per-run metadata.tsv files found under $SANDBOX/synth/" >&2
    exit 1
fi
echo "[attach_bams] $n assay_id,bam_path pairs to attach"
cat "$TSV"

"$CASETRACK_BIN" append \
    --project-dir "$PROJECT_DIR" \
    --analysis attach_bams \
    --results "$TSV"

rm -f "$TSV"
echo "[attach_bams] done."
exit 0
