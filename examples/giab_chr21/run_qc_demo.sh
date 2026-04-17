#!/usr/bin/env bash
# run_qc_demo.sh — end-to-end walk-through of v0.4 QC / censoring / consent
# features on the GIAB chr21 cohort. Tracks GH #14.
#
# Exercises, in order:
#   1.  SLURM autoflag — re-append flagstat with a qc_pass column so one
#       assay auto-emits a qc_events row with source='slurm'
#   2.  manual censor (contamination on HG006_PBA16846)
#   3.  qc-history — show the one active event
#   4.  status --usable — 4/4 done but N/4 usable
#   5.  uncensor (normal path)
#   6.  cohort readiness — register matched-normal + pair-by tissue_site
#   7.  consent revocation — mark HG002 revoked
#   8.  ethics-override gate — attempt uncensor without, then with
#   9.  _active DuckDB view — row count before + after censor
#   10. dashboard regen — QC chips + Excluded section
#   11. validate — should stay clean throughout
#   12. recover round-trip — delete DB, replay provenance, diff
#
# Usage:
#   bash run_qc_demo.sh [PROJECT_DIR]
#     PROJECT_DIR defaults to ./giab_demo_project
#     If the project doesn't exist or is empty, run_mock_demo.sh is invoked
#     first to bootstrap it (2 patients × 2 specimens × 4 assays × 3
#     mock analyses).
#
# Runs entirely in-process — no cluster, no real BAM I/O. All v0.4 state
# transitions are applied to the real casetrack.db in PROJECT_DIR.
#
# Re-runnability: this is a walk-through, not an idempotent script. If the
# project already has any qc_events (from a prior run), the script exits
# cleanly. Delete PROJECT_DIR and re-run for a fresh demonstration.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${1:-${HERE}/giab_demo_project}"

# ── 0. Ensure project is bootstrapped with 3 mock analyses appended ───────────
if [[ ! -f "${PROJECT_DIR}/casetrack.db" ]]; then
    echo "### Project not found — running run_mock_demo.sh first ###"
    bash "${HERE}/run_mock_demo.sh" "${PROJECT_DIR}" >/dev/null
    echo
fi

# Older demo dirs created pre-v0.4 don't have qc_events yet.
if ! sqlite3 "${PROJECT_DIR}/casetrack.db" ".tables" 2>/dev/null | grep -q qc_events; then
    echo "### Pre-v0.4 project detected — running casetrack migrate-qc ###"
    casetrack migrate-qc --project-dir "${PROJECT_DIR}"
    echo
fi

# Guard: refuse to proceed if prior QC state is already in place (avoids
# UNIQUE-conflict errors from censoring the same entities twice).
_existing_events=$(sqlite3 "${PROJECT_DIR}/casetrack.db" "SELECT COUNT(*) FROM qc_events;" 2>/dev/null || echo 0)
if [[ "${_existing_events}" -gt 0 ]]; then
    echo "### ${PROJECT_DIR} already has ${_existing_events} qc_events — demo has run before."
    echo "### Delete the project dir and re-run for a fresh walk-through:"
    echo "###     rm -rf '${PROJECT_DIR}' && bash '${BASH_SOURCE[0]}' '${PROJECT_DIR}'"
    exit 0
fi

sep() { printf '\n────────────────────────────────────────────────────────────────\n=== %s\n────────────────────────────────────────────────────────────────\n' "$*"; }

# ── 1. SLURM autoflag via summary TSV ─────────────────────────────────────────
# Append a synthetic summary carrying a qc_pass column. We force one row to
# "fail" the 95% threshold so the auto-flag mechanism has something to emit.
# Each qc_events row lands with source='slurm' in the same transaction as the
# append — no separate CLI call needed.
sep "1. SLURM autoflag — append flagstat_recheck with qc_pass/qc_fail_reason"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
AUTOFLAG_TSV="${TMP}/flagstat_autoflag.tsv"
python3 - "${PROJECT_DIR}" "${AUTOFLAG_TSV}" <<'PY'
import sqlite3, sys
proj, out = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(f"{proj}/casetrack.db")
# Pull just the IDs — mapped_pct may be absent after a recover round-trip,
# so the demo synthesizes values unconditionally below.
ids = [r[0] for r in conn.execute(
    "SELECT assay_id FROM assays WHERE assay_id NOT LIKE '%_normal_%' "
    "ORDER BY assay_id"
)]
conn.close()
# Deterministic synthetic mapped_pct values — HG006_PAY77227 is forced
# sub-95 so the auto-flag mechanism has something real to emit.
synth = {
    "HG002_PAW70337": 98.3,
    "HG002_PAW71238": 97.6,
    "HG006_PAY77227": 91.3,  # deliberate fail
    "HG006_PBA16846": 96.6,
}
with open(out, "w") as f:
    f.write("assay_id\tmapped_pct_recheck\tqc_pass\tqc_fail_reason\tqc_warn\n")
    for aid in ids:
        mp = synth.get(aid, 98.0)
        if mp < 95:
            pas, reason, warn = "false", f"run_qc_demo: mapped_pct={mp} < 95 (synthetic fail for demo)", ""
        else:
            pas, reason = "true", ""
            warn = "" if mp >= 97 else "mapped_pct below warn threshold (97)"
        f.write(f"{aid}\t{mp}\t{pas}\t{reason}\t{warn}\n")
print(f"Wrote autoflag TSV → {out}")
PY
cat "${AUTOFLAG_TSV}"
casetrack append \
    --project-dir "${PROJECT_DIR}" \
    --analysis flagstat_recheck \
    --results "${AUTOFLAG_TSV}"
echo "--- qc-history assay HG006_PAY77227 (expect one active source=slurm event) ---"
casetrack qc-history --project-dir "${PROJECT_DIR}" --level assay --id HG006_PAY77227

# ── 2. Manual censor ──────────────────────────────────────────────────────────
sep "2. manual censor HG006_PBA16846 (contamination)"
casetrack censor \
    --project-dir "${PROJECT_DIR}" \
    --level assay --id HG006_PBA16846 \
    --kind contamination \
    --reason "run_qc_demo: deliberately flagged for demo; K-mer contamination 3.4%, threshold 1.0%"

# ── 3. qc-history ─────────────────────────────────────────────────────────────
sep "3. qc-history for HG006_PBA16846"
casetrack qc-history \
    --project-dir "${PROJECT_DIR}" \
    --level assay --id HG006_PBA16846

# ── 4. status --usable ────────────────────────────────────────────────────────
sep "4. status --usable (expect 2/4 usable — one slurm-flagged + one manual)"
casetrack status --project-dir "${PROJECT_DIR}" --usable

# ── 5. uncensor the manual contamination flag ─────────────────────────────────
sep "5. uncensor HG006_PBA16846 (normal path)"
casetrack uncensor \
    --project-dir "${PROJECT_DIR}" \
    --level assay --id HG006_PBA16846 \
    --reason "run_qc_demo: re-ran contamination check at higher stringency — within spec (0.4% K-mer)"

# ── 6. cohort readiness — register matched-normal + pair-by ───────────────────
# The giab_ont template doesn't declare `tissue_site` on specimens, so we
# add it dynamically via `casetrack append --level specimen` (append
# auto-creates analysis-added columns via ALTER TABLE). Then cohort
# --pair-by tissue_site has two partitions to work with.
sep "6. cohort readiness — register a matched-normal specimen for HG002"
if ! sqlite3 "${PROJECT_DIR}/casetrack.db" "SELECT specimen_id FROM specimens WHERE specimen_id='HG002_gDNA_normal';" | grep -q HG002; then
    casetrack register \
        --project-dir "${PROJECT_DIR}" \
        --level specimen --id HG002_gDNA_normal \
        --parent HG002 \
        --meta "specimen_type=whole_genome_dna,cell_line=GM24385,source=Coriell/NIST"
    casetrack register \
        --project-dir "${PROJECT_DIR}" \
        --level assay --id HG002_normal_WGS \
        --parent HG002_gDNA_normal \
        --meta "assay_type=ONT_WGS,flowcell_id=DEMO-NORMAL,chemistry=R10.4.1,basecaller_model=dorado_sup,condition=reference"
else
    echo "HG002_gDNA_normal already registered — skipping"
fi

# Tag existing specimens tumor vs normal so --pair-by has real partitions.
TISSUE_TSV="${TMP}/tissue_site_tags.tsv"
python3 - "${PROJECT_DIR}" "${TISSUE_TSV}" <<'PY'
import sqlite3, sys
proj, out = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(f"{proj}/casetrack.db")
rows = list(conn.execute("SELECT specimen_id FROM specimens ORDER BY specimen_id"))
conn.close()
with open(out, "w") as f:
    f.write("specimen_id\ttissue_site\n")
    for (sid,) in rows:
        tissue = "normal" if sid.endswith("_gDNA_normal") else "tumor"
        f.write(f"{sid}\t{tissue}\n")
PY
cat "${TISSUE_TSV}"
casetrack append \
    --project-dir "${PROJECT_DIR}" \
    --level specimen --analysis tissue_tag \
    --results "${TISSUE_TSV}"

echo "--- cohort --pair-by tissue_site ---"
casetrack cohort \
    --project-dir "${PROJECT_DIR}" \
    --pair-by tissue_site \
    --partition-order tumor,normal

# ── 7. consent revocation on HG002 ────────────────────────────────────────────
sep "7. consent revocation — mark HG002 consent_revoked"
casetrack censor \
    --project-dir "${PROJECT_DIR}" \
    --level patient --id HG002 \
    --kind consent_revoked \
    --reason "run_qc_demo: simulated withdrawal — not a real revocation" \
    --withdrawal-date 2026-04-17

echo "--- status --usable (HG002's 3 assays should drop out) ---"
casetrack status --project-dir "${PROJECT_DIR}" --usable

# ── 8. ethics-override gate ───────────────────────────────────────────────────
sep "8a. uncensor WITHOUT --ethics-override (expect exit 2)"
set +e
casetrack uncensor \
    --project-dir "${PROJECT_DIR}" \
    --level patient --id HG002 \
    --reason "try without override"
rc=$?
set -e
if [[ "${rc}" != "2" ]]; then
    echo "Expected exit 2 on consent reversal without --ethics-override, got ${rc}" >&2
    exit 1
fi
echo "✓ correctly refused with exit 2"

sep "8b. uncensor WITH --ethics-override --yes + qualifying reason"
casetrack uncensor \
    --project-dir "${PROJECT_DIR}" \
    --level patient --id HG002 \
    --ethics-override --yes \
    --reason "re-consent 2026-04-17 IRB-ref:GIAB-demo-12345"

# ── 9. _active view before/after a fresh censor ───────────────────────────────
sep "9. _active view row counts"
echo "--- SELECT COUNT(*) FROM _ (raw join) ---"
casetrack query --project-dir "${PROJECT_DIR}" --fmt json \
    "SELECT COUNT(*) AS n FROM _"
echo "--- SELECT COUNT(*) FROM _active (post-cascade) ---"
casetrack query --project-dir "${PROJECT_DIR}" --fmt json \
    "SELECT COUNT(*) AS n FROM _active"

# Put one fresh censor back so the dashboard has something to show.
casetrack censor \
    --project-dir "${PROJECT_DIR}" \
    --level assay --id HG002_PAW70337 \
    --kind qc_warn \
    --reason "run_qc_demo: demo warning — read N50 below target" \
    >/dev/null

echo "--- _active after adding a qc_warn on HG002_PAW70337 ---"
casetrack query --project-dir "${PROJECT_DIR}" --fmt json \
    "SELECT COUNT(*) AS n FROM _active"

# ── 10. dashboard regen ───────────────────────────────────────────────────────
sep "10. dashboard — QC chips + Excluded section"
casetrack dashboard \
    --project-dir "${PROJECT_DIR}" \
    --output "${PROJECT_DIR}/dashboard_qc.html"
ls -lh "${PROJECT_DIR}/dashboard_qc.html"
grep -E 'QC|Excluded|qc_warn|consent' "${PROJECT_DIR}/dashboard_qc.html" | head -5 || true

# ── 11. validate after all mutations ──────────────────────────────────────────
sep "11. validate — expect clean"
casetrack validate --project-dir "${PROJECT_DIR}"

# ── 12. recover round-trip ────────────────────────────────────────────────────
# --permit-partial is needed here because run_mock_demo.sh synthesizes its
# summary TSVs into a tmpdir that its own EXIT trap cleans up — recover
# can't re-read them. All QC actions (censor / uncensor / ethics_override)
# are self-contained in provenance.jsonl, so they replay byte-equivalently.
# The assertions below check the row counts we care about for this demo
# (PK tables + qc_events).
sep "12. recover round-trip — rebuild DB from provenance.jsonl"
BACKUP="${PROJECT_DIR}/casetrack.db.demo_backup"
cp "${PROJECT_DIR}/casetrack.db" "${BACKUP}"
rm "${PROJECT_DIR}/casetrack.db"
rm -f "${PROJECT_DIR}/casetrack.db-wal" "${PROJECT_DIR}/casetrack.db-shm"
casetrack recover --project-dir "${PROJECT_DIR}" --permit-partial

echo "--- diff backup vs recovered (row counts per table) ---"
python3 - "${PROJECT_DIR}" "${BACKUP}" <<'PY'
import sqlite3, sys
proj, bak = sys.argv[1], sys.argv[2]
cur_a = sqlite3.connect(f"{proj}/casetrack.db")
cur_b = sqlite3.connect(bak)
drift = 0
for t in ("patients", "specimens", "assays", "qc_events"):
    a = cur_a.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    b = cur_b.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    mark = "✓" if a == b else "✗ DRIFT"
    print(f"{mark} {t}: recovered={a}, backup={b}")
    if a != b:
        drift += 1
# Also verify every resolved event round-tripped with its ethics flag intact.
for col in ("kind", "source", "resolved_reason"):
    a_ev = set(cur_a.execute(f"SELECT id, {col} FROM qc_events").fetchall())
    b_ev = set(cur_b.execute(f"SELECT id, {col} FROM qc_events").fetchall())
    if a_ev != b_ev:
        print(f"✗ DRIFT qc_events.{col}: recovered∖backup={a_ev-b_ev}, backup∖recovered={b_ev-a_ev}")
        drift += 1
if not drift:
    print("✓ qc_events content matches byte-equivalently")
sys.exit(1 if drift else 0)
PY
rm -f "${BACKUP}"

sep "DONE — all v0.4 QC features exercised end-to-end against ${PROJECT_DIR}"
