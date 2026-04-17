#!/bin/bash
# 00_fetch_reference.sh — download GRCh38 chr17 and slice a 1 Mb BRCA1 region.
#
# The slice is ~1 MB on disk. Safe to repeat — skips the download if the
# slice FASTA already exists.
#
# Author: Samuel Ahuno (ekwame001@gmail.com)

set -euo pipefail

# Resolve paths relative to the repo root (assumes script is invoked from
# any CWD via an absolute or relative path to this file).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

SANDBOX="${SANDBOX:-$REPO_ROOT/sandbox/hgsoc_sim}"
REF_DIR="$SANDBOX/ref"
mkdir -p "$REF_DIR"

# Parameters read from config.yaml via a tiny Python one-liner so we never
# drift from the declared slice coordinates.
readarray -t CFG < <(python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('$DEMO_DIR/config.yaml'))['reference']
print(cfg['source_url'])
print(cfg['chrom'])
print(cfg['region_start'])
print(cfg['region_end'])
print(cfg['slice_contig'])
")
SRC_URL="${CFG[0]}"
CHROM="${CFG[1]}"
REGION_START="${CFG[2]}"   # 0-based
REGION_END="${CFG[3]}"     # exclusive
SLICE_CONTIG="${CFG[4]}"

SLICE_FA="$REF_DIR/${SLICE_CONTIG}.fa"
SLICE_FAI="${SLICE_FA}.fai"

if [[ -s "$SLICE_FA" && -s "$SLICE_FAI" ]]; then
    echo "[00] slice already present: $SLICE_FA"
    exit 0
fi

FULL_GZ="$REF_DIR/chr17.full.fa.gz"
FULL_FA="$REF_DIR/chr17.full.fa"

if [[ ! -s "$FULL_GZ" ]]; then
    echo "[00] downloading $SRC_URL → $FULL_GZ"
    curl -sSL "$SRC_URL" -o "$FULL_GZ"
fi

if [[ ! -s "$FULL_FA" ]]; then
    echo "[00] decompressing chromosome 17 FASTA"
    gunzip -c "$FULL_GZ" > "$FULL_FA"
fi

# Ensembl's FASTA header is ">17 dna:chromosome …". Rename to "chr17" so the
# slice coordinates line up with GRCh38 UCSC-style references commonly used
# on IRIS.
if ! head -n1 "$FULL_FA" | grep -qE '^>chr17'; then
    echo "[00] normalizing contig name to chr17"
    sed -i.bak '1 s/^>17/>chr17/' "$FULL_FA"
fi

# Prefer the container samtools so the host doesn't need it installed.
CONTAINER_DIR="${CONTAINER_DIR:-$HOME/apps/containers}"
SAMTOOLS_SIF="$CONTAINER_DIR/samtools_1.21.sif"

if command -v samtools >/dev/null 2>&1; then
    SAMTOOLS="samtools"
elif [[ -s "$SAMTOOLS_SIF" ]]; then
    SAMTOOLS="apptainer exec --bind $REF_DIR $SAMTOOLS_SIF samtools"
else
    echo "Error: need samtools on PATH OR the samtools SIF at $SAMTOOLS_SIF" >&2
    echo "       See examples/hgsoc_sim/containers/README.md for pull commands." >&2
    exit 1
fi

# Build full-chr FAI so we can pull a region.
$SAMTOOLS faidx "$FULL_FA"

# Extract the 1-Mb slice. The extracted block keeps its original coordinates
# in the FASTA header (e.g. ">chr17:42500000-43500000"), which would confuse
# VISOR — so we rewrite the header to SLICE_CONTIG and let VISOR treat it as
# a standalone contig numbered from 1.
ONE_BASED_START=$((REGION_START + 1))
$SAMTOOLS faidx "$FULL_FA" "${CHROM}:${ONE_BASED_START}-${REGION_END}" | \
    awk -v name="$SLICE_CONTIG" 'NR==1 {print ">"name; next} {print}' > "$SLICE_FA"
$SAMTOOLS faidx "$SLICE_FA"

echo "[00] wrote $SLICE_FA"
echo "[00] wrote $SLICE_FAI"
printf "[00] slice length: "
awk 'NR>1 {n += length($0)} END {print n}' "$SLICE_FA"
