# casetrack

**Lifecycle data management for computational biology pipelines on HPC.**

Answers two questions about a multi-patient, multi-specimen, multi-assay
cohort: "*is this analysis complete?*" and "*is this sample usable?*"

Three storage layers, one CLI:
- **v0.4 (current)**: the v0.3 project backend + a QC / censoring / consent
  subsystem. Every read path (`status`, `rerun`, `export`, `query`,
  `dashboard`) filters out QC-failed and consent-revoked entities by
  default. SLURM summary TSVs can auto-flag via `qc_pass` /
  `qc_fail_reason` / `qc_warn` columns. Paired-design readiness via
  `casetrack cohort --pair-by`.
- **v0.3 (project mode)**: a SQLite-backed project directory with
  normalized `patient → specimen → assay` tables, enforced foreign keys,
  typed columns, and DuckDB-powered SQL queries. Survives DB corruption —
  everything is regenerable from `casetrack.toml` + `provenance.jsonl`.
- **v0.2 (flat mode — deprecated)**: one TSV manifest per project, one
  row per sample. Still works, loud deprecation warning, removed in **v1.0**.

Upgrade paths:
`v0.2 → v0.3` via `casetrack migrate` ([guide](docs/MIGRATION_v0.2_to_v0.3.md)).
`v0.3 → v0.4` via `casetrack migrate-qc` ([guide](docs/MIGRATION_v0.3_to_v0.4.md)).

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
- [Quick start — v0.3 project mode (recommended)](#quick-start--v03-project-mode-recommended)
- [Quick start — v0.2 flat mode (deprecated)](#quick-start--v02-flat-mode-deprecated)
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
casetrack register --project-dir cohort/ --level patient  --id HGSOC002 \
    --meta 'age=55,sex=F,brca_status=brca1'
casetrack register --project-dir cohort/ --level specimen --id HGSOC002-normal \
    --parent HGSOC002 --meta 'tissue_site=normal'
casetrack register --project-dir cohort/ --level assay    --id HGSOC002-normal-ONT-RNA \
    --parent HGSOC002-normal --meta 'assay_type=ONT'

# 3. At the end of a SLURM job, append the per-sample summary TSV.
#    If the TSV has qc_pass=False, casetrack auto-emits a qc_events row
#    in the same transaction — no extra CLI call needed (see §SLURM pattern).
casetrack append --project-dir cohort/ --level assay \
    --results summary.tsv --analysis modkit_methylation

# 4. Flag a bad assay manually (library prep failed, contamination, etc.).
casetrack censor --project-dir cohort/ \
    --level assay --id HGSOC002-normal-ONT-RNA \
    --kind library_prep_failed --reason "cDNA yield 8 ng, need >100"

# 5. See what's complete AND usable. `--usable` adds the exclusion breakdown.
casetrack status    --project-dir cohort/ --usable
casetrack dashboard --project-dir cohort/ --output dashboard.html

# 6. Cohort readiness — paired designs surface broken pairs.
casetrack cohort --project-dir cohort/ \
    --assay-type ONT-RNA-Seq --pair-by tissue_site

# 7. Query across the three levels with SQL. `_` is the raw join;
#    `_active` applies the §4.4 cascade (QC + consent) automatically.
casetrack query --project-dir cohort/ --fmt json \
    "SELECT patient_id, assay_id, mean_meth FROM _active WHERE mean_meth > 0.6"
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
PROJECT_DIR="$2"

# Phase 1: run the tool
apptainer exec container.sif modkit pileup input.bam output.bed

# Phase 2: distill to per-sample TSV.
#   Required:   assay_id (first column)
#   Optional:   qc_pass / qc_fail_reason / qc_warn — v0.4 autoflag. If the
#               summarizer decides the whole assay is unusable, emit qc_pass=False
#               (+ optional qc_fail_reason). casetrack append turns that into a
#               qc_events row inside the same transaction as the data update.
python3 scripts/summarize_modkit.py \
    --input output.bed \
    --assay "$SAMPLE_ID" \
    --output summary.tsv

# Phase 3: append to project. If the target assay is already censored,
# this exits 2 unless you pass --force-append-on-censored --yes.
casetrack append \
    --project-dir "$PROJECT_DIR" \
    --results summary.tsv \
    --analysis modkit_methylation
```

Example summary TSV with autoflag columns:

```
assay_id                    modkit_mean_meth   qc_pass   qc_fail_reason
HGSOC002-tumor-ONT-RNA      0.72               True
HGSOC002-normal-ONT-RNA                        False     library prep failed (cDNA yield 8 ng)
```

The `qc_pass` / `qc_fail_reason` / `qc_warn` columns are **consumed** by
`casetrack append` — they never become analysis columns on `assays`.

See `examples/run_modkit.sh` for a complete worked example and
`examples/scripts/summarize_*.py` for the summarize-script contract.

## Concurrency & safety rails

- **SQLite WAL + `busy_timeout=30000`** in project mode (`BEGIN IMMEDIATE`
  envelopes every mutation). **POSIX `flock`** in flat mode. Parallel
  SLURM array tasks queue cleanly on both.
- **Smart-merge** fills only NaN cells by default, so sibling array tasks
  don't fight each other.
- **Three `--yes` double opt-ins** — every one protects against a different
  kind of silent data loss or bad-data admit:
  - `--allow-new --yes` on `append` / `add-metadata` — admit row IDs that
    aren't already in the project.
  - `--force-append-on-censored --yes` on `append` (v0.4) — land data on
    an entity whose `qc_status` is `fail` / `censored` / `consent_revoked`.
  - `--ethics-override --yes` on `uncensor` (v0.4) — reverse a
    `consent_revoked` event. The `--reason` must additionally mention an
    IRB ref / re-consent phrasing / an ISO date, so a later auditor can
    grep `provenance.jsonl` for every ethics transaction.
- **Provenance-first**: every mutation logs to `provenance.jsonl` with
  user, hostname, SLURM job id + array task id, git commit / branch /
  dirty flag, and a checksum of the results file. v0.4 adds `censor`,
  `uncensor`, `ethics_override`, `migrate_qc` actions to the schema.
- **`casetrack validate`** catches TOML↔DB drift, FK orphans, orphan
  `_done` timestamps, consent invariant violations, and `qc_status` ↔
  active-events mismatch.
- **`casetrack recover`** rebuilds `casetrack.db` byte-equivalent from
  provenance alone — the DB is never the source of truth.

## Provenance

Project mode writes to `<project>/provenance.jsonl`; flat mode to
`<manifest>.provenance.jsonl`. Both are append-only JSONL audit trails.
Example v0.4 entry — a SLURM job that appended modkit results and auto-
flagged a failed assay in the same transaction:

```json
{
  "action": "censor",
  "level": "assay",
  "entity_id": "HGSOC002-normal-ONT-RNA",
  "kind": "qc_fail",
  "reason": "library prep failed (cDNA yield 8 ng)",
  "source": "slurm",
  "created_by": "slurm:12345",
  "transaction_id": "txn_20260417T090312_a1b2c3",
  "qc_event_id": 42,
  "new_qc_status": "fail",
  "from_analysis": "modkit_methylation",
  "timestamp": "2026-04-17T09:03:12",
  "user": "sahuno",
  "hostname": "is01",
  "slurm_job_id": "12345",
  "git": {
    "commit": "67bba917afc…",
    "branch": "main",
    "dirty": false,
    "toplevel": "/data1/greenbab/projects/hgsoc_2026"
  }
}
```

The `git` block captures the analysis pipeline's repo state at write time —
so a reviewer can ask "which commit produced this column / this QC flag?"
six months later. Ethics-sensitive actions (`consent_revoked` censors and
`ethics_override` uncensors) additionally carry `"ethics": true`, so a
single `grep` surfaces every consent transaction for audit. Opt out of
the `git` block with `CASETRACK_NO_GIT=1` if a node lacks git. Every
action — including QC — is replayable via `casetrack recover`.

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

The `casetrack_append_project` process inherits v0.4's strict-refuse +
autoflag automatically — nothing to configure. If a summarize step emits a
TSV with `qc_pass=False`, the append step in the pipeline turns it into a
`qc_events` row in the same transaction.

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

A v0.4 project directory:

```
cohort_v4/
├── casetrack.toml                  # declared schema (git-tracked)
├── casetrack.db                    # SQLite; WAL + FK enforcement (gitignored)
├── provenance.jsonl                # append-only audit log (git-trackable)
├── .gitignore                      # excludes casetrack.db + -wal/-shm + exports/
├── results/
│   ├── modkit/{assay_id}/
│   ├── tldr/{assay_id}/
│   └── qc/{assay_id}/
├── scripts/
│   ├── summarize_modkit.py         # distils raw output → assay TSV
│   ├── summarize_tldr.py           # emits qc_pass / qc_fail_reason optionally
│   └── summarize_qc.py
├── logs/                           # SLURM logs
├── containers/                     # Apptainer .sif files
└── sandbox/                        # preserved source TSVs (migration artifact)
```

Repo layout:

```
casetrack/
├── casetrack.py                 # v0.3 commands (single file)
├── casetrack_qc/                # v0.4 QC subsystem (subpackage)
│   ├── schema.py                # qc_events DDL + qc_status + TOML
│   ├── events.py                # insert / query / resolve; derive_status
│   ├── censor.py                # cmd_censor, cmd_uncensor, cmd_qc_history
│   ├── consent.py               # consent updates + ethics regex + invariant
│   ├── autoflag.py              # SLURM summary-TSV convention
│   ├── reader.py                # _active cascade + DuckDB view
│   ├── cohort.py                # cmd_cohort + pair-by N-partition
│   ├── migrate.py               # cmd_migrate_qc
│   ├── recover.py               # replay helpers for QC actions
│   └── cli.py                   # argparse wiring helpers
├── setup.py                     # pip entry_points + packages=[casetrack_qc]
├── README.md                    # you are here
├── docs/
│   ├── MIGRATION_v0.2_to_v0.3.md
│   ├── MIGRATION_v0.3_to_v0.4.md
│   └── proposals/0001-…, 0002-qc-events-and-censoring.md
├── examples/
│   ├── run_modkit.sh
│   ├── scripts/                 # summarize_*.py
│   ├── nextflow/                # DSL2 module + demo pipeline
│   ├── claude/                  # post-analysis QC hook
│   └── giab_chr21/              # real-data demo (HG002+HG006 chr21)
├── tests/                       # 522 pytest tests across 27 files
└── sandbox/                     # non-tracked build leftovers (gitignored)
```

## Testing

```bash
pip install --user pytest openpyxl pyarrow
python3 -m pytest tests/ -q
```

522 tests across 27 files (~2 min full run).

Suite covers: helpers (locking, provenance, checksums, schema), every
subcommand (flat + project mode), smart-merge correctness and 5000-sample
perf regression, concurrent append via `multiprocessing`, WAL concurrency
under real contention, git provenance with a real tmp repo, the Nextflow
module's shell contract (extract-and-execute), the Claude Code hook (with
a stubbed `claude` on PATH), and every v0.4 QC path: schema + events CRUD,
censor / uncensor / qc-history CLI, SLURM autoflag, strict-refuse on
censored append, rerun / status / export QC defaults, validate
invariants, ethics-override gate, `migrate-qc` + `recover` round-trip,
and `cohort` base + `--pair-by` N-partition.

## Design principles

1. **The DB is a cache. Provenance is the truth.** `casetrack.db` is
   gitignored and regenerable from `casetrack.toml` + `provenance.jsonl`
   via `casetrack recover`. Every mutation — including QC — produces a
   provenance entry before it produces a DB change.
2. **Append-only by default.** Columns only get added; cells only get
   filled. Destructive operations require explicit flags. Censoring is
   append-only too: `uncensor` writes `resolved_at` on the same row,
   never deletes.
3. **Convention over configuration.** Every analysis gets a
   `{analysis}_done` timestamp column — that's what `status` and `rerun`
   read. Every summary TSV can opt into QC with three reserved column
   names. Every mutation logs provenance. No per-project config.
4. **Strict FK, strict QC, strict consent.** Unknown parent → exit 2.
   Append on censored → exit 2. Consent reversal without ethics gate →
   exit 2. Every strict check has a paired `--yes` opt-in so deliberate
   overrides are loud.
5. **Whole-entity QC, not per-analysis.** An assay is usable or it isn't.
   Per-analysis censoring (fine for modkit, bad for xtea on the same
   assay) is deferred until a concrete motivating case appears.
6. **Read-time cascade, not write-time denormalization.** A consent-
   revoked patient excludes its specimens and assays at query time — the
   child rows don't carry a copy of the consent flag.
7. **Safety rails are explicit.** Three `--yes` opt-ins (§Concurrency),
   provenance on every write, `validate` + `recover` + `doctor` for
   post-hoc checks.

## License

MIT
