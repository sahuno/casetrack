#!/usr/bin/env bash
# run_summary.sh — final phase: cohort readiness, dashboard, benchmarks.
# Launched by submit_pipeline.sh phase_summary as a single wrap-up job
# after every upstream phase completes.

set -euo pipefail

: "${PROJECT_DIR:?run_summary: PROJECT_DIR required}"
: "${SANDBOX:?run_summary: SANDBOX required}"
: "${CASETRACK_BIN:?run_summary: CASETRACK_BIN required}"
: "${HPC_DIR:?run_summary: HPC_DIR required}"

mkdir -p "$HPC_DIR/benchmarks"

echo "=============================================="
echo "== casetrack status --usable"
echo "=============================================="
"$CASETRACK_BIN" status --project-dir "$PROJECT_DIR" --usable

echo
echo "=============================================="
echo "== cohort --pair-by tissue_site --assay-type ONT"
echo "=============================================="
"$CASETRACK_BIN" cohort --project-dir "$PROJECT_DIR" \
    --pair-by tissue_site \
    --partition-order tumor,normal \
    --assay-type ONT

echo
echo "=============================================="
echo "== cohort --pair-by tissue_site --assay-type scRNA"
echo "=============================================="
"$CASETRACK_BIN" cohort --project-dir "$PROJECT_DIR" \
    --pair-by tissue_site \
    --partition-order tumor,normal \
    --assay-type scRNA

echo
echo "=============================================="
echo "== qc-history (active events only)"
echo "=============================================="
"$CASETRACK_BIN" qc-history --project-dir "$PROJECT_DIR"

echo
echo "=============================================="
echo "== dashboard"
echo "=============================================="
"$CASETRACK_BIN" dashboard --project-dir "$PROJECT_DIR" \
    --output "$HPC_DIR/benchmarks/dashboard.html"
ls -lh "$HPC_DIR/benchmarks/dashboard.html"

echo
echo "=============================================="
echo "== benchmark report"
echo "=============================================="
python3 "$HPC_DIR/scripts/bench_report.py" \
    --project-dir "$PROJECT_DIR" \
    --output "$HPC_DIR/benchmarks/run_$(date +%Y%m%d_%H%M%S).md"

echo
echo "[$(date '+%F %T')] summary phase done."
