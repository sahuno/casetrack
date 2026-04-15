# casetrack

**Manifest-centric case management for bioinformatics pipelines on HPC.**

One TSV manifest per project. One row per sample. Every analysis appends columns.
File locking makes it safe for concurrent SLURM jobs; every mutation is logged
with SLURM job id, git commit, and a file checksum.

```
sample_id   bam_path            modkit_mean_meth   modkit_done   tldr_l1_count   tldr_done
SAMPLE_01   /data/s01.bam       0.72               2026-04-14    14              2026-04-14
SAMPLE_02   /data/s02.bam       0.81               2026-04-14    3               2026-04-14
SAMPLE_03   /data/s03.bam                                        7               2026-04-14
```

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Commands](#commands)
- [The three-phase SLURM pattern](#the-three-phase-slurm-pattern)
- [Concurrency & safety rails](#concurrency--safety-rails)
- [Provenance](#provenance)
- [Nextflow integration](#nextflow-integration)
- [Claude Code integration](#claude-code-integration)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Design principles](#design-principles)

## Install

```bash
git clone https://github.com/sahuno/casetrack.git
cd casetrack

# user-level install, no sudo
pip install -e . --user

# with Excel + Parquet export
pip install -e ".[all]" --user

casetrack --help
```

Python ≥ 3.8. Depends only on `pandas`. Optional extras: `openpyxl` (xlsx), `pyarrow` (parquet).

## Quick start

```bash
# 1. Sample list
printf "SAMPLE_001\nSAMPLE_002\nSAMPLE_003\n" > samples.txt

# 2. Init a manifest (optionally with pre-existing metadata)
casetrack init --manifest manifest.tsv --samples samples.txt

# 3. Run analyses — each sbatch job ends with:
casetrack append \
    --manifest manifest.tsv \
    --results summary.tsv \
    --key sample_id \
    --analysis modkit_methylation

# 4. Check status
casetrack status --manifest manifest.tsv

# 5. Generate a browsable dashboard
casetrack dashboard --manifest manifest.tsv --output dashboard.html
```

## Commands

| Command          | Purpose                                                           |
|------------------|-------------------------------------------------------------------|
| `init`           | Create a manifest from a samples list + optional metadata TSV     |
| `append`         | Append analysis results as new columns (lock-safe, smart-merge)   |
| `add-metadata`   | Attach metadata columns post-init (clinical data, QC labels, …)   |
| `status`         | Per-analysis completion percentages (table / tsv / json)          |
| `validate`       | Integrity check: duplicate keys, nulls, orphan _done columns      |
| `log`            | Show provenance entries (who/when/SLURM id/git commit)            |
| `schema`         | Column-to-analysis mapping                                        |
| `rerun`          | Emit or submit sbatch commands for samples missing an analysis    |
| `dashboard`      | Self-contained offline HTML dashboard                             |
| `projects`       | Cross-project overview: scan a root for manifests                 |
| `export`         | Export to xlsx / csv / json / parquet                             |

`casetrack <cmd> --help` for the full option list on any subcommand.

### Representative examples

**Append analysis results**
```bash
casetrack append \
    --manifest manifest.tsv \
    --results results/modkit/SAMPLE_001/summary.tsv \
    --key sample_id \
    --analysis modkit_methylation
```
- Smart-merge: if the analysis columns already exist from a sibling SLURM task,
  NaN cells get filled in without `--overwrite`.
- `--overwrite` replaces existing values for the target columns.
- `--allow-new --yes` admits sample IDs not in the manifest. Without `--yes`,
  it previews the new IDs and refuses to commit — prevents typo'd IDs from
  silently growing the manifest.

**Attach clinical data after the fact**
```bash
casetrack add-metadata \
    --manifest manifest.tsv \
    --metadata clinical.tsv \
    --key sample_id
```
Collision policy is strict by default. Use `--fill-only` (smart merge) or
`--overwrite` to touch existing columns.

**Status**
```
$ casetrack status --manifest manifest.tsv

Manifest: manifest.tsv
Samples:  50
Columns:  18
───────────────────────────────────────────────────────
Analysis                         Done  Total       %
───────────────────────────────────────────────────────
modkit_methylation                 48     50   96.0% ████████░░
tldr_insertions                    50     50  100.0% ██████████
qc_metrics                         45     50   90.0% █████████░
───────────────────────────────────────────────────────
  Missing for modkit_methylation: SAMPLE_033, SAMPLE_047
```

**Rerun incomplete samples**
```bash
# Dry-run: print the sbatch commands
casetrack rerun --manifest manifest.tsv --analysis tldr_insertions --script run_tldr.sh

# Actually dispatch
casetrack rerun --manifest manifest.tsv --analysis tldr_insertions --script run_tldr.sh --submit

# Or just get the sample IDs
casetrack rerun --manifest manifest.tsv --analysis tldr_insertions --script ignored --list-only
```
`--submit` logs every submitted SLURM job id into the provenance, linking the
rerun action to the individual jobs that will eventually append back.

**Cross-project overview**
```
$ casetrack projects --root ~/projects/

Project            Samples  Analyses   Complete
─────────────────────────────────────────────────
alzheimers_rnaseq       60         3     91.7% █████████░
brca_immune             35         2     74.3% ███████░░░
l1_mouse_ont            24         4     74.0% ███████░░░
─────────────────────────────────────────────────
3 project(s) under /Users/ahunos/projects
```
Walks the tree up to `--max-depth 4`, skips hidden dirs and `sandbox/`, tolerates
one-off corrupted manifests (warns + continues). Output formats: table / tsv / json.

**Dashboard**
```bash
casetrack dashboard --manifest manifest.tsv --output dashboard.html
```
A single self-contained HTML file — no network, no CDN, no JavaScript libraries.
Summary metrics, per-analysis progress bars with expandable "missing samples" lists,
a sample × analysis heatmap, and a provenance timeline with short git commit hashes.
Safe to `scp` to a laptop.

## The three-phase SLURM pattern

Every analysis job follows the same three phases:

```bash
#!/bin/bash -l
#SBATCH --job-name=modkit
# ... resource directives ...

SAMPLE_ID="$1"
MANIFEST="$2"

# Phase 1: run the tool
apptainer exec container.sif modkit pileup input.bam output.bed

# Phase 2: distill to per-sample TSV (sample_id must be the first column)
python3 scripts/summarize_modkit.py \
    --input output.bed \
    --sample "$SAMPLE_ID" \
    --output summary.tsv

# Phase 3: append to manifest
casetrack append \
    --manifest "$MANIFEST" \
    --results summary.tsv \
    --key sample_id \
    --analysis modkit_methylation
```

See `examples/run_modkit.sh` for a complete worked example and
`examples/scripts/summarize_*.py` for the summarize-script contract.

## Concurrency & safety rails

- **POSIX `flock`** guards the read-merge-write cycle in `append` and `add-metadata`.
  The lock is held ~1s per invocation; parallel SLURM array tasks queue cleanly.
- **Smart-merge** fills only NaN cells when an analysis' columns already exist, so
  sibling array tasks don't fight each other.
- **`--yes` gate** on `--allow-new` — prevents typo'd sample IDs from silently
  expanding the manifest. Without `--yes`, casetrack previews the would-be adds
  and refuses to commit (exit 2).
- **Provenance-first**: every mutation logs to `manifest.tsv.provenance.jsonl`
  with user, hostname, SLURM job id + array task id, git commit / branch / dirty
  flag, and a checksum of the results file. No exceptions.
- **Validate on demand**: `casetrack validate` catches duplicate keys, null IDs,
  empty columns, orphaned `_done` timestamps without paired data columns, and
  schema/manifest drift.

## Provenance

`manifest.tsv.provenance.jsonl` is an append-only JSONL audit trail. Example entry:

```json
{
  "action": "append",
  "analysis": "modkit_methylation",
  "results_file": "summary.tsv",
  "results_checksum": "f4e1b7c9…",
  "columns_added": ["modkit_mean_meth", "modkit_done"],
  "samples_updated": 1,
  "samples_new": 0,
  "timestamp": "2026-04-15T17:03:50",
  "user": "sahuno",
  "hostname": "is01",
  "slurm_job_id": "12345",
  "slurm_array_task_id": "7",
  "git": {
    "commit": "67bba917afc…",
    "branch": "main",
    "dirty": false,
    "toplevel": "/data1/greenbab/projects/alzheimers_rnaseq"
  }
}
```

The `git` block captures the analysis pipeline's repo state at append time —
so a reviewer can ask "which commit produced this column?" six months later.
Opt out with `CASETRACK_NO_GIT=1` if a node lacks git.

## Nextflow integration

Reusable DSL2 module in `examples/nextflow/casetrack.nf`:

```groovy
include { casetrack_append } from './modules/casetrack.nf'

workflow {
    summarize_modkit(samples_ch)                       // emits (analysis, tsv)
    casetrack_append(summarize_modkit.out)             // register in manifest
}
```

See `examples/nextflow/README.md` for:
- the full override matrix (`casetrack_manifest`, `casetrack_key`, `casetrack_bin`, …)
- a ready-to-run example pipeline
- profiles for `standard` / `slurm` / `apptainer` / `test`
- three integration patterns (standalone, `afterScript`, collect-and-batch)

## Claude Code integration

Level 2 hook in `examples/claude/`: after a SLURM analysis finishes and calls
`casetrack append`, a companion shell script invokes `claude --print`, captures a
QC review as a second TSV, validates its shape, and appends it back as its own
analysis (`cc_<analysis>_review`) — so the manifest carries both the raw numbers
and an LLM verdict, fully traceable.

```bash
export SAMPLE_ID ANALYSIS=modkit MANIFEST RESULTS_TSV=summary.tsv
bash /path/to/examples/claude/post_analysis_hook.sh
```

Full contract, exit codes, and prompt customization in `examples/claude/README.md`.

## Project layout

```
project/
├── manifest.tsv                    # single source of truth
├── manifest.tsv.provenance.jsonl   # audit trail
├── manifest.tsv.schema.json        # column-to-analysis mapping
├── manifest.tsv.lock               # transient POSIX lock
├── samples.txt
├── results/
│   ├── modkit/{sample_id}/
│   ├── tldr/{sample_id}/
│   └── qc/{sample_id}/
├── scripts/
│   ├── summarize_modkit.py         # distils raw output → manifest columns
│   ├── summarize_tldr.py
│   └── summarize_qc.py
├── logs/                           # SLURM logs
└── containers/                     # Apptainer .sif files
```

Repo layout:

```
casetrack/
├── casetrack.py          # all CLI commands (single-file)
├── setup.py              # pip entry_points = [casetrack]
├── README.md             # you are here
├── docs/                 # design synopsis + architecture SVG
├── examples/
│   ├── run_modkit.sh
│   ├── scripts/          # summarize_modkit.py, summarize_tldr.py
│   ├── nextflow/         # DSL2 module + demo pipeline + config
│   └── claude/           # post-analysis QC hook + prompt template
├── tests/                # 152 pytest tests
└── sandbox/              # non-tracked build leftovers (gitignored)
```

## Testing

```bash
pip install --user pytest openpyxl pyarrow
python3 -m pytest tests/ -q
```

Suite covers: helpers (locking, provenance, checksums, schema), every subcommand
(`init` / `append` / `add-metadata` / `status` / `validate` / `log` / `schema` /
`rerun` / `dashboard` / `projects` / `export`), smart-merge correctness and
5000-sample perf regression, concurrent append via `multiprocessing`, git
provenance with a real tmp repo, the Nextflow module's shell contract
(extract-and-execute), and the Claude Code hook (with a stubbed `claude` on PATH).

## Design principles

1. **TSV-first.** Human-readable, git-diffable, works with awk / csvtk / pandas. No database.
2. **The manifest is the source of truth.** If it's not in the manifest, it didn't happen.
3. **Append-only columns, fill-only cells by default.** Mutations require an explicit flag (`--overwrite`).
4. **Convention over configuration.** Every analysis gets a `{analysis}_done` timestamp column — that's what `status` reads.
5. **Summarize scripts are the contract.** Each analysis has a small Python script that produces the per-sample TSV; full raw output stays in per-sample dirs.
6. **One manifest per project.** No shared global state. `casetrack projects` aggregates across projects without coupling them.
7. **Safety rails are explicit.** `flock` for concurrency, `--yes` for row growth, provenance for every mutation.

## License

MIT
