# Nextflow integration — `CASETRACK_REGISTER`

casetrack integrates with Nextflow via a single reusable module that closes the 3-phase pattern:
run tool → summarize → append to casetrack DB.

## The canonical module

`modules/local/casetrack_register.nf` (shipped by `casetrack-nf-subworkflows`):

```groovy
process CASETRACK_REGISTER {
    tag "${tool}:${meta.id}"
    executor 'local'          // SQLite WAL + busy_timeout; never run on SLURM
    maxForks 1                // serialize writes for readable provenance logs
    errorStrategy 'retry'
    maxRetries 2

    input:
    tuple val(meta), val(tool), val(summary_name), path(summary_tsv)

    output:
    tuple val(meta), val(tool), emit: ok

    script:
    def bin     = params.casetrack_bin ?: 'casetrack'
    def proj    = params.casetrack_project_dir
    def run_tag = params.run_tag
    def level   = params.casetrack_level ?: 'assay'
    if (!proj)    error "params.casetrack_project_dir is required"
    if (!run_tag) error "params.run_tag is required"
    def leaf = _resolve_leaf(proj, tool, run_tag, level, meta)
    """
    set -euo pipefail
    LEAF="${leaf}"
    mkdir -p "\$LEAF"
    cp -f "${summary_tsv}" "\$LEAF/${summary_name}"
    cd "\$LEAF"
    ${bin} append --infer-from-path --overwrite
    """
}

def _resolve_leaf(proj, tool, run_tag, level, meta) {
    if (level == 'assay')
        return "${proj}/results/${tool}/${run_tag}/${meta.patient}/${meta.specimen}/${meta.assay_id}"
    else if (level == 'specimen')
        return "${proj}/results/${tool}/${run_tag}/${meta.patient}/${meta.specimen}"
    else if (level == 'patient')
        return "${proj}/results/${tool}/${run_tag}/${meta.patient}"
    else
        error "params.casetrack_level must be one of: assay, specimen, patient (got '${level}')"
}
```

**Why `executor 'local'` + `maxForks 1`:** casetrack serializes its own writes through SQLite WAL + `busy_timeout`, but running one coordinator at a time produces readable provenance logs and avoids SLURM queue overhead for a sub-second task.

**Why `--infer-from-path --overwrite`:** the TSV is placed at the canonical leaf path (derived from `[layout.path_templates]`), so casetrack recovers tool/run_tag/patient/specimen/assay from the directory. `--overwrite` is mandatory — without it, reruns silently no-op.

## Subworkflow pattern

Wrap an nf-core or local module with `CASETRACK_REGISTER`:

```groovy
include { SAMTOOLS_SORT      } from '../../modules/nf-core/samtools/sort/main'
include { SUMMARIZE_SORT     } from '../../modules/local/summarize_sort'
include { CASETRACK_REGISTER } from '../../modules/local/casetrack_register'

workflow SAMTOOLS_SORT_TRACKED {
    take:
    ch_bam    // tuple(meta, bam)

    main:
    SAMTOOLS_SORT(ch_bam, ...)
    SUMMARIZE_SORT(SAMTOOLS_SORT.out.bam)    // emits (meta, summary_tsv) with specimen_id + stats
    CASETRACK_REGISTER(
        SUMMARIZE_SORT.out.summary.map { meta, tsv ->
            tuple(meta, 'samtools_sort', 'samtools_sort_summary.tsv', tsv)
        }
    )

    emit:
    bam = SAMTOOLS_SORT.out.bam
    ok  = CASETRACK_REGISTER.out.ok
}
```

The 3rd element (`'samtools_sort_summary.tsv'`) must exactly match `[analyses.samtools_sort].summary_tsv` in the project's `casetrack.toml`. The 2nd element (`'samtools_sort'`) must match the TOML analysis key.

## Required pipeline parameters

Every NF run that uses `CASETRACK_REGISTER` must provide:

```
--casetrack_project_dir   absolute path to the casetrack project (must live OUTSIDE NF work dir)
--run_tag                 {YYYYMMDD}_{genome}_{description} e.g. 20260421_hg38_normal_basecalling
--casetrack_level         assay | specimen | patient (must match [analyses.<tool>].level)
--casetrack_bin           optional — defaults to `casetrack` on PATH
```

## Summarize module contract

Every `SUMMARIZE_*` module emits a one-row TSV keyed on the level's ID column. The columns are:

1. **key column** (`patient_id`, `specimen_id`, or `assay_id`) — must be first
2. **result columns** — data you want in the DB. Names (after prefixing with `column_prefix` from TOML) will be the DB column names
3. **optional QC columns**: `qc_pass`, `qc_fail_reason`, `qc_warn` — if present, casetrack auto-emits QC events in the same transaction as the append (v0.4 autoflag)

Example (`samtools_sort_summary.tsv`, single row):
```tsv
specimen_id       sorted_bam_path                                                              sorted_bam_size_bytes  n_reads   sort_order
p17424_1_tumor    /data/processed/hg38/p17424_1/p17424_1_tumor/p17424_1_tumor.hg38.sorted.bam  85134617284            142593118 coordinate
```

After `casetrack append --analysis samtools_sort --overwrite`, the DB gets:
- `sort_sorted_bam_path`
- `sort_sorted_bam_size_bytes`
- `sort_n_reads`
- `sort_sort_order`
- `samtools_sort_done` (auto)

## `data/processed/` publishDir convention

Primary biological outputs (BAMs, VCFs) should be published to `{project_dir}/data/processed/{genome}/{patient_id}/{assay_id}/` with genome-tagged filenames, so that:
- Files survive `nxf_work/` cleanup
- The DB can store stable absolute paths in `bam_path`, `sorted_bam_path`, etc.
- Downstream tools find files by DB query, not filesystem scan

```groovy
process {
    withName: 'SAMTOOLS_SORT_TRACKED:SAMTOOLS_SORT' {
        publishDir = [
            path:   { "${params.casetrack_project_dir}/data/processed/${meta.genome}/${meta.patient}/${meta.id}" },
            mode:   'copy',
            saveAs: { fn -> fn.endsWith('.bam') ? "${meta.id}.${meta.genome}.sorted.bam" : null }
        ]
    }
}
```

The summarize module should record this *persistent* path — not the ephemeral NF work path:

```python
# Inside summarize_sort.py:
DEST = f"{project_dir}/data/processed/{genome}/{patient}/{assay_id}/{assay_id}.{genome}.sorted.bam"
# ... write DEST to the summary TSV, not a readlink on the work-dir symlink
```

## Common integration pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| `casetrack_project_dir` inside NF work dir | DB wiped between runs | Use path OUTSIDE `work/`, ideally on shared storage |
| Wrong `summary_tsv` name in `CASETRACK_REGISTER` call | `no such file` during `--infer-from-path` | Must match `[analyses.<name>].summary_tsv` exactly |
| Wrong `nf_process` in TOML | L2 trace import misses the rule | Must match the NF process name as it appears in `trace.txt` |
| Missing `--overwrite` in CASETRACK_REGISTER | DB stays stale on rerun | Already baked into the canonical module — if you fork it, keep it |
| `CASETRACK_REGISTER` on SLURM | Queue wait + DB lock thrashing | Always `executor 'local'` |
| Summary TSV writes ephemeral nxf_work path | DB path breaks after cleanup | Summarize module must construct the `data/processed/` path |
