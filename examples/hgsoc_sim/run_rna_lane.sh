#!/bin/bash
# run_rna_lane.sh — one-shot driver for the optional real-RNA lane.
#
# Runs the four RNA-only steps in sequence, re-using the reference FASTA
# already produced by the DNA lane's step 00.
#
#   00/4  scripts/00_fetch_reference.sh     (reuses DNA lane if already done)
#   01/4  scripts/00b_fetch_gencode.sh      slice the GENCODE GTF, build transcripts.fa
#   02/4  scripts/00c_fetch_nanosim_model.sh  fetch the R9.4.1 cDNA pre-trained model
#   03/4  scripts/01b_prepare_expression.py  per-specimen expression.tsv
#   04/4  scripts/02b_run_nanosim.sh        NanoSim + minimap2 splice
#
# Specimens without an opt-in `rna:` block in config.yaml are skipped.
# Each step is idempotent — re-running the driver only re-runs what's missing.
#
# Prerequisites:
#   - casetrack on PATH (pip install -e . --user from repo root)
#   - python3 with pandas + pyyaml + numpy
#   - either Apptainer with the SIFs from containers/README.md (including
#     the gffread + nanosim additions), OR docker (export RUNNER=docker)
#
# Usage:
#   bash examples/hgsoc_sim/run_rna_lane.sh
#
# Author: Samuel Ahuno (ekwame001@gmail.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$SCRIPT_DIR/scripts"

echo "================================================================"
echo "== hgsoc_sim — optional real-RNA lane"
echo "================================================================"

echo
echo "==> [00/4] Fetching reference slice (shared with DNA lane)…"
bash "$SCRIPTS/00_fetch_reference.sh"

echo
echo "==> [01/4] Slicing GENCODE GTF + extracting transcript FASTAs…"
bash "$SCRIPTS/00b_fetch_gencode.sh"

echo
echo "==> [02/4] Fetching NanoSim pre-trained cDNA model…"
bash "$SCRIPTS/00c_fetch_nanosim_model.sh"

echo
echo "==> [03/4] Preparing per-specimen expression TSVs…"
python3 "$SCRIPTS/01b_prepare_expression.py"

echo
echo "==> [04/4] Running NanoSim + minimap2 splice for every rna/ specimen…"
bash "$SCRIPTS/02b_run_nanosim.sh"

echo
echo "================================================================"
echo "== RNA lane complete. BAMs under sandbox/hgsoc_sim/cohort/*/*/rna/"
echo "================================================================"
