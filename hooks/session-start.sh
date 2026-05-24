#!/usr/bin/env bash
# casetrack plugin — SessionStart hook.
#
# When a Claude Code session starts in a directory that holds a casetrack
# project (./casetrack.db), emit a one-line status banner. Stays silent
# everywhere else.
#
# Output goes to stdout; the SessionStart hook surfaces it as additional
# context to the model. Keep it short, deterministic, and fast — this
# runs on every session start.
set -euo pipefail

DB="casetrack.db"
[[ -f "$DB" ]] || exit 0   # not a casetrack project — silent.

if ! command -v casetrack >/dev/null 2>&1; then
  echo "[casetrack] $DB present but the casetrack CLI is not on PATH — pip install casetrack"
  exit 0
fi

# One-shot DuckDB query via casetrack — counts at each level + censored
# total. Format is TSV (header + one row). Bail silently if the query
# errors so a half-migrated DB never blocks the session.
TSV="$(casetrack query --project-dir . --fmt tsv \
  "SELECT
     (SELECT COUNT(*) FROM patients)                                  AS patients,
     (SELECT COUNT(*) FROM specimens)                                 AS specimens,
     (SELECT COUNT(*) FROM assays)                                    AS assays,
     (SELECT COUNT(*) FROM assays WHERE qc_status IN ('fail','censored')) AS blocked" \
  2>/dev/null || true)"

[[ -z "$TSV" ]] && exit 0

# Parse the second (data) row into the banner.
read -r P S A B < <(printf '%s\n' "$TSV" | tail -n 1)

if [[ "${B:-0}" -gt 0 ]]; then
  echo "[casetrack] ${P} patients · ${S} specimens · ${A} assays · ${B} blocked (censored/failed)"
else
  echo "[casetrack] ${P} patients · ${S} specimens · ${A} assays"
fi

exit 0
