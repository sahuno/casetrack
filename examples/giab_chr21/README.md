# GIAB chr21 — casetrack end-to-end example

A real-data demo of casetrack on the Genome-in-a-Bottle (GIAB) ONT reference
cohort, restricted to chr21 so the BAMs are small enough for interactive use.

## What's here

```
giab_chr21/
├── sample_sheet.tsv            → ../../../GIAB_ont/... (symlink)
├── bootstrap.py                # register sample sheet into a v0.3 project
├── run_mock_demo.sh            # path 1: fast mock demo, no cluster needed
├── slurm/
│   ├── run_flagstat.sh         # three-phase SLURM job for samtools flagstat
│   ├── run_modkit.sh           # three-phase SLURM job for modkit methylation
│   └── submit_all.sh           # fan out one job per (analysis × assay)
└── scripts/
    ├── summarize_flagstat.py   # flagstat → per-assay TSV (real)
    ├── summarize_modkit.py     # bedMethyl → per-assay TSV (real)
    ├── mock_modkit_summary.py  # deterministic fake methylation summary
    └── mock_sniffles_summary.py# deterministic fake SV summary
```

The sample sheet has 4 rows — two GIAB samples (HG002, HG006) × two ONT
flowcells each. All BAMs are restricted to chr21.

## Quick start — path 1 (mock demo, no cluster)

Populates a project end-to-end in under a minute. No actual analysis tooling
needed. Good for: reviewing the CLI surface, demoing the dashboard, wiring
up CI.

```bash
bash examples/giab_chr21/run_mock_demo.sh /tmp/giab_demo/
```

This will:

1. `casetrack init --project-dir /tmp/giab_demo/ --from-template giab_ont`
2. Register 2 patients × 2 specimens × 4 assays from `sample_sheet.tsv`.
3. Synthesize deterministic flagstat / modkit / sniffles summary TSVs.
4. `casetrack append` each one at the assay level.
5. Emit `status`, `dashboard /tmp/giab_demo/dashboard.html`, and an example
   joined SQL query.

## Quick start — path 2 (real SLURM pipeline on MSKCC IRIS)

Runs the real analyses on the real BAMs. Uses the three-phase SLURM pattern
(tool → summarize → casetrack append) — each job is independent and safe
under the WAL + BEGIN IMMEDIATE concurrency model.

```bash
# Bootstrap the project (one-time).
python3 examples/giab_chr21/bootstrap.py \
    --sample-sheet examples/giab_chr21/sample_sheet.tsv \
    --project-dir /data1/greenbab/users/<you>/giab_demo/

# Dry-run the fan-out (prints sbatch commands, no dispatch).
PROJECT_DIR=/data1/greenbab/users/<you>/giab_demo/ \
    bash examples/giab_chr21/slurm/submit_all.sh

# Dispatch the flagstat runs.
PROJECT_DIR=/data1/greenbab/users/<you>/giab_demo/ \
    bash examples/giab_chr21/slurm/submit_all.sh --submit

# Add modkit once you have a reference FASTA handy.
ANALYSES="flagstat modkit" \
REF_FASTA=/data1/greenbab/projects/databases/hg38/hg38.fa \
PROJECT_DIR=/data1/greenbab/users/<you>/giab_demo/ \
    bash examples/giab_chr21/slurm/submit_all.sh --submit

# As jobs finish, check progress:
casetrack status --project-dir /data1/greenbab/users/<you>/giab_demo/
```

Environment assumptions for path 2:
- `samtools` and `casetrack` on `PATH` (or `SAMTOOLS_BIN` / `CASETRACK_BIN`
  set). For modkit either install it natively or point `MODKIT_CONTAINER` at
  the onttools apptainer image from
  `profiles/software_configs/softwares_containers_config.yaml`.
- `REF_FASTA` is required when `modkit` is in `ANALYSES`.
- The chr21 BAMs still need MM/ML tags for modkit to produce anything
  meaningful — the original GIAB chr21 BAMs should have them from dorado.

## Schema — `giab_ont` TOML template

v0.3.0 ships with a `giab_ont` template optimized for ONT reference cohorts:

- **patient**: `patient_id`, `sex`, `reference_source` (NIST/GIAB),
  `trio_role` (proband/father/mother/unrelated), `cohort`
- **specimen**: `specimen_id`, `specimen_type` (`whole_genome_dna` by
  default), `cell_line`, `source`
- **assay**: `assay_id`, `assay_type` (ONT enum), `flowcell_id`,
  `chemistry` (R9/R10 variants), `basecaller_model`, `bam_path`,
  `condition`, `qc_pass`

Analysis columns are added dynamically by `casetrack append`.

## Verifying a real run

```bash
casetrack query --project-dir /path/to/project "
    SELECT patient_id, assay_id,
           mapped_pct, mean_meth, n_svs_total
    FROM _ ORDER BY assay_id"
```

You should see four rows with mapped_pct > 90% (real chr21 BAMs), non-zero
mean_meth if modkit ran successfully, and a few hundred SVs if sniffles ran.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `bootstrap.py` complains about missing columns | Your sheet schema differs from GIAB's — pass `--sample-sheet` pointing at a TSV with `patient_id`, `sample_id`, `assay_type`, `bam_path` columns. |
| `casetrack append ... exit 2 ... keys do not exist` | The bootstrap never ran; re-run `bootstrap.py` before submitting analysis jobs. |
| modkit produces an empty bedMethyl | BAM lacks MM/ML tags. Re-basecall with a modification-aware dorado model. |
| Job hangs on BEGIN IMMEDIATE | Another writer has the lock — BEGIN IMMEDIATE will retry up to 30s per the `busy_timeout` pragma, then raise. Run `casetrack doctor` on a fresh FS to verify lock semantics before submitting a big array. |
