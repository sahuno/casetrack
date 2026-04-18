#!/bin/bash
# 00b_fetch_gencode.sh — download GENCODE v47, slice to our reference regions,
#                       extract per-slice transcript FASTAs via gffread.
#
# Reads reference.slices from config.yaml, then for each slice:
#   1. filters the GENCODE GTF to chr17 entries fully contained in the slice
#   2. rewrites chrom name to the slice's renamed contig (chr17_brca1 / chr17_tp53)
#   3. shifts all coordinates so the contig starts at 1
#   4. extracts transcript sequences from sandbox/hgsoc_sim/ref/ref.fa via gffread
#
# Emits:
#   sandbox/hgsoc_sim/ref/
#       gencode.sliced.gtf          (all slices, in renamed/shifted coords)
#       transcripts.fa              (all transcripts from all slices)
#       transcripts.tsv             (transcript_id, gene_name, slice, length)
#
# Prerequisites:
#   - scripts/00_fetch_reference.sh has already produced ref.fa
#   - gffread on PATH OR the gffread SIF under $CONTAINER_DIR
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
if [[ ! -s "$REF_FA" ]]; then
    echo "Error: reference not found at $REF_FA — run 00_fetch_reference.sh first." >&2
    exit 1
fi

# GENCODE release is pinned here rather than read from config so the URL
# and filename stay in lockstep. Bump when you want a newer annotation.
GENCODE_RELEASE="${GENCODE_RELEASE:-47}"
GENCODE_URL="https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_${GENCODE_RELEASE}/gencode.v${GENCODE_RELEASE}.primary_assembly.annotation.gtf.gz"
GENCODE_GZ="$REF_DIR/gencode.v${GENCODE_RELEASE}.gtf.gz"

SLICED_GTF="$REF_DIR/gencode.sliced.gtf"
TRANSCRIPTS_FA="$REF_DIR/transcripts.fa"
TRANSCRIPTS_TSV="$REF_DIR/transcripts.tsv"

# Short-circuit if we've already built everything.
if [[ -s "$TRANSCRIPTS_FA" && -s "$TRANSCRIPTS_TSV" && -s "$SLICED_GTF" ]]; then
    echo "[00b] transcripts already built:"
    wc -l "$TRANSCRIPTS_TSV" | awk '{printf "[00b]   %s transcripts in %s\n", $1-1, $2}'
    exit 0
fi

# ── Download GENCODE GTF (once, cached) ───────────────────────────────────────

if [[ ! -s "$GENCODE_GZ" ]]; then
    echo "[00b] downloading $GENCODE_URL"
    curl -sSL "$GENCODE_URL" -o "$GENCODE_GZ"
fi

# ── Slice + rewrite the GTF per config.yaml's reference.slices ────────────────

# Pull slice definitions via Python one-liner to avoid drift.
readarray -t SLICE_LINES < <(python3 -c "
import yaml
cfg = yaml.safe_load(open('$DEMO_DIR/config.yaml'))['reference']
for s in cfg['slices']:
    print('\\t'.join([s['name'], s['chrom'], str(s['start']), str(s['end'])]))
")

if [[ ${#SLICE_LINES[@]} -eq 0 ]]; then
    echo "Error: no slices in config.yaml reference.slices" >&2
    exit 1
fi

: > "$SLICED_GTF"
for line in "${SLICE_LINES[@]}"; do
    IFS=$'\t' read -r slice_name chrom start end <<< "$line"
    # GENCODE GTF is 1-based inclusive; config.yaml start is 0-based inclusive,
    # end is exclusive. Convert to GTF space so we can compare against GTF
    # fields directly.
    gtf_start=$((start + 1))
    gtf_end="$end"
    offset="$start"
    echo "[00b]   slicing ${chrom}:${gtf_start}-${gtf_end} → ${slice_name} (offset=${offset})"
    zcat "$GENCODE_GZ" | awk -F'\t' -v OFS='\t' \
        -v chrom="$chrom" -v start="$gtf_start" -v end="$gtf_end" \
        -v slice="$slice_name" -v off="$offset" '
        /^#/ { next }                                      # drop headers
        $1 == chrom && $4 >= start && $5 <= end {
            $1 = slice
            $4 = $4 - off
            $5 = $5 - off
            print
        }
    ' >> "$SLICED_GTF"
done

SLICED_LINES=$(wc -l < "$SLICED_GTF")
if [[ "$SLICED_LINES" -eq 0 ]]; then
    echo "Error: sliced GTF is empty — did the slice coords cover any GENCODE entries?" >&2
    exit 1
fi
echo "[00b] sliced GTF has $SLICED_LINES feature lines"

# ── Resolve gffread runner ────────────────────────────────────────────────────

CONTAINER_DIR="${CONTAINER_DIR:-$HOME/apps/containers}"
GFFREAD_SIF="$CONTAINER_DIR/gffread_0.12.9.sif"

if command -v gffread >/dev/null 2>&1; then
    GFFREAD=(gffread)
elif [[ -s "$GFFREAD_SIF" ]]; then
    GFFREAD=(apptainer exec --bind "$REF_DIR" "$GFFREAD_SIF" gffread)
else
    echo "Error: need gffread on PATH OR SIF at $GFFREAD_SIF" >&2
    echo "       See examples/hgsoc_sim/containers/README.md." >&2
    exit 1
fi

# ── Extract transcripts FASTA with gffread ────────────────────────────────────
# -w: write spliced exon sequences per transcript (what NanoSim needs)
# -g: genome reference (our sliced multi-contig ref.fa)

echo "[00b] running gffread to extract transcript FASTA"
"${GFFREAD[@]}" -w "$TRANSCRIPTS_FA" -g "$REF_FA" "$SLICED_GTF"

# ── Emit a transcript index TSV ───────────────────────────────────────────────
# gffread's FASTA headers look like:
#   >ENST00000357654.9 gene=BRCA1
# We parse them and add slice + length.

python3 <<PY
import re
from pathlib import Path

fa = Path("$TRANSCRIPTS_FA")
tsv = Path("$TRANSCRIPTS_TSV")

# Build a (transcript_id → slice) map from the sliced GTF's transcript rows.
import re
slice_by_tx: dict[str, str] = {}
for line in Path("$SLICED_GTF").read_text().splitlines():
    fields = line.split("\t")
    if len(fields) < 9 or fields[2] != "transcript":
        continue
    m = re.search(r'transcript_id "([^"]+)"', fields[8])
    if m:
        slice_by_tx[m.group(1)] = fields[0]

rows: list[tuple[str, str, str, int]] = []
tx_id = None
tx_gene = None
tx_len = 0

def flush():
    if tx_id is None:
        return
    rows.append((tx_id, tx_gene or "", slice_by_tx.get(tx_id, ""), tx_len))

for line in fa.read_text().splitlines():
    if line.startswith(">"):
        flush()
        header = line[1:].strip()
        tx_id = header.split()[0]
        m = re.search(r"gene=(\S+)", header)
        tx_gene = m.group(1) if m else ""
        tx_len = 0
    else:
        tx_len += len(line.strip())
flush()

with tsv.open("w") as f:
    f.write("transcript_id\tgene_name\tslice\tlength\n")
    for tid, gene, sl, ln in rows:
        f.write(f"{tid}\t{gene}\t{sl}\t{ln}\n")

print(f"[00b] wrote {len(rows)} transcripts to {tsv}")
PY

echo "[00b] wrote $TRANSCRIPTS_FA"
echo "[00b] wrote $TRANSCRIPTS_TSV"
