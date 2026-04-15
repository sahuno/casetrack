# casetrack × Nextflow integration

Three ways to register per-sample analysis results into a casetrack manifest
from a Nextflow pipeline, in increasing order of coupling.

## 0. Prereq

The `casetrack` CLI must be available inside every process's runtime
environment — either on `$PATH`, or bound into the Apptainer/Singularity
image used by the process. A conda env or a module-loaded python install
both work.

Sanity check:

```bash
casetrack --help
```

## Pattern 1 — standalone module (recommended)

**File layout**

```
my_pipeline/
├── main.nf
├── nextflow.config
├── modules/
│   └── casetrack.nf         ← copy of examples/nextflow/casetrack.nf
└── manifest.tsv             ← pre-created via `casetrack init`
```

**main.nf**

```groovy
nextflow.enable.dsl = 2
include { casetrack_append } from './modules/casetrack.nf'

workflow {
    summarize_modkit(samples_ch)                      // emits (analysis, tsv)
    casetrack_append(summarize_modkit.out)            // register
}
```

**Rules the producer must follow:**

- Emit a tuple `(val analysis, path results_tsv)`.
- The TSV's first column must be `${params.casetrack_key}` (default `sample_id`).
- Every other column becomes a manifest column; casetrack prefixes nothing —
  name them however you want the manifest to see them.

**Why this is the default:** it makes the "register results" step a
first-class node in the DAG. Nextflow's `-resume` logic, workdir caching,
and failure handling all apply. Provenance captures the Nextflow process
work dir, hostname, and git commit hash automatically.

## Pattern 2 — `afterScript` directive

Use when you can't refactor a legacy process to emit a clean tuple and
want the append to happen as a side-effect of the main process finishing:

```groovy
process run_modkit {
    tag "${sample_id}"
    publishDir "results/modkit/${sample_id}", mode: 'copy'

    input:
      tuple val(sample_id), path(bam)

    output:
      path "summary.tsv"

    afterScript """
        casetrack append \
            --manifest '${params.casetrack_manifest}' \
            --results summary.tsv \
            --key sample_id \
            --analysis modkit_methylation
    """

    script:
    """
    # ... real modkit invocation ...
    python3 summarize_modkit.py --input modkit.out --sample ${sample_id} --output summary.tsv
    """
}
```

**Trade-off:** `afterScript` runs on the compute node, inherits the
process's environment, and fires even on partial success. But Nextflow
does *not* re-run `afterScript` on `-resume`, so if you change the append
side you have to re-run the whole process. Keep it dumb.

## Pattern 3 — collect & batch-append

When many processes produce `(sample_id, value)` pairs for the *same*
analysis, collect them and append in one shot:

```groovy
workflow {
    summarize_modkit(samples_ch)

    summarize_modkit.out
        .collectFile(name: 'modkit_batch.tsv', keepHeader: true, newLine: false)
        .set { modkit_batch_ch }

    modkit_batch_ch
        .map { tsv -> tuple('modkit_methylation', tsv) }
        | casetrack_append
}
```

This reduces flock contention and produces one provenance entry per
analysis instead of per sample.

## SLURM + Apptainer notes

- Set `casetrack_bin` to an absolute path when the CLI lives inside a sif:
  `--casetrack_bin '/opt/bin/casetrack'`.
- The `casetrack_append` process uses `maxForks 1` to serialize appends.
  POSIX flock inside casetrack already makes this safe for truly
  concurrent access; the throttle just keeps the provenance log readable
  and avoids log-line interleaving.
- Disable git capture if your compute nodes lack git:
  `process.env.CASETRACK_NO_GIT = 1` in your config.

## Override matrix

| Override                               | How                                           |
|----------------------------------------|-----------------------------------------------|
| Manifest path                          | `--casetrack_manifest /abs/path/manifest.tsv` |
| Key column                             | `--casetrack_key patient_id`                  |
| Binary path                            | `--casetrack_bin /path/to/casetrack`          |
| Allow previously-unseen sample IDs     | `--casetrack_allow_new true`                  |
| Arbitrary extra flags                  | `--casetrack_extra '--overwrite'`             |

## Running the shipped example

```bash
cd examples/nextflow
# Prepare a samples.csv with columns: sample_id,bam_path
# Prepare manifest.tsv via `casetrack init --manifest manifest.tsv --samples samples.txt`
nextflow run example_pipeline.nf -profile test \
    --samples_csv samples.csv \
    --casetrack_manifest $PWD/manifest.tsv
```

## Integration with Claude Code (item 9 in synopsis)

At the end of a pipeline you can chain `casetrack_append.out` into a
Claude Code process that reviews the results and appends a QC-flag column.
See `../../docs/CASETRACK_SYNOPSIS.md` Level 2 for the full pattern.
