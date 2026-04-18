#!/bin/bash
# 02b_run_nanosim.sh — NanoSim (cDNA) + minimap2 splice pipeline (ONT-RNA).
#
# For each (patient, specimen, ONT-RNA assay) this script:
#   1. Runs NanoSim transcriptome-mode with the per-assay expression.tsv
#      and the R9.4.1 cDNA pre-trained model.
#   2. Concatenates aligned + unaligned NanoSim FASTQs.
#   3. Aligns with minimap2 -ax splice against the sliced multi-contig ref.
#   4. Sorts and indexes with samtools → <ASSAY>/sim.srt.bam.
#
# DNA assays (ONT-DNA) are NOT processed here — they go through
# scripts/02_run_visor.sh.
#
# Requires:
#   - scripts/00b_fetch_gencode.sh (transcripts.fa)
#   - scripts/00c_fetch_nanosim_model.sh (pre-trained model)
#   - scripts/01b_prepare_expression.py (per-assay expression.tsv)
#
# Runner resolution mirrors 02_run_visor.sh:
#   - apptainer + SIFs under $CONTAINER_DIR
#   - RUNNER=docker + docker on PATH
#   - RUNNER=native (nanosim + minimap2 + samtools on PATH)
#
# Author: Samuel Ahuno (ekwame001@gmail.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

SANDBOX="${SANDBOX:-$REPO_ROOT/sandbox/hgsoc_sim}"
COHORT_DIR="$SANDBOX/cohort"
REF_FA="$SANDBOX/ref/ref.fa"
TRANSCRIPTS_FA="$SANDBOX/ref/transcripts.fa"

RNA_ASSAY_TYPE="ONT-RNA"

NANOSIM_MODEL="${NANOSIM_MODEL:-human_NA12878_cDNA_Bham1_guppy}"
MODEL_DIR="$SANDBOX/nanosim_models/$NANOSIM_MODEL"

# Number of threads used for minimap2 / samtools.
THREADS="${THREADS:-4}"

for required in "$REF_FA" "$TRANSCRIPTS_FA"; do
    if [[ ! -s "$required" ]]; then
        echo "Error: required file $required missing — run the 00* scripts." >&2
        exit 1
    fi
done
if [[ ! -d "$MODEL_DIR" ]]; then
    echo "Error: NanoSim model dir $MODEL_DIR missing — run 00c_fetch_nanosim_model.sh." >&2
    exit 1
fi

# NanoSim's -c expects a prefix of the training files. Common naming has
# all files sharing a 'training' prefix; fall back to the model name if
# the model ships with a different convention.
MODEL_PREFIX_CANDIDATE="$MODEL_DIR/training"
if ls "${MODEL_PREFIX_CANDIDATE}"*.pkl 2>/dev/null | grep -q .; then
    NANOSIM_C="$MODEL_PREFIX_CANDIDATE"
else
    # Fallback: pull the longest common prefix of .pkl files in the dir.
    NANOSIM_C="$(python3 -c "
import os
from pathlib import Path
p = Path('$MODEL_DIR')
stems = [f.stem for f in p.glob('*.pkl')]
if not stems:
    print('$MODEL_PREFIX_CANDIDATE')
else:
    def lcp(xs):
        import os.path
        return os.path.commonprefix(xs)
    print(p / lcp(stems).rstrip('_'))
")"
fi
echo "[02b] NanoSim model prefix: $NANOSIM_C"

# ── Runner resolution ────────────────────────────────────────────────────────

CONTAINER_DIR="${CONTAINER_DIR:-$HOME/apps/containers}"
NANOSIM_SIF="$CONTAINER_DIR/nanosim_3.2.3.sif"
MINIMAP_SIF="$CONTAINER_DIR/minimap2_2.28.sif"
SAMTOOLS_SIF="$CONTAINER_DIR/samtools_1.21.sif"

RUNNER="${RUNNER:-auto}"
_resolve_runner() {
    if [[ "$RUNNER" == "auto" ]]; then
        if [[ -s "$NANOSIM_SIF" && -s "$MINIMAP_SIF" && -s "$SAMTOOLS_SIF" ]]; then
            RUNNER=apptainer
        elif command -v docker >/dev/null 2>&1; then
            RUNNER=docker
        elif command -v simulator.py >/dev/null 2>&1 \
             && command -v minimap2 >/dev/null 2>&1 \
             && command -v samtools >/dev/null 2>&1; then
            RUNNER=native
        else
            echo "Error: no viable runner for nanosim + minimap2 + samtools." >&2
            echo "       See containers/README.md." >&2
            exit 1
        fi
    fi
    echo "[02b] runner = $RUNNER"
}

_bind_args=(--bind "$SANDBOX" --bind "$REPO_ROOT")

nanosim_simulator() {
    # NanoSim's v3+ entry point is `simulator.py`. Inside the biocontainer it's
    # on PATH as `simulator.py`.
    case "$RUNNER" in
        apptainer) apptainer exec "${_bind_args[@]}" "$NANOSIM_SIF" simulator.py "$@" ;;
        docker)    docker run --rm -v "$SANDBOX":"$SANDBOX" -v "$REPO_ROOT":"$REPO_ROOT" -w "$PWD" \
                     quay.io/biocontainers/nanosim:3.2.3--hdfd78af_2 simulator.py "$@" ;;
        native)    simulator.py "$@" ;;
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

# ── Iterate over ONT-RNA assays ──────────────────────────────────────────────

shopt -s nullglob
patients=("$COHORT_DIR"/*)
if [[ ${#patients[@]} -eq 0 ]]; then
    echo "Error: no patients under $COHORT_DIR." >&2
    exit 1
fi

for patient_dir in "${patients[@]}"; do
    patient=$(basename "$patient_dir")
    for spec_dir in "$patient_dir"/*/; do
        specimen=$(basename "$spec_dir")
        rna_dir="$spec_dir/$RNA_ASSAY_TYPE"
        if [[ ! -d "$rna_dir" ]]; then
            continue
        fi
        expression_tsv="$rna_dir/expression.tsv"
        if [[ ! -s "$expression_tsv" ]]; then
            echo "Warning: $expression_tsv missing — run 01b_prepare_expression.py." >&2
            continue
        fi
        out_bam="$rna_dir/sim.srt.bam"
        if [[ -s "$out_bam" ]]; then
            echo "[02b] $patient/$specimen/$RNA_ASSAY_TYPE — already simulated, skipping"
            continue
        fi

        # Target read count = sum of est_counts column.
        target_n=$(awk -F'\t' 'NR > 1 {s += $2} END {print s}' "$expression_tsv")
        echo "[02b] $patient/$specimen/$RNA_ASSAY_TYPE — target ${target_n} reads"

        sim_prefix="$rna_dir/sim"

        # ── Step 1: NanoSim transcriptome mode ─────────────────────────────
        # --no_model_ir drops intron-retention simulation; keeps runtime down
        # and avoids requiring the full genome reference.
        # -b guppy hints the basecaller family (our model was basecalled with guppy).
        echo "[02b] $patient/$specimen/$RNA_ASSAY_TYPE — NanoSim"
        rm -f "${sim_prefix}_aligned_reads.fastq" "${sim_prefix}_unaligned_reads.fastq"
        nanosim_simulator transcriptome \
            -rt "$TRANSCRIPTS_FA" \
            -e "$expression_tsv" \
            -c "$NANOSIM_C" \
            -o "$sim_prefix" \
            -n "$target_n" \
            -b guppy \
            --fastq \
            --no_model_ir \
            -t "$THREADS"

        # Concatenate aligned + unaligned reads for minimap2 input.
        reads_fq="$rna_dir/reads.fastq"
        : > "$reads_fq"
        for suffix in _aligned_reads.fastq _unaligned_reads.fastq; do
            if [[ -s "${sim_prefix}${suffix}" ]]; then
                cat "${sim_prefix}${suffix}" >> "$reads_fq"
            fi
        done
        if [[ ! -s "$reads_fq" ]]; then
            echo "Error: NanoSim produced no reads for $patient/$specimen/$RNA_ASSAY_TYPE" >&2
            exit 1
        fi

        # ── Step 2: minimap2 splice-aware alignment ────────────────────────
        # -uf: forward-strand cDNA model (standard for stranded sequencing)
        # -k14: smaller k-mer helps with ONT RNA's higher error rate
        # --MD + -Y: downstream-caller-friendly (MD tag, no hard clip)
        echo "[02b] $patient/$specimen/$RNA_ASSAY_TYPE — minimap2 splice + samtools sort"
        minimap2 -ax splice -uf -k14 -t "$THREADS" --MD -Y "$REF_FA" "$reads_fq" 2>/dev/null | \
            samtools sort -@ "$THREADS" -o "$out_bam" -
        samtools index "$out_bam"

        # Keep the reads FASTQ around for debugging but compress it.
        gzip -f "$reads_fq"

        echo "[02b] $patient/$specimen/$RNA_ASSAY_TYPE → $out_bam"
    done
done

echo "[02b] all ${RNA_ASSAY_TYPE} assays simulated."
