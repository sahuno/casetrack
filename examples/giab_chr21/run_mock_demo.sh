#!/usr/bin/env bash
# run_mock_demo.sh — fast, zero-cluster demo of casetrack on the GIAB chr21 cohort.
#
# Bootstraps a v0.3 project from the sample sheet, generates synthetic
# analysis summaries, appends them via casetrack, and emits a browsable
# HTML dashboard. Runs in under a minute on a laptop.
#
# Usage:
#   bash run_mock_demo.sh [PROJECT_DIR]
#
#   PROJECT_DIR defaults to ./giab_demo_project/

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${1:-${HERE}/giab_demo_project}"
SHEET="${HERE}/sample_sheet.tsv"

echo "=== 1. Bootstrap project from $SHEET ==="
python3 "${HERE}/bootstrap.py" \
    --sample-sheet "${SHEET}" \
    --project-dir "${PROJECT_DIR}" \
    --force

# Collect the assay IDs we just registered so the mock summarizers can target them.
ASSAY_IDS="$(awk -F'\t' 'NR>1 && $2 != "" {print $2}' "${SHEET}" | paste -sd,)"
echo "=== assay IDs ==="
echo "$ASSAY_IDS"

TMP="$(mktemp -d)"
trap "rm -rf $TMP" EXIT

echo
echo "=== 2. Synthesize mock analysis summaries ==="
python3 "${HERE}/scripts/mock_modkit_summary.py" \
    --assay-ids "$ASSAY_IDS" --output "$TMP/modkit.tsv"
python3 "${HERE}/scripts/mock_sniffles_summary.py" \
    --assay-ids "$ASSAY_IDS" --output "$TMP/sniffles.tsv"

# Mock flagstat — same shape as the real one; uses hashlib for determinism.
python3 - <<EOF > "$TMP/flagstat.tsv"
import hashlib
cols = ["assay_id", "total_reads", "mapped_reads", "mapped_pct",
        "properly_paired_reads", "duplicates_reads", "supplementary_reads"]
print("\t".join(cols))
for aid in "$ASSAY_IDS".split(","):
    seed = int(hashlib.md5(("flagstat" + aid).encode()).hexdigest()[:8], 16)
    total = 1_500_000 + (seed % 600_000)
    mapped = int(total * (0.96 + (seed % 30) / 1000))
    pp = int(mapped * 0.01)  # ONT → near-zero proper pairs
    dup = int(total * 0.005)
    supp = int(total * 0.04)
    print("\t".join([aid, str(total), str(mapped),
                      f"{100*mapped/total:.2f}", str(pp), str(dup), str(supp)]))
EOF

echo
echo "=== 3. Append each analysis via casetrack ==="
casetrack append --project-dir "${PROJECT_DIR}" --analysis flagstat \
    --results "$TMP/flagstat.tsv"
casetrack append --project-dir "${PROJECT_DIR}" --analysis modkit \
    --results "$TMP/modkit.tsv"
casetrack append --project-dir "${PROJECT_DIR}" --analysis sniffles \
    --results "$TMP/sniffles.tsv"

echo
echo "=== 4. Status ==="
casetrack status --project-dir "${PROJECT_DIR}"

echo
echo "=== 5. Dashboard ==="
casetrack dashboard --project-dir "${PROJECT_DIR}" \
    --output "${PROJECT_DIR}/dashboard.html"

echo
echo "=== 6. Example query ==="
casetrack query --project-dir "${PROJECT_DIR}" \
    "SELECT patient_id, assay_id, mean_meth, n_svs_total
     FROM _ ORDER BY assay_id"

echo
echo "Demo complete. Open ${PROJECT_DIR}/dashboard.html in a browser."
