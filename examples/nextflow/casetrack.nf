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
    def allow_flag = params.casetrack_allow_new ? '--allow-new' : ''
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
    def allow_flag   = params.casetrack_allow_new ? '--allow-new' : ''
    def fill_only    = params.containsKey('casetrack_fill_only') && params.casetrack_fill_only ? '--fill-only' : ''
    def overwrite    = params.containsKey('casetrack_overwrite') && params.casetrack_overwrite ? '--overwrite' : ''
    """
    ${params.casetrack_bin} add-metadata \\
        --manifest '${params.casetrack_manifest}' \\
        --metadata '${metadata_tsv}' \\
        --key '${params.casetrack_key}' \\
        ${allow_flag} ${fill_only} ${overwrite}
    """
}
