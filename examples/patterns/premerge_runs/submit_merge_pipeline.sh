#!/usr/bin/env bash
# submit_merge_pipeline.sh — drive the premerge-runs pattern across a
# casetrack project. Three phases, each a SLURM array:
#
#   phase 1 (PHASE=premerge_flagstat)   one job per assay
#   phase 2 (PHASE=merge)               one job per specimen (depends on phase 1)
#   phase 3 (PHASE=modkit_merged)       one job per specimen (depends on phase 2)
#
# Usage:
#   PROJECT_DIR=/path/to/proj bash submit_merge_pipeline.sh PHASE [--submit]
#     PHASE ∈ {premerge_flagstat, merge, modkit_merged, all}
#     omit --submit for dry-run
#
# Required env:
#   PROJECT_DIR       — casetrack project
#
# Recommended env:
#   SAMTOOLS_CONTAINER — apptainer image path (exported to jobs)
#   CASETRACK_BIN      — explicit path (SLURM compute nodes may not have it on PATH)
#   REF_FASTA          — required for PHASE=modkit_merged
#   MODKIT_CONTAINER   — apptainer image with modkit (for modkit_merged)
#   CHR_LIMIT          — optional, passed to modkit (e.g. "chr21")
#   SUFFIX             — appended to merged BAM filename (e.g. "hg38")
#
# --dependency afterok:<jobids> is used when PHASE=all so phase 2 waits on
# phase 1 and phase 3 waits on phase 2.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DEMO_SCRIPTS_DIR="${HERE}"

: "${PROJECT_DIR:?submit_merge_pipeline: PROJECT_DIR is required}"

PHASE="${1:-}"
if [[ -z "${PHASE}" ]]; then
    echo "Usage: PROJECT_DIR=... bash $0 {premerge_flagstat|merge|modkit_merged|all} [--submit]" >&2
    exit 1
fi
shift
SUBMIT=""
if [[ "${1:-}" == "--submit" ]]; then
    SUBMIT=1
fi

mkdir -p "${PROJECT_DIR}/logs"
CASETRACK_BIN="${CASETRACK_BIN:-casetrack}"

# Phases print progress to stderr (so we can capture dep-lists on stdout).
log() { printf '%s\n' "$*" >&2; }

_dispatch_or_preview() {
    # Args: <label> <upstream_dep_or_empty> <sbatch args...>
    # In --submit mode, sbatches and returns the job ID on stdout.
    # In dry-run, prints the sbatch command to stderr and returns nothing.
    local label="$1"; shift
    local dep="$1"; shift
    local cmd=(sbatch)
    [[ -n "${dep}" ]] && cmd+=(--dependency=afterok:"${dep}")
    cmd+=("$@")
    if [[ -n "${SUBMIT}" ]]; then
        local jid
        jid=$("${cmd[@]}" | awk '/Submitted batch job/ {print $4}')
        log "${label} → job ${jid}"
        printf '%s' "${jid}"
    else
        log "${label}:"
        log "  ${cmd[*]}"
    fi
}

# ── Phase 1: premerge_flagstat — one sbatch per assay ────────────────────────
phase1_premerge_flagstat() {
    local dep_jobs=()
    while IFS=$'\t' read -r assay_id bam_path; do
        [[ -z "${assay_id}" || "${assay_id}" == "assay_id" ]] && continue
        local exports="ASSAY_ID=${assay_id},BAM_PATH=${bam_path},PROJECT_DIR=${PROJECT_DIR},DEMO_SCRIPTS_DIR=${DEMO_SCRIPTS_DIR},CASETRACK_BIN=${CASETRACK_BIN}"
        [[ -n "${SAMTOOLS_CONTAINER:-}" ]] && exports+=",SAMTOOLS_CONTAINER=${SAMTOOLS_CONTAINER}"
        [[ -n "${MIN_TOTAL_READS:-}" ]] && exports+=",MIN_TOTAL_READS=${MIN_TOTAL_READS}"
        [[ -n "${MIN_MAPPED_PCT:-}" ]] && exports+=",MIN_MAPPED_PCT=${MIN_MAPPED_PCT}"

        local jid
        jid=$(_dispatch_or_preview "premerge_flagstat ${assay_id}" "" \
            --chdir="${PROJECT_DIR}" --export=ALL,"${exports}" \
            "${HERE}/run_premerge_flagstat.sh")
        [[ -n "${jid}" ]] && dep_jobs+=("${jid}")
    done < <(
        "${CASETRACK_BIN}" query --project-dir "${PROJECT_DIR}" --fmt tsv \
            "SELECT assay_id, bam_path FROM assays WHERE bam_path IS NOT NULL ORDER BY assay_id"
    )
    printf '%s\n' "${dep_jobs[@]}" | paste -sd: -
}

# ── Phase 2a (optional): subset_chr — restrict each merged BAM to one chr ────
# Usage: CHR=chr17 bash ... subset_chr --submit
phase2a_subset_chr() {
    local upstream_dep="${1:-}"
    : "${CHR:?subset_chr phase requires CHR (e.g. chr17)}"
    local dep_jobs=()
    while IFS=$'\t' read -r specimen_id; do
        [[ -z "${specimen_id}" || "${specimen_id}" == "specimen_id" ]] && continue
        local exports="SPECIMEN_ID=${specimen_id},PROJECT_DIR=${PROJECT_DIR},CHR=${CHR},DEMO_SCRIPTS_DIR=${DEMO_SCRIPTS_DIR},CASETRACK_BIN=${CASETRACK_BIN}"
        [[ -n "${SAMTOOLS_CONTAINER:-}" ]] && exports+=",SAMTOOLS_CONTAINER=${SAMTOOLS_CONTAINER}"

        local jid
        jid=$(_dispatch_or_preview "subset_${CHR} ${specimen_id}" "${upstream_dep}" \
            --chdir="${PROJECT_DIR}" --export=ALL,"${exports}" \
            "${HERE}/run_subset_chr.sh")
        [[ -n "${jid}" ]] && dep_jobs+=("${jid}")
    done < <(
        "${CASETRACK_BIN}" query --project-dir "${PROJECT_DIR}" --fmt tsv \
            "SELECT DISTINCT specimen_id FROM specimens ORDER BY specimen_id"
    )
    printf '%s\n' "${dep_jobs[@]}" | paste -sd: -
}

# ── Phase 2: merge — one sbatch per specimen ─────────────────────────────────
phase2_merge() {
    local upstream_dep="${1:-}"
    local dep_jobs=()
    while IFS=$'\t' read -r specimen_id; do
        [[ -z "${specimen_id}" || "${specimen_id}" == "specimen_id" ]] && continue
        local exports="SPECIMEN_ID=${specimen_id},PROJECT_DIR=${PROJECT_DIR},DEMO_SCRIPTS_DIR=${DEMO_SCRIPTS_DIR},CASETRACK_BIN=${CASETRACK_BIN}"
        [[ -n "${SAMTOOLS_CONTAINER:-}" ]] && exports+=",SAMTOOLS_CONTAINER=${SAMTOOLS_CONTAINER}"
        [[ -n "${SUFFIX:-}" ]] && exports+=",SUFFIX=${SUFFIX}"

        local jid
        jid=$(_dispatch_or_preview "merge ${specimen_id}" "${upstream_dep}" \
            --chdir="${PROJECT_DIR}" --export=ALL,"${exports}" \
            "${HERE}/run_merge.sh")
        [[ -n "${jid}" ]] && dep_jobs+=("${jid}")
    done < <(
        "${CASETRACK_BIN}" query --project-dir "${PROJECT_DIR}" --fmt tsv \
            "SELECT DISTINCT specimen_id FROM specimens ORDER BY specimen_id"
    )
    printf '%s\n' "${dep_jobs[@]}" | paste -sd: -
}

# ── Phase 3: modkit_merged — one sbatch per specimen ─────────────────────────
phase3_modkit_merged() {
    local upstream_dep="${1:-}"
    : "${REF_FASTA:?phase3 requires REF_FASTA}"
    while IFS=$'\t' read -r specimen_id; do
        [[ -z "${specimen_id}" || "${specimen_id}" == "specimen_id" ]] && continue
        local exports="SPECIMEN_ID=${specimen_id},PROJECT_DIR=${PROJECT_DIR},REF_FASTA=${REF_FASTA},DEMO_SCRIPTS_DIR=${DEMO_SCRIPTS_DIR},CASETRACK_BIN=${CASETRACK_BIN}"
        [[ -n "${MODKIT_CONTAINER:-}" ]] && exports+=",MODKIT_CONTAINER=${MODKIT_CONTAINER}"
        [[ -n "${CHR_LIMIT:-}" ]] && exports+=",CHR_LIMIT=${CHR_LIMIT}"
        [[ -n "${BAM_COL:-}" ]] && exports+=",BAM_COL=${BAM_COL}"
        [[ -n "${ANALYSIS_NAME:-}" ]] && exports+=",ANALYSIS_NAME=${ANALYSIS_NAME}"

        _dispatch_or_preview "modkit_merged ${specimen_id}" "${upstream_dep}" \
            --chdir="${PROJECT_DIR}" --export=ALL,"${exports}" \
            "${HERE}/run_modkit_merged.sh" >/dev/null
    done < <(
        "${CASETRACK_BIN}" query --project-dir "${PROJECT_DIR}" --fmt tsv \
            "SELECT DISTINCT specimen_id FROM specimens ORDER BY specimen_id"
    )
}

case "${PHASE}" in
    premerge_flagstat)
        phase1_premerge_flagstat ;;
    merge)
        phase2_merge ;;
    subset_chr)
        phase2a_subset_chr ;;
    modkit_merged)
        phase3_modkit_merged ;;
    all)
        echo "### Phase 1: premerge_flagstat ###"
        dep1=$(phase1_premerge_flagstat)
        echo "### Phase 2: merge (depends on ${dep1:-<none>}) ###"
        dep2=$(phase2_merge "${dep1}")
        echo "### Phase 3: modkit_merged (depends on ${dep2:-<none>}) ###"
        phase3_modkit_merged "${dep2}"
        ;;
    *)
        echo "Unknown PHASE: ${PHASE}" >&2; exit 1 ;;
esac

if [[ -z "${SUBMIT}" ]]; then
    echo
    echo "Dry-run — re-run with --submit to dispatch."
fi
