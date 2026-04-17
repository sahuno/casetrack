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
echo "==> [00/5] Fetching reference slice…"
bash "$SCRIPTS/00_fetch_reference.sh"

echo
echo "==> [01/5] Preparing VISOR BEDs…"
python3 "$SCRIPTS/01_prepare_visor_beds.py"

echo
echo "==> [02/5] Running VISOR HACk + Badread + minimap2 for every specimen…"
bash "$SCRIPTS/02_run_visor.sh"

echo
echo "==> [03/5] Truncating HGSOC_SIM_02 normal (to simulate a broken library)…"
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
