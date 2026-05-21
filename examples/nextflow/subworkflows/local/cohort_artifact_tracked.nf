/*
 * cohort_artifact_tracked.nf — packaged subworkflow for registering ONE
 * cohort-level artifact (joint VCF, panel-of-normals, cohort matrix) derived
 * from MANY assays, with its assay lineage, into casetrack (proposal 0009).
 *
 * Author: Samuel Ahuno <ekwame001@gmail.com>
 *
 * This is the fan-IN companion to the per-sample tracked pattern. The caller
 * runs the actual joint tool (joint genotyping, PoN build, …) and hands this
 * subworkflow three things:
 *
 *   ch_assay_ids     — a queue channel of the contributing assay_id strings.
 *                      The subworkflow gathers them into a one-per-line lineage
 *                      file with collectFile (so casetrack records exactly which
 *                      assays fed the artifact).
 *   ch_artifact_stats — a value channel emitting ONE tuple `(artifact, stats)`:
 *                      the cohort artifact file paired with its stats JSON file,
 *                      or `[]` in the stats slot when there are none (the process
 *                      drops --stats accordingly). Pairing them in one channel
 *                      avoids the `.combine([])` arity trap and matches reality —
 *                      the joint step produces the artifact and its stats together.
 *   ch_uses_references — (optional, default []) a value channel emitting a
 *                      comma-joined string of reference keys consumed by this
 *                      cohort analysis (e.g. "genome,dbsnp").  Pass `[]` or
 *                      omit when no references need recording — the process
 *                      drops --uses-references accordingly, mirroring stats.
 *   ch_derived_from   — (optional, default []) a value channel emitting a
 *                      comma-joined string of upstream node-refs this artifact
 *                      derives from (proposal 0011, e.g.
 *                      "cohort:make_pon@v1,reference:genome").  Pass `[]` or
 *                      omit when no derivation edges need recording — the
 *                      process drops --derived-from accordingly, mirroring
 *                      uses_references.
 *
 * `analysis` and `run_tag` come from params (pipeline-level config), matching
 * the rest of the casetrack Nextflow module. `(analysis, run_tag)` is the
 * unique key — a re-genotyping run uses a new run_tag and coexists with the
 * prior artifact.
 *
 * Usage:
 *
 *   include { COHORT_ARTIFACT_TRACKED } from './subworkflows/local/cohort_artifact_tracked.nf'
 *
 *   workflow {
 *       gvcfs = call_gvcf(assays_ch)                 // tuple(assay_id, gvcf)
 *       joint = joint_genotype(gvcfs.map { it[1] }.collect())   // → vcf
 *       stats = bcftools_stats(joint)                // → stats.json
 *
 *       COHORT_ARTIFACT_TRACKED(
 *           gvcfs.map { it[0] },                     // assay_ids
 *           joint.combine(stats),                    // (artifact, stats)
 *           Channel.value("genome,dbsnp"),            // uses_references (optional)
 *           Channel.value("cohort:make_pon@v1")       // derived_from (optional, 0011)
 *       )
 *       // …or with no stats/refs/derivation:
 *       //   joint.map { v -> tuple(v, []) }  + omit 3rd/4th args
 *   }
 *
 * Params consumed:
 *   params.casetrack_project_dir  (required) — casetrack project directory.
 *   params.cohort_analysis        (default 'joint_genotype') — analysis name.
 *   params.run_tag                (required) — run identifier.
 *   params.casetrack_bin, params.casetrack_extra — as in casetrack.nf.
 */

nextflow.enable.dsl = 2

include { casetrack_append_cohort } from '../../casetrack.nf'

params.cohort_analysis = params.containsKey('cohort_analysis') ? params.cohort_analysis : 'joint_genotype'
params.run_tag         = params.containsKey('run_tag') ? params.run_tag : 'run'

workflow COHORT_ARTIFACT_TRACKED {

    take:
      ch_assay_ids        // queue channel: one assay_id per contributing assay
      ch_artifact_stats   // value channel: ONE tuple (artifact, stats-or-[])
      ch_uses_references  // value channel: comma-joined ref keys, or [] to omit
      ch_derived_from     // value channel: comma-joined upstream node-refs (0011), or [] to omit

    main:
      // Gather the lineage into one assay-id-per-line manifest. collectFile
      // emits a single file once the upstream channel is complete.
      inputs_tsv = ch_assay_ids
          .collectFile(name: "${params.run_tag}.inputs.txt", newLine: true)

      // Assemble the single fan-in tuple the registration process expects.
      // combine of a 1-item channel with the (artifact, stats) tuple yields
      // (tsv, artifact, stats); stats, uses_references, and derived_from stay
      // single elements (possibly []), so the process drops their flags when empty.
      ch_call = inputs_tsv
          .combine(ch_artifact_stats)
          .combine(ch_uses_references)
          .combine(ch_derived_from)
          .map { tsv, artifact, stats, uses_refs, derived ->
              tuple(params.cohort_analysis, params.run_tag, artifact, tsv, stats, uses_refs, derived)
          }

      casetrack_append_cohort(ch_call)

    emit:
      registered = casetrack_append_cohort.out   // tuple(analysis, run_tag)
}
