#!/bin/bash
# 00_fetch_reference.sh — download GRCh38 chr17 and extract per-slice FASTAs.
#
# Reads the `reference.slices` list from config.yaml. For each slice:
#   1. downloads chr17 once (cached across slices)
#   2. extracts the slice region via samtools faidx
#   3. renames the header to the slice's `name` so downstream tools see a
#      clean contig starting at position 0
#   4. concatenates all slices into sandbox/hgsoc_sim/ref/ref.fa
#
# The concatenated reference is what both VISOR HACk and LASeR use — one
# multi-contig FASTA.
#
# Author: Samuel Ahuno (ekwame001@gmail.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

SANDBOX="${SANDBOX:-$REPO_ROOT/sandbox/hgsoc_sim}"
REF_DIR="$SANDBOX/ref"
mkdir -p "$REF_DIR"

REF_FA="$REF_DIR/ref.fa"
REF_FAI="${REF_FA}.fai"

# ── Pull slice list from config.yaml via a tiny Python helper ────────────────
# Emits lines of "name<TAB>chrom<TAB>start<TAB>end" (one per slice) + a
# trailing line for the source URL prefixed with "URL<TAB>".

readarray -t CFG_LINES < <(python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('$DEMO_DIR/config.yaml'))['reference']
print('URL\\t' + cfg['source_url'])
for s in cfg['slices']:
    print('\\t'.join([s['name'], s['chrom'], str(s['start']), str(s['end'])]))
")

SRC_URL=""
declare -a SLICE_LINES=()
for line in "${CFG_LINES[@]}"; do
    case "$line" in
        URL$'\t'*) SRC_URL="${line#URL$'\t'}" ;;
        *)         SLICE_LINES+=("$line") ;;
    esac
done

if [[ -z "$SRC_URL" ]]; then
    echo "Error: no reference.source_url in config.yaml" >&2
    exit 1
fi
if [[ ${#SLICE_LINES[@]} -eq 0 ]]; then
    echo "Error: reference.slices is empty in config.yaml" >&2
    exit 1
fi

# If the concatenated ref already exists AND covers every slice, short-circuit.
if [[ -s "$REF_FA" && -s "$REF_FAI" ]]; then
    all_present=true
    for sl in "${SLICE_LINES[@]}"; do
        name="${sl%%$'\t'*}"
        if ! awk -v want="$name" '$1==want {found=1} END {exit found?0:1}' "$REF_FAI"; then
            all_present=false
            break
        fi
    done
    if $all_present; then
        echo "[00] reference already built: $REF_FA"
        awk '{printf "[00]   %-20s %d bp\n", $1, $2}' "$REF_FAI"
        exit 0
    fi
fi

# ── Download chr17 once (cached) ─────────────────────────────────────────────

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
# slice coordinates line up with UCSC-style references commonly used on IRIS.
if ! head -n1 "$FULL_FA" | grep -qE '^>chr17'; then
    echo "[00] normalizing contig name to chr17"
    sed -i.bak '1 s/^>17/>chr17/' "$FULL_FA"
fi

# ── Resolve samtools ──────────────────────────────────────────────────────────

CONTAINER_DIR="${CONTAINER_DIR:-$HOME/apps/containers}"
SAMTOOLS_SIF="$CONTAINER_DIR/samtools_1.21.sif"

if command -v samtools >/dev/null 2>&1; then
    SAMTOOLS=(samtools)
elif [[ -s "$SAMTOOLS_SIF" ]]; then
    SAMTOOLS=(apptainer exec --bind "$REF_DIR" "$SAMTOOLS_SIF" samtools)
else
    echo "Error: need samtools on PATH OR the samtools SIF at $SAMTOOLS_SIF" >&2
    echo "       See examples/hgsoc_sim/containers/README.md for pull commands." >&2
    exit 1
fi

"${SAMTOOLS[@]}" faidx "$FULL_FA"

# ── Extract + rename each slice, then concat ─────────────────────────────────

# Build into a tmp FASTA and swap atomically so re-runs don't leave a half-
# written ref.fa on disk.
TMP_FA="$REF_FA.tmp"
: > "$TMP_FA"

for line in "${SLICE_LINES[@]}"; do
    IFS=$'\t' read -r name chrom start end <<< "$line"
    one_based_start=$((start + 1))
    "${SAMTOOLS[@]}" faidx "$FULL_FA" "${chrom}:${one_based_start}-${end}" | \
        awk -v name="$name" 'NR==1 {print ">"name; next} {print}' \
        >> "$TMP_FA"
    echo "[00]   extracted ${chrom}:${one_based_start}-${end} → contig ${name}"
done

mv "$TMP_FA" "$REF_FA"
"${SAMTOOLS[@]}" faidx "$REF_FA"

echo "[00] wrote $REF_FA (+ .fai)"
awk '{printf "[00]   %-20s %d bp\n", $1, $2}' "$REF_FAI"
