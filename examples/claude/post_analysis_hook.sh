#!/usr/bin/env bash
# post_analysis_hook.sh — drop-in SLURM post-analysis hook.
#
# Runs Claude Code on the freshly-written results TSV, captures a QC
# review as a second TSV, validates the header, and appends it back
# into the manifest as its own analysis (cc_<analysis>_review).
#
# Author: Samuel Ahuno <ekwame001@gmail.com>
# Date:   2026-04-15
#
# Required env:
#   SAMPLE_ID     — sample identifier (row key)
#   ANALYSIS      — name of the just-completed analysis (e.g. modkit)
#   MANIFEST      — path to manifest.tsv
#   RESULTS_TSV   — path to the per-sample summary TSV just appended
#
# Optional env:
#   CC_BIN        — Claude Code binary (default: claude)
#   CASETRACK_BIN — casetrack binary (default: casetrack)
#   PROMPT_FILE   — path to the prompt template
#                   (default: qc_review_prompt.md next to this script)
#   REVIEW_DIR    — where to write the intermediate review TSV
#                   (default: $PWD)
#
# Usage (at the end of a SLURM job, after a successful casetrack append):
#
#   export SAMPLE_ID ANALYSIS MANIFEST RESULTS_TSV
#   bash /path/to/post_analysis_hook.sh

set -euo pipefail

: "${SAMPLE_ID:?post_analysis_hook: SAMPLE_ID is required}"
: "${ANALYSIS:?post_analysis_hook: ANALYSIS is required}"
: "${MANIFEST:?post_analysis_hook: MANIFEST is required}"
: "${RESULTS_TSV:?post_analysis_hook: RESULTS_TSV is required}"

CC_BIN="${CC_BIN:-claude}"
CASETRACK_BIN="${CASETRACK_BIN:-casetrack}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROMPT_FILE="${PROMPT_FILE:-$script_dir/qc_review_prompt.md}"
REVIEW_DIR="${REVIEW_DIR:-$PWD}"

if [[ ! -f "$MANIFEST" ]]; then
    echo "post_analysis_hook: manifest not found: $MANIFEST" >&2
    exit 1
fi
if [[ ! -f "$RESULTS_TSV" ]]; then
    echo "post_analysis_hook: results TSV not found: $RESULTS_TSV" >&2
    exit 1
fi
if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "post_analysis_hook: prompt file not found: $PROMPT_FILE" >&2
    exit 1
fi

# ── build the prompt: substitute placeholders ─────────────────────────────────
prompt="$(cat "$PROMPT_FILE")"
prompt="${prompt//__SAMPLE_ID__/$SAMPLE_ID}"
prompt="${prompt//__ANALYSIS__/$ANALYSIS}"
prompt="${prompt//__RESULTS_TSV__/$RESULTS_TSV}"

# ── invoke claude non-interactively; capture stdout to a review TSV ───────────
review_tsv="${REVIEW_DIR}/cc_review_${SAMPLE_ID}_${ANALYSIS}.tsv"
if ! "$CC_BIN" --print "$prompt" > "$review_tsv"; then
    echo "post_analysis_hook: claude invocation failed" >&2
    exit 3
fi

# ── validate: exactly one header line with the three expected columns ────────
expected_header=$'sample_id\tcc_'"${ANALYSIS}"$'_qc_pass\tcc_'"${ANALYSIS}"$'_qc_note'
actual_header="$(head -n1 "$review_tsv" | tr -d '\r')"
if [[ "$actual_header" != "$expected_header" ]]; then
    echo "post_analysis_hook: review TSV header mismatch" >&2
    echo "  expected: $(printf '%q' "$expected_header")" >&2
    echo "  got:      $(printf '%q' "$actual_header")" >&2
    exit 4
fi

n_rows=$(($(wc -l < "$review_tsv") - 1))
if (( n_rows < 1 )); then
    echo "post_analysis_hook: review TSV has no data rows" >&2
    exit 5
fi

# ── append into the manifest as a distinct analysis so it gets its own ───────
# ── _done timestamp, its own schema entry, and its own provenance line. ──────
"$CASETRACK_BIN" append \
    --manifest "$MANIFEST" \
    --results "$review_tsv" \
    --key sample_id \
    --analysis "cc_${ANALYSIS}_review"

echo "post_analysis_hook: logged QC review for ${SAMPLE_ID} (${ANALYSIS})"
