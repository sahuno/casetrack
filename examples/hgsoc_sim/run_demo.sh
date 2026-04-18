#!/bin/bash
# run_demo.sh — one-shot driver for the hgsoc_sim example.
#
# Runs steps 00 → 05 in sequence. Safe to re-invoke: each step short-circuits
# if its outputs already exist.
#
# Prerequisites:
#   - `casetrack` on PATH (pip install -e . --user from repo root)
#   - python3 with pandas + pyyaml
#   - either Apptainer with the SIFs pulled per containers/README.md,
#     OR docker on PATH (export RUNNER=docker), OR VISOR + samtools native
#
# Usage:
#   bash examples/hgsoc_sim/run_demo.sh
#
# Author: Samuel Ahuno (ekwame001@gmail.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$SCRIPT_DIR/scripts"

echo "================================================================"
echo "== hgsoc_sim demo"
echo "================================================================"

echo
echo "==> [00a/5] Fetching reference slice…"
bash "$SCRIPTS/00_fetch_reference.sh"

echo
echo "==> [00b/5] Fetching GENCODE GTF + extracting transcript FASTAs…"
bash "$SCRIPTS/00b_fetch_gencode.sh"

echo
echo "==> [00c/5] Fetching NanoSim pre-trained cDNA model…"
bash "$SCRIPTS/00c_fetch_nanosim_model.sh"

echo
echo "==> [01a/5] Preparing VISOR BEDs (DNA lane)…"
python3 "$SCRIPTS/01_prepare_visor_beds.py"

echo
echo "==> [01b/5] Preparing NanoSim expression TSVs (RNA lane)…"
python3 "$SCRIPTS/01b_prepare_expression.py"

echo
echo "==> [02a/5] Simulating DNA reads: VISOR HACk + Badread + minimap2…"
bash "$SCRIPTS/02_run_visor.sh"

echo
echo "==> [02b/5] Simulating RNA reads: NanoSim + minimap2 splice…"
bash "$SCRIPTS/02b_run_nanosim.sh"

echo
echo "==> [03/5] Truncating HGSOC_SIM_02 normal RNA (simulates failed library)…"
bash "$SCRIPTS/03_break_hgsoc_sim_02.sh"

echo
echo "==> [04/5] Summarizing simulated BAMs into per-assay TSVs…"
python3 "$SCRIPTS/04_summarize_mock.py"

echo
echo "==> [05/5] Bootstrapping casetrack project…"
python3 "$SCRIPTS/05_bootstrap_casetrack.py"

echo
echo "================================================================"
echo "== done. Try:"
PROJECT_DIR="$(python3 -c "from pathlib import Path; import os; print(Path(os.environ.get('SANDBOX', str(Path('$SCRIPT_DIR').resolve().parents[1] / 'sandbox' / 'hgsoc_sim'))) / 'project')")"
echo "   casetrack status    --project-dir $PROJECT_DIR --usable"
echo "   casetrack cohort    --project-dir $PROJECT_DIR \\"
echo "                       --assay-type ONT --pair-by tissue_site"
echo "   casetrack dashboard --project-dir $PROJECT_DIR --output dashboard.html"
echo "================================================================"
