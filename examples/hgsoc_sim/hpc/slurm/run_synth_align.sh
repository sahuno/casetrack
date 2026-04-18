#!/usr/bin/env bash
#SBATCH --job-name=hgsoc_synth_align
#SBATCH --account=greenbab
#SBATCH --partition=componc_cpu
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=02:00:00
#SBATCH --output=logs/synth_align_%A_%a.out
#SBATCH --error=logs/synth_align_%A_%a.err
#
# run_synth_align.sh — per-(specimen × flowcell-run) synthesis + alignment.
#
# Phases:
#   1. VISOR HACk — build two haplotype FASTAs from the specimen's BED
#   2. Badread    — R10.4.1 long reads from each haplotype + (if purity<100)
#                   reference reads for normal contamination
#   3. minimap2   — map-ont against the reference slice
#   4. samtools   — sort + index, output at SYNTH_DIR/sim.srt.bam
#
# Not a casetrack-aware wrapper — that happens later when flagstat/merge/
# modkit land under examples/patterns/premerge_runs/. This job only
# produces a sorted+indexed BAM at a known path.
#
# Required env:
#   PATIENT                  — e.g. HGSOC_SIM_01
#   SPECIMEN                 — e.g. HGSOC_SIM_01_tumor
#   RUN_ID                   — R01 / R02 / ...
#   COVERAGE                 — per-run target coverage (e.g. 17 for half of 35x)
#   PURITY                   — tumor purity, 0-100
#   SEED                     — Badread seed (distinct per run for divergent reads)
#   SANDBOX                  — HPC sandbox root
#   HPC_CONFIG               — path to hpc/config.yaml (for container paths)
#   PARENT_CONFIG            — path to parent's config.yaml (for cohort + ref slices)
#   COHORT_DIR_FOR_SPECIMEN  — path to prepared VISOR BEDs for this specimen
#
# Optional env:
#   NO_PURITY_MIX            — if "true", skip normal-contamination reads (pure haplotype)
#
# Produces:
#   $SANDBOX/synth/$SPECIMEN/$RUN_ID/sim.srt.bam      (+ .bai)
#   $SANDBOX/synth/$SPECIMEN/$RUN_ID/metadata.tsv     (assay_id, bam_path, flagstat summary)
#   $SANDBOX/synth/$SPECIMEN/$RUN_ID/haplotype{1,2}.fa
#   $SANDBOX/synth/$SPECIMEN/$RUN_ID/reads.fastq.gz

set -euo pipefail

: "${PATIENT:?run_synth_align: PATIENT required}"
: "${SPECIMEN:?run_synth_align: SPECIMEN required}"
: "${RUN_ID:?run_synth_align: RUN_ID required}"
: "${COVERAGE:?run_synth_align: COVERAGE required}"
: "${PURITY:?run_synth_align: PURITY required}"
: "${SEED:?run_synth_align: SEED required}"
: "${SANDBOX:?run_synth_align: SANDBOX required}"
: "${HPC_CONFIG:?run_synth_align: HPC_CONFIG required}"
: "${COHORT_DIR_FOR_SPECIMEN:?run_synth_align: COHORT_DIR_FOR_SPECIMEN required}"

# Tool resolution: prefer native binaries (faster than container exec;
# matches the user's snakemake env on IRIS). Fall back to containers if
# the binary isn't on PATH.
#
#   - badread, minimap2, samtools — available natively on IRIS under
#     /home/ahunos/miniforge3/envs/snakemake/bin/ (added to PATH by
#     submit_pipeline.sh). Containers are fallbacks.
#   - VISOR — typically not native. Looks for $CONTAINER_DIR/visor_*.sif,
#     falls back to `VISOR` on PATH if present.

BIND="/data1/greenbab"

# VISOR HACk shells out to bedtools which isn't in the visor SIF. We bind-
# mount a host conda env that ships bedtools and prepend its bin/ to PATH
# inside the container. Override BEDTOOLS_ENV if bedtools lives elsewhere.
: "${BEDTOOLS_ENV:=/home/ahunos/miniforge3/envs/snakemake}"

_probe() {  # $1=tool, $2=sif_env_path (optional)
    if command -v "$1" >/dev/null 2>&1; then
        echo "$1"
    elif [[ -n "${2:-}" && -s "$2" ]]; then
        echo "apptainer exec --bind $BIND $2 $1"
    else
        echo "__MISSING__"
    fi
}

_probe_visor() {  # VISOR needs bedtools on PATH inside the container
    if command -v VISOR >/dev/null 2>&1; then
        echo "VISOR"
    elif [[ -n "${VISOR_SIF:-}" && -s "$VISOR_SIF" && -x "$BEDTOOLS_ENV/bin/bedtools" ]]; then
        echo "apptainer exec --bind $BIND --bind ${BEDTOOLS_ENV}:/opt/bt_env --env PATH=/opt/bt_env/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin $VISOR_SIF VISOR"
    else
        echo "__MISSING__"
    fi
}

CONTAINER_DIR=$(python3 -c "import yaml; print(yaml.safe_load(open('$HPC_CONFIG'))['containers']['dir'])")
# VISOR SIF is an absolute path in config (group-shared), not relative to CONTAINER_DIR.
VISOR_SIF=$(python3 -c "import yaml; print(yaml.safe_load(open('$HPC_CONFIG'))['containers']['visor_sif'])")
BADREAD_SIF="$CONTAINER_DIR/$(python3 -c "import yaml; print(yaml.safe_load(open('$HPC_CONFIG'))['containers']['badread'])")"
MINIMAP_SIF="$CONTAINER_DIR/$(python3 -c "import yaml; print(yaml.safe_load(open('$HPC_CONFIG'))['containers']['minimap2'])")"
SAMTOOLS_SIF=$(python3 -c "import yaml; print(yaml.safe_load(open('$HPC_CONFIG'))['containers']['ont_shared'])")

VISOR_CMD=$(_probe_visor)
BADREAD_CMD=$(_probe badread "$BADREAD_SIF")
MINIMAP_CMD=$(_probe minimap2 "$MINIMAP_SIF")
SAMTOOLS_CMD=$(_probe samtools "$SAMTOOLS_SIF")

for var in VISOR_CMD BADREAD_CMD MINIMAP_CMD SAMTOOLS_CMD; do
    if [[ "${!var}" == "__MISSING__" ]]; then
        echo "ERROR: cannot resolve ${var%_CMD} — not on PATH and no container at the config path." >&2
        exit 1
    fi
    echo "${var%_CMD} = ${!var}"
done

VISOR=(${VISOR_CMD})
BADREAD=(${BADREAD_CMD})
MINIMAP=(${MINIMAP_CMD})
SAMTOOLS=(${SAMTOOLS_CMD})

OUT_DIR="$SANDBOX/synth/$SPECIMEN/$RUN_ID"
REF_FA="$SANDBOX/ref/ref.fa"
mkdir -p "$OUT_DIR" "$SANDBOX/logs"

STAMP=$(date +%Y%m%d_%H%M%S)
LOG="$SANDBOX/logs/synth_align_${SPECIMEN}_${RUN_ID}_${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date '+%F %T')] === synth_align $SPECIMEN $RUN_ID ==="
echo "OUT_DIR=$OUT_DIR"
echo "COVERAGE=$COVERAGE, PURITY=$PURITY, SEED=$SEED"

# ── Phase 1: VISOR HACk — haplotype FASTAs ────────────────────────────────────
HAP_DIR="$OUT_DIR/hack"
rm -rf "$HAP_DIR"
"${VISOR[@]}" HACk \
    -g "$REF_FA" \
    -b "$COHORT_DIR_FOR_SPECIMEN/haplotype1.hack.bed" \
       "$COHORT_DIR_FOR_SPECIMEN/haplotype2.hack.bed" \
    -o "$HAP_DIR"
HAP1="$HAP_DIR/h1.fa"
HAP2="$HAP_DIR/h2.fa"
mv "$HAP_DIR/h1.fa" "$HAP1" 2>/dev/null || true
[[ -s "$HAP1" ]] || { echo "HACk did not produce h1.fa" >&2; ls -la "$HAP_DIR" >&2; exit 1; }
"${SAMTOOLS[@]}" faidx "$HAP1"
"${SAMTOOLS[@]}" faidx "$HAP2"
echo "[Phase 1] haplotypes built at $HAP_DIR"

# ── Phase 2: Badread — tumor haplotypes + optional normal contamination ──────
# Per-haplotype read budget (bp) at purity P, total coverage C, ref length R:
#   tumor haplotype coverage  = C × P / 200  (split across two haplotypes)
#   normal-contamination cov  = C × (1 - P/100)
REF_BP=$("${SAMTOOLS[@]}" view -H "$REF_FA" 2>/dev/null | awk 'BEGIN{n=0} /^@SQ/ {for(i=1;i<=NF;i++) if ($i ~ /^LN:/) {split($i,a,":"); n += a[2]}} END {print n}')
if [[ -z "$REF_BP" || "$REF_BP" -eq 0 ]]; then
    REF_BP=$("${SAMTOOLS[@]}" faidx "$REF_FA" 2>/dev/null; awk '{s+=$2} END {print s}' "$REF_FA.fai")
fi
echo "[Phase 2] reference length: $REF_BP bp"

hap_cov=$(python3 -c "print(round($COVERAGE * $PURITY / 200, 2))")
norm_cov=$(python3 -c "print(round($COVERAGE * (1 - $PURITY/100), 2))")
hap_bp=$(python3 -c "print(int($REF_BP * $hap_cov))")
norm_bp=$(python3 -c "print(int($REF_BP * $norm_cov))")
echo "[Phase 2] per-haplotype target: ${hap_cov}x (${hap_bp} bp); normal-mix: ${norm_cov}x (${norm_bp} bp)"

FASTQ="$OUT_DIR/reads.fastq"
> "$FASTQ"
# Two tumor haplotypes, two seeds (SEED, SEED+1) so inserts differ between runs.
for i in 1 2; do
    hap_fa="$OUT_DIR/hack/h${i}.fa"
    [[ -s "$hap_fa" ]] || continue
    "${BADREAD[@]}" simulate \
        --reference "$hap_fa" \
        --quantity "${hap_bp}bp" \
        --seed $((SEED + i - 1)) \
        --error_model nanopore2023 \
        --qscore_model nanopore2023 \
        --identity 95,3,99 \
        --length 15000,13000 \
        >> "$FASTQ"
done
# Normal-contamination reads — straight from the reference. Only emit if
# purity < 100 (so normal specimens don't produce a zero-bp stream).
if [[ "${NO_PURITY_MIX:-}" != "true" && "$norm_bp" -gt 0 ]]; then
    "${BADREAD[@]}" simulate \
        --reference "$REF_FA" \
        --quantity "${norm_bp}bp" \
        --seed $((SEED + 100)) \
        --error_model nanopore2023 \
        --qscore_model nanopore2023 \
        --identity 95,3,99 \
        --length 15000,13000 \
        >> "$FASTQ"
fi
gzip -f "$FASTQ"
FASTQ_GZ="${FASTQ}.gz"
echo "[Phase 2] reads: $FASTQ_GZ ($(du -h "$FASTQ_GZ" | cut -f1))"

# ── Phase 3: minimap2 alignment ───────────────────────────────────────────────
SAM="$OUT_DIR/sim.sam"
"${MINIMAP[@]}" -ax map-ont -t "${SLURM_CPUS_PER_TASK:-16}" "$REF_FA" "$FASTQ_GZ" > "$SAM"

# ── Phase 4: sort + index ────────────────────────────────────────────────────
BAM="$OUT_DIR/sim.srt.bam"
"${SAMTOOLS[@]}" sort -@ "${SLURM_CPUS_PER_TASK:-16}" -o "$BAM" "$SAM"
"${SAMTOOLS[@]}" index -@ "${SLURM_CPUS_PER_TASK:-16}" "$BAM"
rm -f "$SAM"
ls -lh "$BAM"*

# ── metadata manifest — downstream pipeline reads from this ──────────────────
ASSAY_ID="${SPECIMEN}-ONT-WGS-${RUN_ID}"
"${SAMTOOLS[@]}" flagstat "$BAM" > "$OUT_DIR/flagstat.txt"

printf "assay_id\tbam_path\n%s\t%s\n" "$ASSAY_ID" "$BAM" > "$OUT_DIR/metadata.tsv"
echo "[$(date '+%F %T')] === DONE: $SPECIMEN $RUN_ID — $BAM ==="
