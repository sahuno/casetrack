# Casetrack: development synopsis for coding agent

## What this document is

This is a design synopsis for **casetrack**, a manifest-centric case management CLI tool for bioinformatics pipelines running on HPC (SLURM). It is intended to be read by a coding agent (Claude Code) that will develop the project further. The working prototype already exists — this document captures the full design intent, architecture decisions, known issues, and planned features so the agent can continue development with full context.

## Problem statement

A computational biologist runs dozens of analyses per project on an HPC cluster (IRIS at MSKCC, SLURM-based). Pipelines include Nextflow workflows, ad hoc SLURM scripts, and Claude Code agent sessions. Results end up scattered across directories with no unified tracking of what has been computed for which sample. There is no single source of truth for project completion status.

The inspiration comes from the Broad Institute's Firehose/FireCloud/Terra lineage — systematic cancer analysis pipelines where every sample passes through a defined set of analyses and results are tracked centrally. Casetrack is the lightweight, local-first version of this pattern.

## Core concept

One TSV manifest per project. Each row is a sample. Each analysis appends new columns. The manifest only grows rightward — it is append-only for columns and fill-only for cells. Every pipeline, script, or agent calls the same CLI tool (`casetrack`) at the end to register its results.

```
sample_id  bam_path       modkit_mean_meth  modkit_done   tldr_l1_count  tldr_done
SAMPLE_01  /data/s01.bam  0.72              2026-04-14    14             2026-04-14
SAMPLE_02  /data/s02.bam  0.81              2026-04-14    3              2026-04-14
SAMPLE_03  /data/s03.bam                                  7              2026-04-14
```

## Architecture

### File layout per project

```
project/
├── manifest.tsv                    # The single source of truth
├── manifest.tsv.provenance.jsonl   # Audit trail (who ran what, when, SLURM job ID)
├── manifest.tsv.schema.json        # Column-to-analysis mapping
├── manifest.tsv.lock               # POSIX flock file (transient)
├── samples.txt                     # Sample ID list
├── results/
│   ├── modkit/{sample_id}/         # Full output per sample per analysis
│   ├── tldr/{sample_id}/
│   └── qc/{sample_id}/
├── scripts/
│   ├── summarize_modkit.py         # Distills raw output → manifest columns
│   ├── summarize_tldr.py
│   └── summarize_qc.py
├── logs/                           # SLURM logs
└── containers/                     # Apptainer/Singularity .sif files
```

### Multi-project model

Each project has its own independent manifest. No shared state between projects. The same `casetrack` CLI works on whichever `--manifest` path you point it at. Example projects that would each have their own manifest:

- `alzheimers_rnaseq/` — 60 patients, analyses: star_alignment, deseq2_de, age_prediction, pathway_gsea
- `brca_immunopeptidome/` — 35 samples, analyses: maxquant_ms, netmhcpan, hla_typing, gibbscluster
- `l1_mouse_ont/` — 24 samples, analyses: dorado_basecalling, minimap2_alignment, modkit_methylation, tldr_insertions, xtea_somatic_l1

A future `casetrack projects` command could scan for manifests under a root directory and aggregate status, but this is a convenience layer, not the core design.

### The three-phase SLURM pattern

Every SLURM job follows this structure:

```bash
# Phase 1: Run analysis
apptainer exec container.sif tool input output

# Phase 2: Summarize to per-sample TSV
python3 scripts/summarize_tool.py --input output --sample $SAMPLE_ID --output summary.tsv
# Contract: emit a TSV with sample_id as the first column

# Phase 3: Append to manifest
casetrack append --manifest manifest.tsv --results summary.tsv --key sample_id --analysis tool_name
```

### Concurrency model

Multiple SLURM array tasks can finish simultaneously. `casetrack append` uses POSIX `flock` for exclusive file locking during the read-merge-write cycle. The lock is held only during I/O (typically <1 second). Each task writes to its own per-sample results directory, then contends only on the manifest append.

### Smart merge behavior

When a SLURM array job runs, the first task to finish creates the new columns in the manifest. Subsequent tasks find those columns already exist but with NaN values for their sample. The smart merge logic detects this and fills in NaN cells without requiring `--overwrite`. This is critical for the array job pattern. The `--overwrite` flag is reserved for when you genuinely want to replace existing data.

## Current implementation

### Existing CLI commands

The prototype is a single Python file (`casetrack.py`) with these subcommands:

| Command | Status | Description |
|---------|--------|-------------|
| `init` | Working | Create manifest from samples.txt + optional metadata TSV |
| `append` | Working | Append analysis results with file locking and smart merge |
| `status` | Working | Show completion (table/tsv/json formats, progress bars) |
| `validate` | Working | Check integrity: duplicate keys, null IDs, schema consistency |
| `log` | Working | Show provenance entries (SLURM job IDs, timestamps, user) |
| `schema` | Working | Show which analysis added which columns |
| `export` | Working | Export to xlsx, csv, json, parquet |

### Dependencies

- Python >= 3.8
- pandas >= 1.5.0
- Optional: openpyxl (Excel export), pyarrow (Parquet export)
- No other dependencies. Designed for HPC environments where installing packages is constrained.

### Installation

```bash
pip install -e . --user
# or
pip install -e ".[all]" --user  # with Excel + Parquet support
```

### Known issues in prototype

1. The `append` smart merge uses iterrows() which is slow for large manifests (>1000 samples). Should use vectorized pandas merge/update instead.
2. No `casetrack rerun` command yet — users must manually generate sbatch commands for missing samples.
3. No `casetrack dashboard` command for HTML visualization.
4. No `casetrack projects` command for cross-project overview.
5. Provenance log doesn't track the git commit hash of the analysis script.
6. No support for sample-level metadata updates after init (e.g., adding a new metadata column).
7. The `--allow-new` flag on append could accidentally introduce typos as new samples. Should have a confirmation prompt or require exact match against a whitelist.
8. No tests.

## Planned features (priority order)

### 1. `casetrack rerun` command

Read status, identify incomplete samples for a given analysis, and generate (or submit) SLURM commands.

```bash
# Show what needs re-running
casetrack rerun --manifest manifest.tsv --analysis tldr_insertions --script run_tldr.sh

# Output:
#   sbatch run_tldr.sh MC_TUMOR_003 manifest.tsv
#   sbatch run_tldr.sh MC_TUMOR_017 manifest.tsv
#   sbatch run_tldr.sh MC_NORMAL_009 manifest.tsv

# Actually submit
casetrack rerun --manifest manifest.tsv --analysis tldr_insertions --script run_tldr.sh --submit
```

### 2. `casetrack dashboard` command

Generate a self-contained HTML file from the manifest. No server required — just open in a browser or scp to local machine.

```bash
casetrack dashboard --manifest manifest.tsv --output dashboard.html
```

The dashboard should show: summary metrics (samples, columns, overall completion), per-analysis progress bars with completion percentages, per-sample heatmap grid (click an analysis to see which samples are done/missing), and provenance timeline.

### 3. Tests

Unit tests for core logic: smart merge, file locking, provenance logging, schema tracking, validation. Integration test: init → append multiple analyses → verify manifest state. Concurrency test: simulate multiple simultaneous appends.

### 4. Performance optimization

Replace iterrows() in smart merge with vectorized update. For manifests with >100 columns, consider SQLite backend with TSV export.

### 5. `casetrack add-metadata` command

Add new metadata columns to an existing manifest from a TSV file, without going through the analysis append path.

```bash
casetrack add-metadata --manifest manifest.tsv --metadata new_clinical_data.tsv --key sample_id
```

### 6. `casetrack projects` command

Scan for manifests under a root directory and show cross-project overview.

```bash
casetrack projects --root ~/projects/
# Output:
#   alzheimers_rnaseq     60 samples   5 analyses   87% complete
#   brca_immunopeptidome  35 samples   4 analyses   62% complete
#   l1_mouse_ont          24 samples   7 analyses   91% complete
```

### 7. Git integration in provenance

Log the git commit hash of the project repo (if available) alongside each append entry. This creates a link between manifest state and code version.

### 8. Nextflow integration

A Nextflow process or `publishDir` hook that automatically calls `casetrack append` when a process completes. This would be a small Nextflow module or a `afterScript` directive.

### 9. Claude Code integration patterns

Three levels of integration, from simple to autonomous:

**Level 1: Interactive CLI** — Claude Code reads manifest status and generates commands in conversation.

**Level 2: Post-analysis hook** — A shell hook that runs Claude Code after each SLURM job to review results and append QC flag columns (cc_analysis_qc_pass, cc_analysis_qc_note).

```bash
# At end of SLURM job, after casetrack append:
claude --print \
  "Read results for sample ${SAMPLE_ID}. Evaluate QC metrics.
   Write cc_review.tsv with sample_id, cc_${ANALYSIS}_qc_pass, cc_${ANALYSIS}_qc_note."
casetrack append --manifest ${MANIFEST} --results cc_review.tsv --key sample_id --analysis cc_${ANALYSIS}_review
```

**Level 3: SDK agent loop** — A Python script using the Claude Code SDK that reads manifest gaps, plans work, submits SLURM jobs, monitors completion, and appends results autonomously.

## Design principles

1. **TSV-first**: Human-readable, git-diffable, works with awk/csvtk/pandas. No database required.
2. **Manifest is the source of truth**: If it's not in the manifest, it didn't happen.
3. **Append-only columns**: The manifest only grows rightward. Analyses never remove columns (except with explicit --overwrite).
4. **Convention: _done columns**: Every analysis appends a `{analysis}_done` timestamp column alongside its data columns. This is what `casetrack status` reads.
5. **Summarize scripts are the contract**: Each analysis has a small Python script that distills raw tool output into the few columns that belong in the manifest. Full results stay in per-sample directories.
6. **One manifest per project**: No shared global state. Each project is fully independent.
7. **File locking for concurrency**: POSIX flock, held only during read-merge-write. No database, no server.
8. **Provenance by default**: Every append logs who, when, what, from which SLURM job, with file checksums.

## Environment context

- HPC cluster: IRIS at MSKCC, SLURM scheduler
- Containers: Apptainer/Singularity
- Primary user: computational biologist running ONT long-read sequencing, DNA methylation, L1 retrotransposon, and cancer genomics analyses
- Tools in use: modkit, TLDR, xTea, minimap2, Dorado, Snakemake, Nextflow, samtools
- Python environment: typically conda or module-loaded, limited pip install permissions (use --user or --break-system-packages)
- Claude Code SDK available on cluster for agentic workflows

## Repository structure

```
casetrack/
├── casetrack.py          # Main CLI (single file, all commands)
├── setup.py              # pip-installable with entry_points
├── README.md             # Usage documentation
├── examples/
│   ├── run_modkit.sh     # Example SLURM script (three-phase pattern)
│   └── scripts/
│       ├── summarize_modkit.py   # Example: modkit pileup → manifest columns
│       └── summarize_tldr.py     # Example: TLDR table → manifest columns
└── tests/                # (to be created)
```

## What to build next

The recommended order for a coding agent picking this up:

1. Write tests for existing functionality (init, append, status, validate, smart merge, file locking)
2. Fix the iterrows() performance issue in smart merge
3. Implement `casetrack rerun`
4. Implement `casetrack dashboard` (static HTML generation)
5. Implement `casetrack add-metadata`
6. Implement `casetrack projects`
7. Add git commit hash to provenance logging
8. Write a Nextflow integration module

The existing `casetrack.py` is the starting point. All code is in one file for simplicity — it can be refactored into a package structure (`casetrack/cli.py`, `casetrack/manifest.py`, `casetrack/provenance.py`, etc.) when complexity warrants it, but single-file is fine for now.
