# casetrack

**Lifecycle data management for computational biology pipelines on HPC.**

Two storage modes:
- **v0.3 (project mode — recommended)**: a SQLite-backed project directory with
  normalized `patient → specimen → assay` tables, enforced foreign keys, typed
  columns, and DuckDB-powered SQL queries. Survives DB corruption — everything
  is regenerable from `casetrack.toml` + `provenance.jsonl`.
- **v0.2 (flat mode — deprecated)**: one TSV manifest per project, one row per
  sample, every analysis appends columns. Still works, but prints a loud
  deprecation warning. Scheduled for removal in **v1.0** (~6 months post-v0.3).

Migrate existing flat manifests with `casetrack migrate` — see
[`docs/MIGRATION_v0.2_to_v0.3.md`](docs/MIGRATION_v0.2_to_v0.3.md).

```
cohort_v3/
├── casetrack.toml       # declared schema — git-tracked source of truth
├── casetrack.db         # SQLite, WAL + busy_timeout=30000 + FK enforcement
├── provenance.jsonl     # append-only audit log (git-trackable)
├── .gitignore           # excludes casetrack.db, casetrack.db-wal/-shm, exports/
└── sandbox/             # preserved source TSVs (migration artifact)
```

## How people actually use this

`casetrack` is a **CLI that wraps a SQLite DB**. It's installed once (globally
or per-env) and used against many projects. Three layers — keep them separate:

| Layer | Where it lives | How many | What it is |
|---|---|---|---|
| **1. `casetrack` package** | Wherever pip put it | One per env | The CLI itself — install once with `pip install casetrack` |
| **2. Casetrack projects** | Your data filesystem (`/data1/.../cohort_X/`) | Many per user, one per cohort | A directory with `casetrack.toml`, `casetrack.db`, `provenance.jsonl` |
| **3. Your pipeline code** | Your own git repo (Snakemake / Nextflow / bash / etc.) | Many per user, one per pipeline | Orchestration + summary scripts — ends each job with `casetrack append --project-dir ...` |

Users do **not** clone this repo to use casetrack — they install it once, create
project directories wherever their data lives, and call it from their own
pipeline code. The `examples/giab_chr21/` directory is a **demo and
reference** for the three-phase SLURM pattern; it's not a template you need
to copy wholesale.

Three recommended patterns by user shape:

| User shape | Where pipeline lives | What to do |
|---|---|---|
| **Researcher with their own analysis repo** | `~/projects/alice_pipeline/` | End each SLURM job with `casetrack append --project-dir ...`. Your git history is the audit trail — casetrack already records `git.commit`/`branch`/`dirty` of the CWD in every provenance entry. |
| **Team with a Nextflow / Snakemake workflow** | Shared workflow repo | Use the `casetrack_append_project` process in [`examples/nextflow/casetrack.nf`](examples/nextflow/casetrack.nf). The workflow's own git history is the audit trail. |
| **Trying the demo / kicking the tires** | `examples/giab_chr21/` | Run `bash examples/giab_chr21/run_mock_demo.sh` or copy-adapt `slurm/run_*.sh`. `submit_all.sh` snapshots the wrappers it ran into `<project>/scripts/` so a later reader can see what produced each row without needing the casetrack repo nearby. |

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

Python ≥ 3.10. Runtime deps: `pandas`, `duckdb`, `tomli` (backport on 3.10).
Optional extras: `openpyxl` (xlsx), `pyarrow` (parquet).

## Quick start — v0.3 project mode (recommended)

```bash
# 1. Create a project directory with the HGSOC schema template.
casetrack init --project-dir cohort/ --from-template hgsoc

# 2. Register a few rows (or load them in bulk via `add-metadata` / `migrate`).
casetrack register --project-dir cohort/ --level patient  --id P001 \
    --meta 'age=55,sex=F,brca_status=brca1'
casetrack register --project-dir cohort/ --level specimen --id S001 \
    --parent P001 --meta 'tissue_site=tumor'
casetrack register --project-dir cohort/ --level assay    --id A001 \
    --parent S001 --meta 'assay_type=WGS'

# 3. At the end of a SLURM job, append the per-sample summary TSV:
casetrack append --project-dir cohort/ --level assay \
    --results summary.tsv --analysis modkit_methylation

# 4. See what's done.
casetrack status    --project-dir cohort/ --group-by analysis
casetrack dashboard --project-dir cohort/ --output dashboard.html

# 5. Query across the three levels with SQL (assays⋈specimens⋈patients
#    is exposed as the view `_`).
casetrack query --project-dir cohort/ --fmt json \
    "SELECT patient_id, assay_id, mean_meth FROM _ WHERE mean_meth > 0.6"
```

## Quick start — v0.2 flat mode (deprecated)

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

> **Flat mode emits a deprecation warning.** Silence with
> `CASETRACK_NO_DEPRECATION=1` while you migrate.

## Commands

All commands accept either `--manifest PATH` (flat mode) or `--project-dir DIR`
(v0.3 project mode, recommended). The write commands log every mutation to
`provenance.jsonl` with user, SLURM job id, git commit, source file checksum,
and exact SQL.

| Command          | Purpose                                                                      |
|------------------|------------------------------------------------------------------------------|
| `init`           | Create a flat manifest OR a v0.3 project directory                           |
| `migrate`        | **v0.3** — convert a v0.2 flat manifest into a project directory             |
| `register`       | **v0.3** — insert a single row at patient/specimen/assay with strict FK      |
| `append`         | Attach analysis results (flat: columns; v0.3: ALTER TABLE ADD COLUMN)        |
| `add-metadata`   | Bulk UPDATE/INSERT metadata from a TSV (no `_done` timestamp)                |
| `status`         | Completion summary (`--group-by {analysis,assay,specimen,patient}`)          |
| `validate`       | Integrity check: TOML↔DB drift, FK violations, orphan `_done` columns        |
| `log`            | Show provenance entries (`--level L` / `--transaction TX` filters in v0.3)   |
| `schema`         | Flat: column-to-analysis map. v0.3: `{show,dump,check,apply}`                |
| `rerun`          | Emit or submit sbatch commands for rows missing an analysis                  |
| `dashboard`      | Self-contained offline HTML dashboard (v0.3: nested patients → specimens → assays) |
| `projects`       | Cross-project overview — detects both v0.2 manifests and v0.3 projects       |
| `query`          | SQL over flat manifests or v0.3 projects (DuckDB-backed; v0.3 exposes `_`)   |
| `export`         | Export to xlsx / csv / json / parquet / tsv (`--shape tables\|joined` in v0.3) |
| `doctor`         | **v0.3** — concurrency stress test on the project's filesystem               |
| `recover`        | **v0.3** — rebuild `casetrack.db` by replaying `provenance.jsonl`            |
| `censor`         | **v0.4** — record a QC failure / consent revocation on an entity             |
| `uncensor`       | **v0.4** — resolve an active qc_events row (consent reversal gated)          |
| `qc-history`     | **v0.4** — full QC event history for one entity (or all active)              |
| `migrate-qc`     | **v0.4** — one-shot: add QC schema + port a legacy `qc_pass` column          |
| `cohort`         | **v0.4** — cohort readiness summary + paired-design view (`--pair-by`)       |

`casetrack <cmd> --help` for the full option list on any subcommand.

### v0.4 QC workflow (HGSOC002 worked example)

Concrete scenario from proposal 0002 §4.5: patient HGSOC002's normal
ONT-RNA-Seq failed library prep; the matching tumor ONT-RNA passed; both
halves of HGSOC006's pair are fine. Whole-cohort readiness should surface
the broken pair so you can decide whether to drop the intact tumor half.

```bash
# 1. Flag the failed assay.
casetrack censor --project-dir hgsoc_2026 \
    --level assay --id HGSOC002-normal-ONT-RNA \
    --kind library_prep_failed \
    --reason "cDNA yield 8 ng, need >100"

# 2. A subsequent `append` on that assay now exits 2 — protects cluster hours:
casetrack append --project-dir hgsoc_2026 \
    --results modkit.tsv --analysis modkit
# Error: 1 assay(s) in modkit.tsv are censored: ['HGSOC002-normal-ONT-RNA']
#        ...
#        - If you need to land data anyway (rare): --force-append-on-censored --yes

# 3. See what's usable vs excluded.
casetrack status --project-dir hgsoc_2026 --usable
#   Usable assays: 11 / 12
#   Excluded:      1
#     QC-failed:   1   (HGSOC002-normal-ONT-RNA)

# 4. Paired-design readiness: HGSOC002 is "broken", HGSOC006 is "complete".
casetrack cohort --project-dir hgsoc_2026 \
    --assay-type ONT-RNA-Seq --pair-by tissue_site
#   PATIENT     TUMOR    NORMAL   GROUP STATUS
#   HGSOC002    pass     FAIL     broken
#   HGSOC006    pass     pass     complete

# 5. After re-sequencing, reverse the flag — uncensor is an append-only
#    write (resolved_at + resolved_by + resolved_reason on the same row).
casetrack uncensor --project-dir hgsoc_2026 \
    --level assay --id HGSOC002-normal-ONT-RNA \
    --reason "re-sequenced on 2026-04-10 batch, passes"
```

**SLURM auto-flag.** If a summary TSV contains `qc_pass` / `qc_fail_reason`
/ `qc_warn` columns, `casetrack append` consumes them and emits
`qc_events` rows inside the same transaction — no extra CLI call needed.

**Consent revocation.** `casetrack censor --level patient --kind
consent_revoked` flips the patient's `consent_status`, sets
`withdrawal_date`, and cascades exclusion at read. `casetrack uncensor`
refuses to resolve a `consent_revoked` event unless `--ethics-override
--yes` is passed AND the `--reason` mentions IRB / re-consent / an ISO
date. See `docs/MIGRATION_v0.3_to_v0.4.md` for the upgrade path and
proposal 0002 for the full design.

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
