# Pre-merge runs → specimen-level analyses (pattern)

A reusable pattern for cohorts where each biological specimen has multiple
pre-merge BAMs (flowcell runs, lanes, resequencing, technical replicates)
that need to be merged with `samtools merge` before downstream analyses
can run.

## The pattern

```
 patient (biological subject)
   └── specimen (tumor / normal / timepoint 1 / ...)
         ├── assay  (pre-merge BAM, one per flowcell run)      ← casetrack assays
         ├── assay  (pre-merge BAM, one per flowcell run)
         └── assay  (pre-merge BAM, one per flowcell run)

 specimen also gets analysis columns attached at the specimen level:
     ├── merged_bam_path       ← set by `casetrack append --level specimen --analysis merge`
     ├── modkit_merged_done    ← set by modkit run on the merged BAM
     └── ... other post-merge analyses
```

Three casetrack-native ideas make this work:

1. **Per-assay QC at the assay level.** `casetrack append --analysis
   premerge_flagstat` auto-flags bad pre-merge BAMs via the `qc_pass` /
   `qc_fail_reason` / `qc_warn` columns on the summary TSV (proposal 0002
   §4 / §0 #4). A bad flowcell gets an active `qc_events` row with
   `source='slurm'`.
2. **The merge step queries `_active`**, not the raw `assays` table, so
   censored flowcells are automatically excluded from the merge. Censoring
   a flowcell after the merge completed → re-run `run_merge.sh` → the
   merged BAM is rebuilt without the bad flowcell, and `merged_bam_path`
   is updated.
3. **`merged_bam_path` is a specimen-level analysis column**, not a
   declared schema column. It lands via `ALTER TABLE ADD COLUMN` on the
   first `casetrack append --level specimen --analysis merge`. No TOML
   edit, no `schema apply`. Downstream tools read it from the specimens
   table, not from env vars.

## Files in this directory

```
premerge_runs/
├── run_premerge_flagstat.sh         # SBATCH: flagstat per assay + autoflag
├── run_merge.sh                     # SBATCH: _active → samtools merge → append
├── run_modkit_merged.sh             # SBATCH: modkit at specimen level
├── summarize_premerge_flagstat.py   # flagstat → per-assay TSV with qc_pass cols
├── summarize_merge.py               # merged-BAM flagstat → per-specimen TSV
├── submit_merge_pipeline.sh         # orchestrator: 3-phase fan-out with afterok deps
└── README.md                        # you are here
```

## Column naming convention (analysis-scoped)

Every specimen-level analysis in this pattern prefixes its metric columns
with its own natural scope so two analyses sharing the same specimens
table never collide under the default fill-only COALESCE semantics:

| Analysis | Writes columns |
|---|---|
| `merge` (run_merge.sh) | `merged_bam_path`, `merged_n_input_bams`, `merged_total_reads`, `merged_mapped_reads`, `merged_mapped_pct` |
| `subset_chr17` (run_subset_chr.sh, CHR=chr17) | `chr17_bam_path`, `chr17_total_reads`, `chr17_mapped_reads` |
| `modkit_merged` (BAM_COL=merged_bam_path) | `merged_n_cpg_sites`, `merged_mean_meth`, `merged_median_meth`, `merged_pct_high_conf` |
| `modkit_chr17` (BAM_COL=chr17_bam_path) | `chr17_n_cpg_sites`, `chr17_mean_meth`, `chr17_median_meth`, `chr17_pct_high_conf` |

**Why**: casetrack's default `append` semantics are fill-only (`SET col =
COALESCE(col, ?)`), which protects concurrent SLURM array tasks from racing.
But two analyses that both write a generic column like `n_cpg_sites` end up
with the first-writer's values stuck — running the full-genome modkit after
a chr17 demo will silently preserve the chr17 number. Prefixing by scope
makes this impossible by construction.

Exceptions (deliberately unprefixed): `qc_pass`, `qc_fail_reason`, `qc_warn`
— these are v0.4 autoflag columns consumed by `casetrack append` by exact
name to emit `qc_events` rows (proposal 0002 §0 #4).

### Two ways to apply the convention

| Approach | Where the prefix is applied | When it's right |
|---|---|---|
| **`casetrack append --column-prefix P`** (v0.4.1+) | casetrack rewrites the TSV header on the way in | Summarizers stay generic; prefix is declared at the append site. Less code to maintain, less risk of a summarizer author forgetting. |
| **Summarizer writes already-prefixed columns** | Per-summarizer convention | The current scripts in this directory work this way. Fine for bespoke summarizers; move to `--column-prefix` for new work. |

The two are equivalent in outcome. Mix and match freely.

## Usage — happy path

```bash
# 1. Have a casetrack project with patients/specimens/assays bootstrapped
#    from a sample sheet (pre-merge BAMs are ASSAYS with bam_path set).
#    See examples/project_17424/bootstrap.py for a worked example.

# 2. Pre-merge QC — one sbatch per assay.
PROJECT_DIR=/data1/.../cohort \
SAMTOOLS_CONTAINER=/data1/greenbab/software/images/onttools_v3.10.sif \
CASETRACK_BIN=$(which casetrack) \
  bash examples/patterns/premerge_runs/submit_merge_pipeline.sh \
    premerge_flagstat --submit

# 3. Inspect — bad flowcells will show up as active qc_events with source='slurm'.
casetrack qc-history --project-dir $PROJECT_DIR --level assay

# 4. (Optional) censor anything the autoflag missed.
casetrack censor --project-dir $PROJECT_DIR --level assay \
    --id BAD_ASSAY --kind sequencing_run_failed --reason "..."

# 5. Merge — one sbatch per specimen; queries _active so censored
#    flowcells are excluded.
PROJECT_DIR=... SAMTOOLS_CONTAINER=... \
  bash examples/patterns/premerge_runs/submit_merge_pipeline.sh \
    merge --submit

# 6. Downstream analysis at specimen level (modkit here, but extend as needed).
PROJECT_DIR=... REF_FASTA=/data1/.../hg38.fa \
MODKIT_CONTAINER=/data1/greenbab/software/images/onttools_v3.10.sif \
  bash examples/patterns/premerge_runs/submit_merge_pipeline.sh \
    modkit_merged --submit

# Or one-shot: all three phases with automatic --dependency=afterok chaining.
PROJECT_DIR=... SAMTOOLS_CONTAINER=... REF_FASTA=... MODKIT_CONTAINER=... \
  bash examples/patterns/premerge_runs/submit_merge_pipeline.sh all --submit
```

## Re-merge semantics

If a flowcell is censored *after* the merge ran, re-run phase 2:

```bash
casetrack censor --project-dir $PROJECT_DIR --level assay --id BAD_ASSAY \
    --kind contamination --reason "caught in post-merge review"

PROJECT_DIR=... bash submit_merge_pipeline.sh merge --submit
```

`run_merge.sh` overwrites the old merged BAM (`samtools merge -f`) and
the `casetrack append --level specimen --analysis merge` call refreshes
`merged_bam_path` + `merge_done`. Downstream analyses (modkit etc.)
need to be re-run too — use `casetrack rerun --level specimen --analysis
modkit_merged` to list specimens whose downstream work is stale.

## QC thresholds

Tunable via env vars; defaults reflect universal "is this flowcell at
least alive" gates:

- `MIN_TOTAL_READS` — default 1_000_000
- `MIN_MAPPED_PCT` — default 95.0

Raise per project. Values are applied both in the pre-merge flagstat
(per assay) and the merge flagstat (per merged BAM).

## Resource defaults

Tuned for MSKCC IRIS / componc_cpu / WekaFS. Change per cluster:

| Script | CPUs | Mem | Walltime |
|---|---:|---:|---|
| `run_premerge_flagstat.sh` | 2 | 4 GB | 30 min |
| `run_merge.sh` | 8 | 16 GB | 4 h |
| `run_modkit_merged.sh` | 8 | 64 GB | 8 h |

## Keeping assay-level analyses when you want them

If you also want per-flowcell methylation calls (not just per-specimen),
nothing stops you — run `examples/giab_chr21/slurm/run_modkit.sh` at
assay level against each `assays.bam_path` alongside this pattern. Same
project, same casetrack.db. The assay-level and specimen-level analyses
accumulate independently.
