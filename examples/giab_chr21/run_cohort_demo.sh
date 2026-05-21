#!/usr/bin/env bash
# run_cohort_demo.sh — demo of casetrack COHORT-LEVEL artifacts (proposal 0009).
#
# A cohort artifact is ONE output derived from MANY assays — here a joint-
# genotyped multi-sample VCF over the 4-assay GIAB chr21 cohort. The demo shows
# the three-phase cohort pattern (run → summarize → append-cohort) and the
# punchline: censoring ONE input assay flips the cohort artifact to STALE,
# because staleness cascades from the QC state of its inputs — something a flat
# sample sheet cannot express.
#
# Two engines, both cheap:
#   --engine mock      (default) zero-compute; fakes the joint VCF + stats.
#                      Runs anywhere in ~15 s, no external tools.
#   --engine bcftools  real `bcftools merge` of tiny per-sample chr21 VCFs into
#                      a real multi-sample VCF. Needs bcftools + bgzip + tabix
#                      on PATH (or run inside an htslib container). Seconds.
#
# Usage:
#   bash run_cohort_demo.sh [--engine mock|bcftools] [PROJECT_DIR]
#
#   PROJECT_DIR defaults to ./giab_cohort_project/

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="mock"
PROJECT_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --engine) ENGINE="$2"; shift 2 ;;
        *)        PROJECT_DIR="$1"; shift ;;
    esac
done
PROJECT_DIR="${PROJECT_DIR:-${HERE}/giab_cohort_project}"
SHEET="${HERE}/sample_sheet.tsv"
RUN_TAG="$(date +%Y%m%d)_hg38_chr21_joint"

echo "=== 1. Bootstrap project from $SHEET ==="
python3 "${HERE}/bootstrap.py" \
    --sample-sheet "${SHEET}" \
    --project-dir "${PROJECT_DIR}" \
    --force

# sample_id (col 2) is the assay_id in this cohort.
mapfile -t ASSAYS < <(awk -F'\t' 'NR>1 && $2 != "" {print $2}' "${SHEET}")
echo "=== assays (${#ASSAYS[@]}): ${ASSAYS[*]} ==="

WORK="$(mktemp -d)"
trap "rm -rf $WORK" EXIT

ARTIFACT="${PROJECT_DIR}/results/joint_genotype/${RUN_TAG}/cohort.chr21.vcf.gz"
mkdir -p "$(dirname "$ARTIFACT")"
INPUTS="${WORK}/inputs.txt"
printf '%s\n' "${ASSAYS[@]}" > "${INPUTS}"
STATS="${WORK}/stats.json"

echo
echo "=== 2. Joint genotyping (engine=${ENGINE}) → one cohort VCF ==="
if [[ "$ENGINE" == "bcftools" ]]; then
    for tool in bcftools bgzip tabix; do
        command -v "$tool" >/dev/null 2>&1 || {
            echo "ERROR: '$tool' not on PATH. Either load an htslib container" >&2
            echo "       (apptainer exec <htslib.sif> bash run_cohort_demo.sh ...)" >&2
            echo "       or use the default --engine mock." >&2
            exit 1
        }
    done
    # One tiny valid per-sample chr21 VCF each, then bcftools merge → cohort VCF.
    MERGE_INPUTS=()
    i=0
    for a in "${ASSAYS[@]}"; do
        vcf="${WORK}/${a}.vcf"
        cat > "$vcf" <<EOF
##fileformat=VCFv4.2
##contig=<ID=chr21,length=46709983>
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
#CHROM	POS	ID	REF	ALT	QUAL	FILTER	INFO	FORMAT	${a}
chr21	5030000	.	A	G	50	PASS	.	GT	0/1
chr21	5040000	.	C	T	50	PASS	.	GT	$([[ $((i % 2)) -eq 0 ]] && echo "1/1" || echo "0/1")
EOF
        bgzip -f "$vcf"
        tabix -f -p vcf "${vcf}.gz"
        MERGE_INPUTS+=("${vcf}.gz")
        i=$((i + 1))
    done
    bcftools merge -O z -o "$ARTIFACT" "${MERGE_INPUTS[@]}"
    tabix -f -p vcf "$ARTIFACT"
    N_VARIANTS="$(bcftools stats "$ARTIFACT" | awk -F'\t' '/^SN/ && /number of records:/ {print $4}')"
    N_SAMPLES="$(bcftools query -l "$ARTIFACT" | wc -l | tr -d ' ')"
    printf '{"engine":"bcftools","n_variants":%s,"n_samples":%s}\n' \
        "${N_VARIANTS:-0}" "${N_SAMPLES:-0}" > "${STATS}"
else
    # Mock: deterministic fake artifact + stats; no real tools.
    {
        echo "##fileformat=VCFv4.2 (MOCK joint call over ${#ASSAYS[@]} assays)"
        printf '#cohort\t%s\n' "$(IFS=,; echo "${ASSAYS[*]}")"
    } | gzip -c > "$ARTIFACT"
    python3 - "$STATS" "${#ASSAYS[@]}" <<'EOF'
import hashlib, json, sys
stats_path, n = sys.argv[1], int(sys.argv[2])
seed = int(hashlib.md5(b"joint_chr21").hexdigest()[:8], 16)
json.dump({"engine": "mock",
           "n_variants": 18000 + seed % 4000,
           "n_samples": n,
           "ti_tv": round(2.0 + (seed % 20) / 100, 2)},
          open(stats_path, "w"))
EOF
fi
echo "  artifact: $ARTIFACT"
echo "  stats:    $(cat "$STATS")"

echo
echo "=== 3. Register the cohort artifact + its assay lineage ==="
casetrack append-cohort --project-dir "${PROJECT_DIR}" \
    --analysis joint_genotype --run-tag "${RUN_TAG}" \
    --path "${ARTIFACT}" --inputs-from "${INPUTS}" --stats "${STATS}"

echo
echo "=== 4. cohort-artifacts — fresh (all inputs pass) ==="
casetrack cohort-artifacts --project-dir "${PROJECT_DIR}"

echo
echo "=== 5. Censor ONE contributing assay (simulate a QC failure) ==="
VICTIM="${ASSAYS[0]}"
casetrack censor --project-dir "${PROJECT_DIR}" \
    --level assay --id "${VICTIM}" --kind qc_fail \
    --reason "demo: ${VICTIM} flunked coverage QC after the joint call"

echo
echo "=== 6. cohort-artifacts — now STALE (an input is censored) ==="
casetrack cohort-artifacts --project-dir "${PROJECT_DIR}"

echo
echo "Punchline: the joint VCF didn't change on disk, but casetrack now flags it"
echo "STALE because ${VICTIM} — one of its inputs — is censored. Re-genotype with"
echo "a new --run-tag to supersede it; the old artifact stays in the audit trail."
echo
echo "Demo complete. Project: ${PROJECT_DIR}"
