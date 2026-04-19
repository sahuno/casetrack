#!/usr/bin/env bash
#SBATCH --job-name=inject_methyl
#SBATCH --account=greenbab
#SBATCH --partition=componc_cpu
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:30:00
#
# run_inject_methyl.sh — stamp MM/ML methylation tags onto one synth_align BAM.
#
# Fires between phase 1 (synth_align) and phase 2 (attach_bams). Writes
# sim.meth.srt.bam alongside sim.srt.bam and rewrites metadata.tsv so
# downstream phases consume the methylation-tagged BAM.
#
# Required env:
#   SPECIMEN    — specimen_id (e.g. HGSOC_SIM_01_normal)
#   RUN_ID      — ONT run label (R01, R02)
#   ASSAY_ID    — casetrack assay_id (written back to metadata.tsv)
#   SANDBOX     — pipeline sandbox root
#   REF_FASTA   — reference FASTA (indexed)
#
# Optional:
#   SEED          — per-assay RNG seed. Default: sha256(specimen|run) truncated.
#   TUMOR_RATE    — target mean methylation for tumor specimens (default 0.50).
#   NORMAL_RATE   — target mean methylation for normal specimens (default 0.80).
#   METH_BYTE     — ML byte for methylated calls (default 230).
#   CANON_BYTE    — ML byte for canonical calls (default 25).
#
# Author: Samuel Ahuno

set -euo pipefail

: "${SPECIMEN:?run_inject_methyl: SPECIMEN required}"
: "${RUN_ID:?run_inject_methyl: RUN_ID required}"
: "${ASSAY_ID:?run_inject_methyl: ASSAY_ID required}"
: "${SANDBOX:?run_inject_methyl: SANDBOX required}"
: "${REF_FASTA:?run_inject_methyl: REF_FASTA required}"

TUMOR_RATE="${TUMOR_RATE:-0.50}"
NORMAL_RATE="${NORMAL_RATE:-0.80}"
METH_BYTE="${METH_BYTE:-230}"
CANON_BYTE="${CANON_BYTE:-25}"

case "$SPECIMEN" in
    *_tumor)  SPECIMEN_TYPE=tumor ;;
    *_normal) SPECIMEN_TYPE=normal ;;
    *) echo "ERROR: cannot infer tumor/normal from SPECIMEN=$SPECIMEN" >&2; exit 1 ;;
esac

if [[ -z "${SEED:-}" ]]; then
    SEED=$(python3 -c "import hashlib,sys; print(int(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8], 16))" "${SPECIMEN}|${RUN_ID}")
fi

WORK_DIR="$SANDBOX/synth/$SPECIMEN/$RUN_ID"
IN_BAM="$WORK_DIR/sim.srt.bam"
OUT_BAM="$WORK_DIR/sim.meth.srt.bam"
META="$WORK_DIR/metadata.tsv"

[[ -s "$IN_BAM" ]] || { echo "ERROR: missing $IN_BAM — run synth_align first." >&2; exit 1; }
[[ -s "$REF_FASTA" ]] || { echo "ERROR: missing REF_FASTA=$REF_FASTA" >&2; exit 1; }

LOG_DIR="$SANDBOX/logs"
mkdir -p "$LOG_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
LOG="$LOG_DIR/inject_methyl_${SPECIMEN}_${RUN_ID}_${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1

echo "[$(date '+%F %T')] === inject_methyl $SPECIMEN $RUN_ID ==="
echo "SPECIMEN_TYPE=$SPECIMEN_TYPE  SEED=$SEED"
echo "IN_BAM=$IN_BAM"
echo "OUT_BAM=$OUT_BAM"
echo "REF_FASTA=$REF_FASTA"

: "${SCRIPTS_DIR:?run_inject_methyl: SCRIPTS_DIR required (points to examples/hgsoc_sim/hpc/scripts)}"

# Compute nodes default to system python3, which lacks pysam. Point at the
# snakemake conda env (override with PYTHON_BIN).
PYTHON_BIN="${PYTHON_BIN:-/home/ahunos/miniforge3/envs/snakemake/bin/python3}"

"$PYTHON_BIN" "$SCRIPTS_DIR/inject_methyl_tags.py" \
    --in-bam "$IN_BAM" \
    --out-bam "$OUT_BAM" \
    --reference "$REF_FASTA" \
    --specimen-type "$SPECIMEN_TYPE" \
    --tumor-target-rate "$TUMOR_RATE" \
    --normal-target-rate "$NORMAL_RATE" \
    --meth-prob-byte "$METH_BYTE" \
    --canon-prob-byte "$CANON_BYTE" \
    --seed "$SEED"

printf "assay_id\tbam_path\n%s\t%s\n" "$ASSAY_ID" "$OUT_BAM" > "$META"

ls -lh "$OUT_BAM"* "$META"
echo "[$(date '+%F %T')] === DONE: inject_methyl $SPECIMEN $RUN_ID ==="
