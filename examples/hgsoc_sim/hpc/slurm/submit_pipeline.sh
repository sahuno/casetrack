#!/usr/bin/env bash
# submit_pipeline.sh — fan out the hgsoc_sim HPC pipeline across SLURM.
#
# Phases, each gated on the previous via --dependency=afterok:
#
#   1. synth_align   — one job per (specimen × ONT flowcell-run). 10 jobs.
#   2. attach_bams   — single fast job that pulls bam_path from each
#                      synth_align's metadata.tsv and appends it onto the
#                      corresponding assay row via casetrack add-metadata.
#                      (Runs serially; appends are cheap.)
#   3. premerge_flagstat — reuses examples/patterns/premerge_runs/
#                          one job per ONT_WGS assay. Autoflag writes
#                          qc_events for failing runs.
#   4. merge_ont     — reuses patterns/premerge_runs/run_merge.sh.
#                      one job per specimen (5 specimens × 2 ONT runs each).
#   5. modkit_merged — reuses patterns/premerge_runs/run_modkit_merged.sh.
#                      one job per specimen, writes --column-prefix merged.
#   6. mock_scrna    — 5 jobs, one per scRNA assay. Independent of phases 1-5.
#   7. summary       — after all above land, emit dashboard + cohort report.
#
# Dependency graph:
#
#   synth_align ──► attach_bams ──► premerge_flagstat ──► merge_ont ──► modkit_merged ──┐
#                                                                                      ├──► summary
#   mock_scrna (independent) ─────────────────────────────────────────────────────────┘
#
# Usage:
#   PROJECT_DIR=... bash submit_pipeline.sh {plan|synth|qc|merge|modkit|scrna|summary|all} [--submit]
#     plan   — print what would be submitted (dry-run)
#     synth  — only phase 1 (synth_align)
#     qc     — phases 2+3 (attach_bams + premerge_flagstat)
#     merge  — phase 4
#     modkit — phase 5
#     scrna  — phase 6
#     summary — phase 7
#     all    — 1 → 2 → 3 → 4 → 5 → 7 chained; 6 launched alongside
#
# Required env:
#   PROJECT_DIR            — casetrack project directory
#   SANDBOX                — pipeline sandbox (synth outputs, ref, cohort BEDs)
#
# Recommended env:
#   CASETRACK_BIN          — absolute path to casetrack (compute nodes may
#                            not have it on PATH)
#   PATTERN_DIR            — defaults to examples/patterns/premerge_runs/
#                            (relative to repo root)
#   SAMTOOLS_CONTAINER     — onttools_v3.10.sif for flagstat/merge
#   MODKIT_CONTAINER       — same image works for modkit
#   REF_FASTA              — absolute path to $SANDBOX/ref/ref.fa

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HPC_DIR="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$HPC_DIR/../../.." && pwd)"
SCRIPTS="$HPC_DIR/scripts"
PATTERN_DIR="${PATTERN_DIR:-$REPO_ROOT/examples/patterns/premerge_runs}"

PHASE="${1:-plan}"
shift || true
SUBMIT=""
[[ "${1:-}" == "--submit" ]] && SUBMIT=1

: "${PROJECT_DIR:?submit_pipeline: PROJECT_DIR required}"
: "${SANDBOX:?submit_pipeline: SANDBOX required}"

CASETRACK_BIN="${CASETRACK_BIN:-$(command -v casetrack || echo casetrack)}"
SAMTOOLS_CONTAINER="${SAMTOOLS_CONTAINER:-/data1/greenbab/software/images/onttools_v3.10.sif}"
MODKIT_CONTAINER="${MODKIT_CONTAINER:-/data1/greenbab/software/images/onttools_v3.10.sif}"
REF_FASTA="${REF_FASTA:-$SANDBOX/ref/ref.fa}"

mkdir -p "$PROJECT_DIR/logs"

log() { printf '%s\n' "$*" >&2; }

_dispatch() {
    local label="$1"; shift
    local dep="$1"; shift
    local cmd=(sbatch)
    [[ -n "$dep" ]] && cmd+=(--dependency=afterok:"$dep")
    cmd+=("$@")
    if [[ -n "$SUBMIT" ]]; then
        local jid
        jid=$("${cmd[@]}" | awk '/Submitted batch job/ {print $4}')
        log "  $label → job $jid"
        printf '%s' "$jid"
    else
        log "  $label:"
        log "    ${cmd[*]}"
    fi
}

# Query casetrack for every ONT_WGS assay and walk specimens.
_ont_assays() {
    "$CASETRACK_BIN" query --project-dir "$PROJECT_DIR" --fmt tsv \
        "SELECT assay_id, specimen_id FROM assays WHERE assay_type='ONT' ORDER BY assay_id" \
        | tail -n +2
}

_scrna_assays() {
    "$CASETRACK_BIN" query --project-dir "$PROJECT_DIR" --fmt tsv \
        "SELECT assay_id FROM assays WHERE assay_type='scRNA' ORDER BY assay_id" \
        | tail -n +2
}

_specimens_with_ont() {
    "$CASETRACK_BIN" query --project-dir "$PROJECT_DIR" --fmt tsv \
        "SELECT DISTINCT specimen_id FROM assays WHERE assay_type='ONT' ORDER BY specimen_id" \
        | tail -n +2
}

# ── Phase 1: synth_align ──────────────────────────────────────────────────────
phase_synth() {
    local upstream="${1:-}"
    local deps=()
    log "=== Phase 1: synth_align ==="
    while IFS=$'\t' read -r assay_id specimen_id; do
        [[ -z "$assay_id" ]] && continue
        # Parse RUN_ID from assay_id tail, e.g. R01/R02
        local run_id="${assay_id##*-}"
        # PATIENT from specimen_id by stripping trailing _tumor/_normal.
        local patient="${specimen_id%_*}"

        # Coverage per run: total coverage divided by runs_per_specimen.
        # Pulled from parent config dynamically; fallback to 17 (half of 35).
        local coverage purity seed
        read -r coverage purity seed < <(
            python3 "$SCRIPTS/_specimen_synth_params.py" \
                --patient "$patient" --specimen "$specimen_id" --run-id "$run_id"
        )
        local cohort_dir="$SANDBOX/cohort/$patient/${specimen_id#${patient}_}"

        local exports="PATIENT=$patient,SPECIMEN=$specimen_id,RUN_ID=$run_id"
        exports+=",COVERAGE=$coverage,PURITY=$purity,SEED=$seed"
        exports+=",SANDBOX=$SANDBOX,HPC_CONFIG=$HPC_DIR/config.yaml"
        exports+=",COHORT_DIR_FOR_SPECIMEN=$cohort_dir"

        local jid
        jid=$(_dispatch "synth $assay_id" "$upstream" \
            --chdir="$SANDBOX" --export=ALL,"$exports" \
            "$HERE/run_synth_align.sh")
        [[ -n "$jid" ]] && deps+=("$jid")
    done < <(_ont_assays)
    printf '%s\n' "${deps[@]}" | paste -sd: -
}

# ── Phase 2: attach_bams ──────────────────────────────────────────────────────
# Runs `casetrack add-metadata` once all synths are done, routing each
# assay's bam_path from its synth_align metadata.tsv back onto the assays
# table. This unblocks phases 3-5 which query casetrack for bam_path.
phase_attach_bams() {
    local upstream="${1:-}"
    log "=== Phase 2: attach_bams ==="
    local exports="PROJECT_DIR=$PROJECT_DIR,SANDBOX=$SANDBOX"
    exports+=",CASETRACK_BIN=$CASETRACK_BIN,SCRIPTS=$SCRIPTS"
    _dispatch "attach_bams" "$upstream" \
        --chdir="$PROJECT_DIR" \
        --export=ALL,"$exports" \
        --account=greenbab --partition=componc_cpu \
        --job-name=attach_bams --cpus-per-task=1 --mem=2G --time=00:15:00 \
        --output=logs/attach_bams_%A.out --error=logs/attach_bams_%A.err \
        --wrap "bash $HERE/run_attach_bams.sh"
}

# ── Phase 3: premerge_flagstat ────────────────────────────────────────────────
phase_flagstat() {
    local upstream="${1:-}"
    log "=== Phase 3: premerge_flagstat ==="
    local deps=()
    while IFS=$'\t' read -r assay_id specimen_id; do
        [[ -z "$assay_id" ]] && continue
        # bam_path from synth_align metadata.tsv (casetrack will have it
        # after phase 2, but we pass it explicitly to avoid a race).
        local run_id="${assay_id##*-}"
        local bam_path="$SANDBOX/synth/$specimen_id/$run_id/sim.srt.bam"

        local exports="ASSAY_ID=$assay_id,BAM_PATH=$bam_path,PROJECT_DIR=$PROJECT_DIR"
        exports+=",DEMO_SCRIPTS_DIR=$PATTERN_DIR,CASETRACK_BIN=$CASETRACK_BIN"
        exports+=",SAMTOOLS_CONTAINER=$SAMTOOLS_CONTAINER"
        # Loose thresholds: the 1.1 Mb sim produces ~500 reads at 2x and
        # ~25k at 25x. We want only the deliberately-broken SIM_02 normal
        # to flag — thresholds in hpc/config.yaml qc_thresholds section.
        exports+=",MIN_TOTAL_READS=500,MIN_MAPPED_PCT=95.0"

        local jid
        # Override pattern's --time=00:30:00; IRIS Prolog has been seen to
        # burn 20-25 min before user code starts, leaving too little wall.
        jid=$(_dispatch "flagstat $assay_id" "$upstream" \
            --chdir="$PROJECT_DIR" --export=ALL,"$exports" \
            --time=01:30:00 \
            "$PATTERN_DIR/run_premerge_flagstat.sh")
        [[ -n "$jid" ]] && deps+=("$jid")
    done < <(_ont_assays)
    printf '%s\n' "${deps[@]}" | paste -sd: -
}

# ── Phase 4: merge ────────────────────────────────────────────────────────────
phase_merge() {
    local upstream="${1:-}"
    log "=== Phase 4: merge_ont ==="
    local deps=()
    while IFS=$'\t' read -r specimen_id; do
        [[ -z "$specimen_id" ]] && continue
        local exports="SPECIMEN_ID=$specimen_id,PROJECT_DIR=$PROJECT_DIR"
        exports+=",DEMO_SCRIPTS_DIR=$PATTERN_DIR,CASETRACK_BIN=$CASETRACK_BIN"
        exports+=",SAMTOOLS_CONTAINER=$SAMTOOLS_CONTAINER"
        local jid
        # Override the pattern's default --mem=16G; HGSOC sim runs sort+merge
        # on 2-run BAMs where samtools needs more headroom per the profile.
        # Also bump --time for long IRIS Prolog.
        jid=$(_dispatch "merge $specimen_id" "$upstream" \
            --chdir="$PROJECT_DIR" --export=ALL,"$exports" \
            --mem=64G --time=02:00:00 \
            "$PATTERN_DIR/run_merge.sh")
        [[ -n "$jid" ]] && deps+=("$jid")
    done < <(_specimens_with_ont)
    printf '%s\n' "${deps[@]}" | paste -sd: -
}

# ── Phase 5: modkit_merged ────────────────────────────────────────────────────
phase_modkit() {
    local upstream="${1:-}"
    log "=== Phase 5: modkit_merged ==="
    local deps=()
    while IFS=$'\t' read -r specimen_id; do
        [[ -z "$specimen_id" ]] && continue
        local exports="SPECIMEN_ID=$specimen_id,PROJECT_DIR=$PROJECT_DIR"
        exports+=",REF_FASTA=$REF_FASTA"
        exports+=",DEMO_SCRIPTS_DIR=$PATTERN_DIR,CASETRACK_BIN=$CASETRACK_BIN"
        exports+=",MODKIT_CONTAINER=$MODKIT_CONTAINER"
        exports+=",BAM_COL=merged_bam_path"
        local jid
        jid=$(_dispatch "modkit $specimen_id" "$upstream" \
            --chdir="$PROJECT_DIR" --export=ALL,"$exports" \
            "$PATTERN_DIR/run_modkit_merged.sh")
        [[ -n "$jid" ]] && deps+=("$jid")
    done < <(_specimens_with_ont)
    printf '%s\n' "${deps[@]}" | paste -sd: -
}

# ── Phase 6: mock_scrna ───────────────────────────────────────────────────────
phase_scrna() {
    local upstream="${1:-}"
    log "=== Phase 6: mock_scrna ==="
    local deps=()
    while IFS=$'\t' read -r assay_id; do
        [[ -z "$assay_id" ]] && continue
        local exports="ASSAY_ID=$assay_id,PROJECT_DIR=$PROJECT_DIR"
        exports+=",SCRIPTS_DIR=$SCRIPTS,CASETRACK_BIN=$CASETRACK_BIN"
        local jid
        jid=$(_dispatch "scrna $assay_id" "$upstream" \
            --chdir="$PROJECT_DIR" --export=ALL,"$exports" \
            "$HERE/run_mock_scrna.sh")
        [[ -n "$jid" ]] && deps+=("$jid")
    done < <(_scrna_assays)
    printf '%s\n' "${deps[@]}" | paste -sd: -
}

# ── Phase 7: summary ──────────────────────────────────────────────────────────
phase_summary() {
    local upstream="${1:-}"
    log "=== Phase 7: summary (dashboard + cohort + benchmark) ==="
    local exports="PROJECT_DIR=$PROJECT_DIR,SANDBOX=$SANDBOX"
    exports+=",CASETRACK_BIN=$CASETRACK_BIN,HPC_DIR=$HPC_DIR"
    _dispatch "summary" "$upstream" \
        --chdir="$PROJECT_DIR" \
        --export=ALL,"$exports" \
        --account=greenbab --partition=componc_cpu \
        --job-name=hgsoc_summary --cpus-per-task=2 --mem=4G --time=00:30:00 \
        --output=logs/summary_%A.out --error=logs/summary_%A.err \
        --wrap "bash $HERE/run_summary.sh"
}

case "$PHASE" in
    plan)
        SUBMIT="" ; phase_synth ; phase_attach_bams ; phase_flagstat ; phase_merge ; phase_modkit ; phase_scrna ; phase_summary ;;
    synth)        phase_synth ;;
    qc)           phase_flagstat "$(phase_attach_bams)" ;;
    merge)        phase_merge ;;
    modkit)       phase_modkit ;;
    scrna)        phase_scrna ;;
    summary)      phase_summary ;;
    all)
        dep_synth=$(phase_synth)
        dep_attach=$(phase_attach_bams "$dep_synth")
        dep_flag=$(phase_flagstat "$dep_attach")
        dep_merge=$(phase_merge "$dep_flag")
        dep_modkit=$(phase_modkit "$dep_merge")
        dep_scrna=$(phase_scrna "")   # independent
        # summary waits on BOTH the modkit chain and scrna
        summary_dep="${dep_modkit}"
        [[ -n "$dep_scrna" ]] && summary_dep="${dep_modkit}:${dep_scrna}"
        phase_summary "$summary_dep"
        ;;
    resume)
        # Resume after synth + attach_bams already landed their data.
        # Starts at flagstat (no upstream dep), chains merge → modkit →
        # summary. Assumes bam_path is populated on each assay row and
        # scRNA metrics are already appended (or intentionally skipped).
        dep_flag=$(phase_flagstat "")
        dep_merge=$(phase_merge "$dep_flag")
        dep_modkit=$(phase_modkit "$dep_merge")
        phase_summary "$dep_modkit"
        ;;
    resume_merge)
        # Resume after flagstat data is also already in casetrack (e.g. if
        # flagstat TSVs were appended from the login node after a SLURM
        # timeout). Starts at merge; chains modkit → summary.
        dep_merge=$(phase_merge "")
        dep_modkit=$(phase_modkit "$dep_merge")
        phase_summary "$dep_modkit"
        ;;
    *)
        echo "Unknown phase: $PHASE" >&2
        echo "Usage: PROJECT_DIR=... SANDBOX=... bash $0 {plan|synth|qc|merge|modkit|scrna|summary|all|resume|resume_merge} [--submit]" >&2
        exit 1
        ;;
esac

if [[ -z "$SUBMIT" ]]; then
    echo >&2
    echo "Dry run. Re-run with --submit to dispatch." >&2
fi
