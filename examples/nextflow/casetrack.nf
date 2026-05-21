/*
 * casetrack.nf — reusable Nextflow DSL2 module for registering analysis
 *                results into a casetrack manifest.
 *
 * Author: Samuel Ahuno <ekwame001@gmail.com>
 * Date:   2026-04-15
 *
 * Usage:
 *
 *   include { casetrack_append } from './casetrack.nf'
 *
 *   workflow {
 *       summarize_modkit(samples_ch)               // emits (analysis, tsv)
 *       casetrack_append(summarize_modkit.out)
 *   }
 *
 * Contract:
 *   Input  : tuple(val analysis, path results_tsv)
 *            results_tsv must have `params.casetrack_key` as the first column.
 *   Output : same tuple, so downstream processes can chain off confirmed-logged
 *            results (e.g. QC review, Claude Code post-analysis hook).
 *
 * Notes:
 *   - maxForks 1 keeps the append log readable; flock in casetrack itself
 *     is what guarantees safety under real concurrency.
 *   - Retries twice on failure to tolerate transient NFS lock contention.
 *   - Set params.casetrack_allow_new = true if new sample IDs can appear
 *     mid-pipeline (unusual; most pipelines init the manifest up front).
 */

nextflow.enable.dsl = 2

params.casetrack_manifest  = "${launchDir}/manifest.tsv"
params.casetrack_key       = "sample_id"
params.casetrack_bin       = "casetrack"
params.casetrack_allow_new = false
params.casetrack_extra     = ""

process casetrack_append {
    tag "${analysis}"

    // Serialize project-wide to keep provenance log ordered; real concurrency
    // safety comes from POSIX flock inside `casetrack append`.
    maxForks 1
    errorStrategy 'retry'
    maxRetries 2

    input:
      tuple val(analysis), path(results_tsv)

    output:
      tuple val(analysis), path(results_tsv)

    script:
    // Config-level `params.casetrack_allow_new = true` is itself the
    // explicit confirmation, so we also pass --yes here.
    def allow_flag = params.casetrack_allow_new ? '--allow-new --yes' : ''
    """
    ${params.casetrack_bin} append \\
        --manifest '${params.casetrack_manifest}' \\
        --results '${results_tsv}' \\
        --key '${params.casetrack_key}' \\
        --analysis '${analysis}' \\
        ${allow_flag} ${params.casetrack_extra}
    """
}

process casetrack_add_metadata {
    tag "add-metadata"
    maxForks 1
    errorStrategy 'retry'
    maxRetries 2

    input:
      path metadata_tsv

    output:
      path metadata_tsv

    script:
    // Config-level `params.casetrack_allow_new = true` is itself the
    // explicit confirmation, so we also pass --yes here.
    def allow_flag = params.casetrack_allow_new ? '--allow-new --yes' : ''
    def fill_only  = params.containsKey('casetrack_fill_only') && params.casetrack_fill_only ? '--fill-only' : ''
    def overwrite  = params.containsKey('casetrack_overwrite') && params.casetrack_overwrite ? '--overwrite' : ''
    """
    ${params.casetrack_bin} add-metadata \\
        --manifest '${params.casetrack_manifest}' \\
        --metadata '${metadata_tsv}' \\
        --key '${params.casetrack_key}' \\
        ${allow_flag} ${fill_only} ${overwrite}
    """
}


/* ─────────────────────────────────────────────────────────────────────────────
 * v0.3 project-mode processes — use these for new pipelines.
 *
 * Switches:
 *   params.casetrack_project_dir  : path to the casetrack project directory
 *                                    (contains casetrack.toml + casetrack.db).
 *   params.casetrack_level        : target level (patient | specimen | assay).
 *                                    Defaults to 'assay' to match the shipped
 *                                    template's analysis_defaults.default_level.
 *
 * The "manifest" params above remain for v0.2 compatibility — pipelines can
 * migrate by replacing their `casetrack_append` call with
 * `casetrack_append_project` once they have a casetrack.toml+casetrack.db pair.
 * ──────────────────────────────────────────────────────────────────────────*/

params.casetrack_project_dir = params.containsKey('casetrack_project_dir') ? params.casetrack_project_dir : null
params.casetrack_level       = 'assay'
params.casetrack_col_type    = ''

process casetrack_append_project {
    tag "${analysis}@${params.casetrack_level}"

    maxForks 1
    errorStrategy 'retry'
    maxRetries 2

    input:
      tuple val(analysis), path(results_tsv)

    output:
      tuple val(analysis), path(results_tsv)

    script:
    def col_type_flag = params.casetrack_col_type ?
        "--col-type '${params.casetrack_col_type}'" : ''
    def overwrite_flag = params.containsKey('casetrack_overwrite') && params.casetrack_overwrite ?
        '--overwrite' : ''
    """
    ${params.casetrack_bin} append \\
        --project-dir '${params.casetrack_project_dir}' \\
        --level '${params.casetrack_level}' \\
        --results '${results_tsv}' \\
        --analysis '${analysis}' \\
        ${col_type_flag} ${overwrite_flag} ${params.casetrack_extra}
    """
}

process casetrack_register_project {
    tag "${level}:${row_id}"

    maxForks 1
    errorStrategy 'retry'
    maxRetries 2

    input:
      tuple val(level), val(row_id), val(parent_id), val(meta_str)

    output:
      tuple val(level), val(row_id)

    script:
    def parent_flag = parent_id ? "--parent '${parent_id}'" : ''
    def meta_flag   = meta_str  ? "--meta '${meta_str}'"  : ''
    def allow_flag  = params.casetrack_allow_new ? '--allow-new-parent --yes' : ''
    """
    ${params.casetrack_bin} register \\
        --project-dir '${params.casetrack_project_dir}' \\
        --level '${level}' \\
        --id '${row_id}' \\
        ${parent_flag} ${meta_flag} ${allow_flag}
    """
}

/* ─────────────────────────────────────────────────────────────────────────────
 * Cohort-level artifacts (proposal 0009).
 *
 * A cohort artifact is ONE output derived from MANY assays — a joint-genotyped
 * VCF, a panel-of-normals, a cohort matrix. The Nextflow shape is a fan-IN: the
 * per-assay channel collects (`.collect()`) into a single joint process, whose
 * output is registered once via `casetrack append-cohort`. The contributing
 * assay_ids are passed as a one-per-line file (`inputs_tsv`) — easiest produced
 * with `per_assay_ch.map{ it[0] }.collectFile(name: 'inputs.txt', newLine: true)`.
 *
 * Input  : tuple(val analysis, val run_tag, path artifact, path inputs_tsv,
 *                path stats_json)
 *          - (analysis, run_tag) is the unique key; a re-run uses a new run_tag.
 *          - inputs_tsv: one assay_id per line ('assay_id' header tolerated).
 *          - stats_json: cohort-level summary, OPTIONAL — pass `[]` (an empty
 *            list) for the staged-file slot when there are no stats, and the
 *            `--stats` flag is omitted. (`append-cohort` is stats-optional too.)
 * Output : tuple(val analysis, val run_tag) so a downstream report/QC step can
 *          chain off the confirmed-registered artifact.
 * ──────────────────────────────────────────────────────────────────────────*/

process casetrack_append_cohort {
    tag "${analysis}/${run_tag}"

    maxForks 1
    errorStrategy 'retry'
    maxRetries 2

    input:
      tuple val(analysis), val(run_tag), path(artifact), path(inputs_tsv), path(stats_json), val(uses_references)

    output:
      tuple val(analysis), val(run_tag)

    script:
    // stats_json is optional: when the caller passes `[]`, Nextflow stages
    // nothing and `stats_json` is falsy, so we drop the --stats flag entirely.
    def stats_arg = stats_json ? "--stats '${stats_json}'" : ''
    // uses_references is optional: pass `[]` (or empty string) when not needed;
    // when non-empty, the value is a comma-joined list of reference keys.
    def uses_references_arg = uses_references ? "--uses-references '${uses_references}'" : ''
    """
    ${params.casetrack_bin} append-cohort \\
        --project-dir '${params.casetrack_project_dir}' \\
        --analysis '${analysis}' \\
        --run-tag '${run_tag}' \\
        --path '${artifact}' \\
        --inputs-from '${inputs_tsv}' \\
        ${stats_arg} ${uses_references_arg} ${params.casetrack_extra}
    """
}

process casetrack_add_metadata_project {
    tag "add-metadata@${params.casetrack_level}"

    maxForks 1
    errorStrategy 'retry'
    maxRetries 2

    input:
      path metadata_tsv

    output:
      path metadata_tsv

    script:
    def allow_flag = params.casetrack_allow_new ? '--allow-new --yes' : ''
    def fill_only  = params.containsKey('casetrack_fill_only') && params.casetrack_fill_only ? '--fill-only' : ''
    def overwrite  = params.containsKey('casetrack_overwrite') && params.casetrack_overwrite ? '--overwrite' : ''
    """
    ${params.casetrack_bin} add-metadata \\
        --project-dir '${params.casetrack_project_dir}' \\
        --level '${params.casetrack_level}' \\
        --metadata '${metadata_tsv}' \\
        ${allow_flag} ${fill_only} ${overwrite}
    """
}
