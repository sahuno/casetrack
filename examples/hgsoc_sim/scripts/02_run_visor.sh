#!/bin/bash
# 02_run_visor.sh — run VISOR HACk + LASeR for every simulated specimen.
#
# Each (patient, specimen) gets:
#   sandbox/hgsoc_sim/cohort/<PATIENT>/<SPECIMEN>/hack/     — haplotype FASTAs
#   sandbox/hgsoc_sim/cohort/<PATIENT>/<SPECIMEN>/laser/    — sim.srt.bam (+ .bai)
#
# The cohort list is driven by the directory structure emitted by
# 01_prepare_visor_beds.py — no YAML parsing here.
#
# Author: Samuel Ahuno (ekwame001@gmail.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

SANDBOX="${SANDBOX:-$REPO_ROOT/sandbox/hgsoc_sim}"
COHORT_DIR="$SANDBOX/cohort"
REF_FA="$SANDBOX/ref/chr17_brca1.fa"

if [[ ! -s "$REF_FA" ]]; then
    echo "Error: reference slice not found at $REF_FA" >&2
    echo "       Run scripts/00_fetch_reference.sh first." >&2
    exit 1
fi

if [[ ! -d "$COHORT_DIR" ]]; then
    echo "Error: cohort dir not found at $COHORT_DIR" >&2
    echo "       Run scripts/01_prepare_visor_beds.py first." >&2
    exit 1
fi

# ── Resolve the VISOR runner (apptainer ⋄ docker ⋄ native) ────────────────────

CONTAINER_DIR="${CONTAINER_DIR:-$HOME/apps/containers}"
VISOR_SIF="$CONTAINER_DIR/visor_1.1.2.1.sif"
RUNNER="${RUNNER:-auto}"

resolve_runner() {
    if [[ "$RUNNER" == "auto" ]]; then
        if [[ -s "$VISOR_SIF" ]]; then
            RUNNER=apptainer
        elif command -v docker >/dev/null 2>&1; then
            RUNNER=docker
        elif command -v VISOR >/dev/null 2>&1; then
            RUNNER=native
        else
            echo "Error: no VISOR runner available." >&2
            echo "       Options:" >&2
            echo "       - apptainer: pull $VISOR_SIF per containers/README.md" >&2
            echo "       - docker:    RUNNER=docker and have docker on PATH" >&2
            echo "       - native:    pip install visor" >&2
            exit 1
        fi
    fi
    echo "[02] runner = $RUNNER"
}

visor() {
    # Invoke VISOR with the same args regardless of runner.
    case "$RUNNER" in
        apptainer)
            apptainer exec --bind "$SANDBOX" --bind "$REPO_ROOT" \
                "$VISOR_SIF" VISOR "$@"
            ;;
        docker)
            docker run --rm \
                -v "$SANDBOX":"$SANDBOX" \
                -v "$REPO_ROOT":"$REPO_ROOT" \
                -w "$PWD" \
                quay.io/biocontainers/visor:1.1.2.1--pyh7cba7a3_0 \
                VISOR "$@"
            ;;
        native)
            VISOR "$@"
            ;;
    esac
}

resolve_runner

# ── Iterate: one HACk + one LASeR per specimen ───────────────────────────────

shopt -s nullglob
patients=("$COHORT_DIR"/*)
if [[ ${#patients[@]} -eq 0 ]]; then
    echo "Error: no patients under $COHORT_DIR — did 01_prepare_visor_beds.py run?" >&2
    exit 1
fi

for patient_dir in "${patients[@]}"; do
    patient=$(basename "$patient_dir")
    specimens=("$patient_dir"/*/)
    for spec_dir in "${specimens[@]}"; do
        specimen=$(basename "$spec_dir")
        h1_bed="$spec_dir/haplotype1.hack.bed"
        h2_bed="$spec_dir/haplotype2.hack.bed"
        laser_bed="$spec_dir/regions.laser.bed"

        hack_out="$spec_dir/hack"
        laser_out="$spec_dir/laser"

        if [[ -s "$laser_out/sim.srt.bam" ]]; then
            echo "[02] $patient/$specimen already simulated — skipping"
            continue
        fi

        echo "[02] $patient/$specimen — HACk"
        rm -rf "$hack_out"
        visor HACk \
            -b "$h1_bed" "$h2_bed" \
            -g "$REF_FA" \
            -o "$hack_out"

        echo "[02] $patient/$specimen — LASeR"
        rm -rf "$laser_out"
        # --noaddtag avoids haplotype-tagged bams which confuse generic callers.
        # --threads kept modest (4) so the demo fits a login node / dev laptop.
        visor LASeR \
            -s "$hack_out" \
            -b "$laser_bed" \
            -g "$REF_FA" \
            -o "$laser_out" \
            --threads 4

        echo "[02] $patient/$specimen → $laser_out/sim.srt.bam"
    done
done

echo "[02] all specimens simulated under $COHORT_DIR"
