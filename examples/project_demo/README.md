# Demo ONT tumor cohort

Synthetic example, 6 patients × 2 pre-merge BAMs each (= 12 flowcell runs).
Demonstrates the [pre-merge runs → specimen-level analyses](../patterns/premerge_runs/README.md)
pattern.

## Sheet → casetrack mapping

Sample sheet: `sample_sheet.tsv` (replace the example paths with your own
real BAM locations).

| Sheet column | Where it lands |
|---|---|
| `patient` | `patients.patient_id` (6 rows) |
| `condition` | used to synthesize `specimen_id = {patient}_{condition}` (6 rows) |
| `sample` | `assays.assay_id` + `assays.flowcell_id` (12 rows) |
| `path` | `assays.bam_path` |
| `genome` | informational; not materialized (every BAM here is hg38) |

The cohort will collapse from 12 pre-merge assays down to 6 merged
specimens via `samtools merge` — see below.

## Usage

```bash
# 1. Bootstrap the project.
python3 examples/project_demo/bootstrap.py \
    --sample-sheet examples/project_demo/sample_sheet.tsv \
    --project-dir /data1/greenbab/users/<you>/casetrack_projects/project_demo/

# 2. Follow the premerge_runs pattern — pre-merge QC, merge, downstream.
cd examples/patterns/premerge_runs/

PROJECT_DIR=/data1/.../project_demo/ \
SAMTOOLS_CONTAINER=/data1/greenbab/software/images/onttools_v3.10.sif \
CASETRACK_BIN=$(which casetrack) \
  bash submit_merge_pipeline.sh premerge_flagstat --submit

# ... wait, inspect QC ...

PROJECT_DIR=... SAMTOOLS_CONTAINER=... CASETRACK_BIN=... \
  bash submit_merge_pipeline.sh merge --submit

# ... and downstream:
PROJECT_DIR=... SAMTOOLS_CONTAINER=... CASETRACK_BIN=... \
REF_FASTA=/data1/greenbab/projects/ont/Project_demo/data/ref/GRCh38.fa \
MODKIT_CONTAINER=/data1/greenbab/software/images/onttools_v3.10.sif \
  bash submit_merge_pipeline.sh modkit_merged --submit
```

Full walk-through of the pattern lives in
[`examples/patterns/premerge_runs/README.md`](../patterns/premerge_runs/README.md).
