#!/bin/bash
# 02_run_visor.sh — VISOR HACk + Badread (R10.4.1) + minimap2 pipeline (ONT-DNA).
#
# For each (patient, specimen, ONT-DNA assay) this script:
#   1. Runs VISOR HACk to turn the variant BEDs into two haplotype FASTAs.
#   2. Runs Badread per haplotype + (if purity < 100) per-reference slice,
#      with per-source read budgets weighted so the mix reproduces the
#      requested coverage and tumor purity.
#   3. Aligns the merged reads with minimap2 (map-ont preset).
#   4. Sorts and indexes with samtools → sandbox/.../<ASSAY>/sim.srt.bam.
#
# RNA assays (ONT-RNA) are NOT processed here — they have their own
# pipeline in scripts/02b_run_nanosim.sh (phase f).
#
# Requires one of:
#   - apptainer + the four SIFs under $CONTAINER_DIR (see containers/README.md)
#   - docker on PATH + RUNNER=docker
#   - native VISOR + badread + minimap2 + samtools on PATH (RUNNER=native)
#
# Author: Samuel Ahuno (ekwame001@gmail.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

SANDBOX="${SANDBOX:-$REPO_ROOT/sandbox/hgsoc_sim}"
COHORT_DIR="$SANDBOX/cohort"
REF_FA="$SANDBOX/ref/ref.fa"

DNA_ASSAY_TYPE="ONT-DNA"

if [[ ! -s "$REF_FA" ]]; then
    echo "Error: reference FASTA not found at $REF_FA" >&2
    echo "       Run scripts/00_fetch_reference.sh first." >&2
    exit 1
fi
if [[ ! -d "$COHORT_DIR" ]]; then
    echo "Error: cohort dir not found at $COHORT_DIR" >&2
    echo "       Run scripts/01_prepare_visor_beds.py first." >&2
    exit 1
fi

# ── Resolve runners for each tool ─────────────────────────────────────────────

CONTAINER_DIR="${CONTAINER_DIR:-$HOME/apps/containers}"
VISOR_SIF="$CONTAINER_DIR/visor_1.1.2.1.sif"
BADREAD_SIF="$CONTAINER_DIR/badread_0.4.1.sif"
MINIMAP_SIF="$CONTAINER_DIR/minimap2_2.28.sif"
SAMTOOLS_SIF="$CONTAINER_DIR/samtools_1.21.sif"

RUNNER="${RUNNER:-auto}"

_resolve_runner() {
    if [[ "$RUNNER" == "auto" ]]; then
        if [[ -s "$VISOR_SIF" && -s "$BADREAD_SIF" && -s "$MINIMAP_SIF" && -s "$SAMTOOLS_SIF" ]]; then
            RUNNER=apptainer
        elif command -v docker >/dev/null 2>&1; then
            RUNNER=docker
        elif command -v VISOR >/dev/null 2>&1 \
             && command -v badread >/dev/null 2>&1 \
             && command -v minimap2 >/dev/null 2>&1 \
             && command -v samtools >/dev/null 2>&1; then
            RUNNER=native
        else
            echo "Error: no viable runner for VISOR + badread + minimap2 + samtools." >&2
            echo "       See containers/README.md for pull commands." >&2
            exit 1
        fi
    fi
    echo "[02] runner = $RUNNER"
}

_bind_args=(--bind "$SANDBOX" --bind "$REPO_ROOT")

visor() {
    case "$RUNNER" in
        apptainer) apptainer exec "${_bind_args[@]}" "$VISOR_SIF" VISOR "$@" ;;
        docker)    docker run --rm -v "$SANDBOX":"$SANDBOX" -v "$REPO_ROOT":"$REPO_ROOT" -w "$PWD" \
                     quay.io/biocontainers/visor:1.1.2.1--pyh7cba7a3_0 VISOR "$@" ;;
        native)    VISOR "$@" ;;
    esac
}

badread() {
    case "$RUNNER" in
        apptainer) apptainer exec "${_bind_args[@]}" "$BADREAD_SIF" badread "$@" ;;
        docker)    docker run --rm -v "$SANDBOX":"$SANDBOX" -v "$REPO_ROOT":"$REPO_ROOT" -w "$PWD" \
                     quay.io/biocontainers/badread:0.4.1--pyhdfd78af_0 badread "$@" ;;
        native)    badread "$@" ;;
    esac
}

minimap2() {
    case "$RUNNER" in
        apptainer) apptainer exec "${_bind_args[@]}" "$MINIMAP_SIF" minimap2 "$@" ;;
        docker)    docker run --rm -v "$SANDBOX":"$SANDBOX" -v "$REPO_ROOT":"$REPO_ROOT" -w "$PWD" \
                     quay.io/biocontainers/minimap2:2.28--he4a0461_0 minimap2 "$@" ;;
        native)    minimap2 "$@" ;;
    esac
}

samtools() {
    case "$RUNNER" in
        apptainer) apptainer exec "${_bind_args[@]}" "$SAMTOOLS_SIF" samtools "$@" ;;
        docker)    docker run --rm -v "$SANDBOX":"$SANDBOX" -v "$REPO_ROOT":"$REPO_ROOT" -w "$PWD" \
                     quay.io/biocontainers/samtools:1.21--h50ea8bc_0 samtools "$@" ;;
        native)    samtools "$@" ;;
    esac
}

_resolve_runner

# ── Helper: compute --quantity (bp) from coverage×haplotype-length ────────────

_quantity_bp() {
    local cov="$1"
    local fasta="$2"
    python3 -c "
import pathlib
p = pathlib.Path('$fasta')
total = 0
for line in p.read_text().splitlines():
    if line.startswith('>'):
        continue
    total += len(line.strip())
print(int(total * $cov))
"
}

# ── Iterate DNA assays ──────────────────────────────────────────────────────

shopt -s nullglob
patients=("$COHORT_DIR"/*)
if [[ ${#patients[@]} -eq 0 ]]; then
    echo "Error: no patients under $COHORT_DIR — did 01_prepare_visor_beds.py run?" >&2
    exit 1
fi

for patient_dir in "${patients[@]}"; do
    patient=$(basename "$patient_dir")
    for spec_dir in "$patient_dir"/*/; do
        specimen=$(basename "$spec_dir")
        dna_dir="$spec_dir/$DNA_ASSAY_TYPE"
        if [[ ! -d "$dna_dir" ]]; then
            # This specimen doesn't have a DNA assay (rare) — skip quietly.
            continue
        fi

        h1_bed="$dna_dir/haplotype1.hack.bed"
        h2_bed="$dna_dir/haplotype2.hack.bed"
        laser_bed="$dna_dir/regions.laser.bed"

        hack_out="$dna_dir/hack"
        reads_dir="$dna_dir/reads"
        out_bam="$dna_dir/sim.srt.bam"

        if [[ -s "$out_bam" ]]; then
            echo "[02] $patient/$specimen/$DNA_ASSAY_TYPE — already simulated, skipping"
            continue
        fi

        mkdir -p "$reads_dir"
        rm -rf "$hack_out"

        # ── Step 1: VISOR HACk → haplotype FASTAs ──────────────────────────
        echo "[02] $patient/$specimen/$DNA_ASSAY_TYPE — HACk"
        visor HACk \
            -b "$h1_bed" "$h2_bed" \
            -g "$REF_FA" \
            -o "$hack_out"

        # ── Step 2: pull coverage + purity from the LASeR BED ──────────────
        read -r _slice _start _end coverage purity < <(awk 'NR==1' "$laser_bed")

        # ── Step 3: Badread on each haplotype ──────────────────────────────
        tumor_frac=$(python3 -c "print(float($purity) / 100.0)")
        per_hap_cov=$(python3 -c "print(round(float($coverage) * $tumor_frac / 2, 3))")

        for hap in h1 h2; do
            hap_fa="$hack_out/$hap.fa"
            out_fq="$reads_dir/${hap}.fq.gz"
            if [[ ! -s "$hap_fa" ]]; then
                echo "Warning: $hap_fa missing — HACk didn't emit it?" >&2
                continue
            fi
            q_bp=$(_quantity_bp "$per_hap_cov" "$hap_fa")
            echo "[02] $patient/$specimen/$DNA_ASSAY_TYPE — badread $hap (≈${per_hap_cov}x, ${q_bp} bp)"
            badread simulate \
                --reference "$hap_fa" \
                --quantity "${q_bp}" \
                --error_model nanopore2023 \
                --qscore_model nanopore2023 \
                2>/dev/null \
                | gzip > "$out_fq"
        done

        # ── Step 4: normal-contamination reads from the raw reference ──────
        if (( $(python3 -c "print(int($purity < 100))") )); then
            normal_cov=$(python3 -c "print(round(float($coverage) * (1.0 - float($purity)/100.0), 3))")
            q_bp=$(_quantity_bp "$normal_cov" "$REF_FA")
            out_fq="$reads_dir/normal_contam.fq.gz"
            echo "[02] $patient/$specimen/$DNA_ASSAY_TYPE — badread normal contam (≈${normal_cov}x, ${q_bp} bp)"
            badread simulate \
                --reference "$REF_FA" \
                --quantity "${q_bp}" \
                --error_model nanopore2023 \
                --qscore_model nanopore2023 \
                2>/dev/null \
                | gzip > "$out_fq"
        fi

        # ── Step 5: align with minimap2 and sort with samtools ────────────
        echo "[02] $patient/$specimen/$DNA_ASSAY_TYPE — minimap2 + samtools"
        zcat "$reads_dir"/*.fq.gz | \
            minimap2 -ax map-ont -t 4 --MD -Y "$REF_FA" - 2>/dev/null | \
            samtools sort -@ 4 -o "$out_bam" -
        samtools index "$out_bam"

        echo "[02] $patient/$specimen/$DNA_ASSAY_TYPE → $out_bam"
    done
done

echo "[02] all ${DNA_ASSAY_TYPE} assays simulated under $COHORT_DIR"
