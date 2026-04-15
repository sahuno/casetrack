/*
 * example_pipeline.nf — demo pipeline showing the casetrack_append contract.
 *
 * Author: Samuel Ahuno <ekwame001@gmail.com>
 * Date:   2026-04-15
 *
 * Run (from a project dir that already has manifest.tsv + samples.csv):
 *
 *   nextflow run example_pipeline.nf \
 *       --samples_csv samples.csv \
 *       --casetrack_manifest ./manifest.tsv \
 *       -profile slurm,apptainer
 */

nextflow.enable.dsl = 2

include { casetrack_append } from './casetrack.nf'

params.samples_csv = "${launchDir}/samples.csv"

process summarize_modkit {
    tag "${sample_id}"

    input:
      tuple val(sample_id), path(bam)

    output:
      tuple val('modkit_methylation'), path("${sample_id}_modkit.tsv")

    script:
    """
    # Real pipeline would run modkit here. This stub produces a two-column
    # summary TSV that casetrack_append will register.
    printf 'sample_id\\tmodkit_mean_meth\\n' > ${sample_id}_modkit.tsv
    printf '${sample_id}\\t0.72\\n'         >> ${sample_id}_modkit.tsv
    """
}

process summarize_tldr {
    tag "${sample_id}"

    input:
      tuple val(sample_id), path(bam)

    output:
      tuple val('tldr_insertions'), path("${sample_id}_tldr.tsv")

    script:
    """
    printf 'sample_id\\ttldr_l1_count\\n' > ${sample_id}_tldr.tsv
    printf '${sample_id}\\t14\\n'         >> ${sample_id}_tldr.tsv
    """
}

workflow {
    samples = Channel.fromPath(params.samples_csv)
                     .splitCsv(header: true)
                     .map { row -> tuple(row.sample_id, file(row.bam_path)) }

    // Fan out; each branch emits a (analysis, tsv) tuple and then
    // funnels through the single append gate.
    summarize_modkit(samples)
    summarize_tldr(samples)

    summarize_modkit.out
        .mix(summarize_tldr.out)
        | casetrack_append
}
