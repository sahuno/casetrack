# casetrack

**Lifecycle data management for computational biology pipelines on HPC.**

Answers two questions about a multi-patient, multi-specimen, multi-assay
cohort: "*is this analysis complete?*" and "*is this sample usable?*"

Storage layers, one CLI:
- **v0.6 (current, alpha)**: identity layer on top of v0.4. Every project
  gets a `project_id` slug at init, persisted in TOML + `project_meta`
  SQLite table + `~/.casetrack/registry.json`, so commands can address a
  project by name (`casetrack --project hgsoc-2026 query "..."`) instead
  of a fragile path. Hierarchy IDs (`patient_id`, `specimen_id`,
  `assay_id`) are now validated against an ASCII regex at insert time —
  typos in samplesheets fail loudly at `register`, not silently
  downstream. Per-level escape hatches via `[levels.<level>] id_pattern`
  for legacy LIMS IDs. See [proposal 0005](docs/proposals/0005-id-format-and-project-identity.md).
- **v0.4**: QC / censoring / consent subsystem. Every read path
  (`status`, `rerun`, `export`, `query`, `dashboard`) filters out
  QC-failed and consent-revoked entities by default. SLURM summary TSVs
  auto-flag via `qc_pass` / `qc_fail_reason` / `qc_warn` columns.
  Paired-design readiness via `casetrack cohort --pair-by`.
- **v0.3 (project mode)**: SQLite-backed project directory with
  normalized `patient → specimen → assay` tables, enforced foreign keys,
  typed columns, and DuckDB-powered SQL queries. Survives DB corruption
  — everything is regenerable from `casetrack.toml` + `provenance.jsonl`.
- **v0.2 (flat mode — deprecated)**: one TSV manifest per project, one
  row per sample. Still works, loud deprecation warning, removed in **v1.0**.

Upgrade paths:
`v0.2 → v0.3` via `casetrack migrate` ([guide](docs/MIGRATION_v0.2_to_v0.3.md)).
`v0.3 → v0.4` via `casetrack migrate-qc` ([guide](docs/MIGRATION_v0.3_to_v0.4.md)).
`v0.4 → v0.6` is automatic for new projects (init writes `project_id`); legacy projects continue to work without one until v0.6 final ships `casetrack migrate-project-id`.

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
- [The three-level hierarchy](#the-three-level-hierarchy)
- [Quick start — v0.3 project mode (recommended)](#quick-start--v03-project-mode-recommended)
- [Quick start — v0.2 flat mode (deprecated)](#quick-start--v02-flat-mode-deprecated)
- [Commands](#commands)
- [Project identity & registry (v0.6+)](#project-identity--registry-v06)
- [The three-phase SLURM pattern](#the-three-phase-slurm-pattern)
- [Driving the next batch from the DB](#driving-the-next-batch-from-the-db)
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

## Project layout (v0.6+)

`casetrack init --project-dir <path>` scaffolds a full, publication-ready
project tree by default. The enforced triad — `casetrack.toml` (schema),
`casetrack.db` (SQLite), `provenance.jsonl` (audit log) — lives at the root
alongside a set of leaf directories for inputs, references, outputs, docs,
manuscript artifacts, logs, containers, and sandbox space:

```
cohort/
├── casetrack.{toml,db}           # schema + DB (DB gitignored)
├── provenance.jsonl              # append-only audit log
├── .gitignore                    # excludes DB, raw data, sifs, large outputs
├── data/{raw,processed,ref,validation}/  # raw=immutable inputs;
│                                         # processed/{genome}/{patient}/{assay}/  ← persistent BAMs/VCFs (DB-indexed);
│                                         # ref=reference genome+indexes; validation=truth sets
├── results/                      # analysis outputs (per-run summary TSVs, trace files)
├── scripts/                      # top-level analysis scripts (01_, 02_, …)
├── docs/{research,hypothesis}/   # literature notes, analysis plans
├── manuscript/
│   ├── figures/scripts/{png,pdf,svg}/    # composed manuscript figures
│   ├── draft/ proofs/ references/
├── logs/ containers/ sandbox/
```

Every leaf ships a `.gitkeep` so the tree survives `git clone`. Re-running
`init` on an existing project fills in missing leaves without touching
existing files. Opt out with `casetrack init --project-dir <path> --bare`
if you already have a layout. Full design: `docs/proposals/0003-init-scaffold.md`.

> **Whenever you edit `casetrack.toml`** (add a column, declare a new
> `[analyses.<tool>]`, change an enum), run `casetrack schema apply
> --project-dir .` to apply the change to the SQLite DB via non-destructive
> `ALTER TABLE`. Existing rows are preserved; `schema_v` bumps. Skipping
> this step produces `sqlite3.OperationalError: no such column: <name>` on
> the next `add-metadata` or `append`.

## The three-level hierarchy

Every project mode database has exactly three levels with strict foreign-key
parentage:

```
patients           ← clinical/demographic metadata
                     (sex, cohort, sample_id, internal_id, timepoint, age…)
  └── specimens    ← biological sample (tumor, normal, cell line, …)
                     most analysis tracking lives here
        └── assays ← individual sequencing run
                     per-run basecalling + QC lives here
```

**Which level does my analysis go on?** Match the *granularity of the input*:

| Analysis type            | Typical level | Examples                                  |
|--------------------------|---------------|-------------------------------------------|
| Per-run                  | `assay`       | basecalling (dorado), per-run flagstat    |
| Per-sample / per-merge   | `specimen`    | sort, merge, modkit pileup/callmods, SV   |
| Per-patient / cohort-wide| `patient`     | family-level SV, cohort-level summary     |

Each `[analyses.<tool>]` block in `casetrack.toml` declares which level it
writes to; that determines which table grows the `{tool}_done` timestamp and
the `{column_prefix}_*` result columns. You can register entities at any
level (patient → specimen → assay; FK enforces order) and you can add new
analyses to existing levels by editing TOML and running `casetrack schema apply`.

## Quick start — v0.3 project mode (recommended)

```bash
# 1. Create a project. --project-id is optional (auto-derived from
#    --project-name or directory basename). The slug is registered in
#    ~/.casetrack/registry.json so you can later query by id from anywhere.
casetrack init --project-dir cohort/ --from-template hgsoc \
    --project-id hgsoc-2026 \
    --project-name "HGSOC methylation cohort, spring 2026"

# 2a. Register one row at a time. IDs are validated against
#     \A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z — typos with whitespace, shell
#     metacharacters, or path separators fail loudly here, not three jobs in.
casetrack register --project hgsoc-2026 --level patient  --id HGSOC002 \
    --meta 'age=55,sex=F,brca_status=brca1'
casetrack register --project hgsoc-2026 --level specimen --id HGSOC002-normal \
    --parent HGSOC002 --meta 'tissue_site=normal'
casetrack register --project hgsoc-2026 --level assay    --id HGSOC002-normal-ONT-RNA \
    --parent HGSOC002-normal --meta 'assay_type=ONT'

# 2b. RECOMMENDED for real cohorts — bulk-register from a TSV. One TSV per
#     level, columns matching [levels.<level>.columns] in casetrack.toml.
#     `--allow-new --yes` is the double opt-in to admit IDs not yet in the DB;
#     parent-FK enforcement means patients must land before specimens, before assays.
casetrack add-metadata --project hgsoc-2026 --level patient \
    --metadata patients.tsv  --allow-new --yes
casetrack add-metadata --project hgsoc-2026 --level specimen \
    --metadata specimens.tsv --allow-new --yes
casetrack add-metadata --project hgsoc-2026 --level assay \
    --metadata assays.tsv    --allow-new --yes
# Use --overwrite (without --allow-new) to update existing rows in place.

# 3. At the end of a SLURM job, append the per-sample summary TSV.
#    If the TSV has qc_pass=False, casetrack auto-emits a qc_events row
#    in the same transaction — no extra CLI call needed (see §SLURM pattern).
casetrack append --project hgsoc-2026 --level assay \
    --results summary.tsv --analysis modkit_methylation

# 4. Flag a bad assay manually (library prep failed, contamination, etc.).
casetrack censor --project hgsoc-2026 \
    --level assay --id HGSOC002-normal-ONT-RNA \
    --kind library_prep_failed --reason "cDNA yield 8 ng, need >100"

# 5. See what's complete AND usable. `--usable` adds the exclusion breakdown.
casetrack status    --project hgsoc-2026 --usable
casetrack dashboard --project hgsoc-2026 --output dashboard.html

# 6. Cohort readiness — paired designs surface broken pairs.
casetrack cohort --project hgsoc-2026 \
    --assay-type ONT-RNA-Seq --pair-by tissue_site

# 7. Query across the three levels with SQL. `_` is the raw join;
#    `_active` applies the §4.4 cascade (QC + consent) automatically.
casetrack query --project hgsoc-2026 --fmt json \
    "SELECT patient_id, assay_id, mean_meth FROM _active WHERE mean_meth > 0.6"
```

`--project hgsoc-2026` and `--project-dir cohort/` are interchangeable — pass either, not both. The registry is local to your account (`~/.casetrack/registry.json`); team-shared registries are deferred to a later proposal.

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
| `register`       | **v0.3** — insert a single row at patient/specimen/assay with strict FK. **v0.6**: rejects malformed IDs (whitespace, shell metas, path seps) at the source — see §Project identity. |
| `append`         | Attach analysis results (flat: columns; v0.3: ALTER TABLE ADD COLUMN)        |
| `add-metadata`   | Bulk UPDATE/INSERT metadata from a TSV (no `_done` timestamp)                |
| `status`         | Completion summary (`--group-by {analysis,assay,specimen,patient}`)          |
| `validate`       | Integrity check: TOML↔DB drift, FK violations, orphan `_done` columns        |
| `log`            | Show provenance entries (`--level L` / `--transaction TX` filters in v0.3)   |
| `schema`         | Flat: column-to-analysis map. v0.3: `{show,dump,check,apply}`                |
| `rerun`          | Emit or submit sbatch commands for rows missing an analysis                  |
| `dashboard`      | Self-contained offline HTML dashboard (v0.3: nested patients → specimens → assays) |
| `projects`       | Manage `~/.casetrack/registry.json` (`list`/`register`/`deregister`) or scan a tree (`scan --root <path>`). v0.5 form `projects --root` still works. |
| `query`          | SQL over flat manifests or v0.3 projects (DuckDB-backed; v0.3 exposes `_`)   |
| `export`         | Export to xlsx / csv / json / parquet / tsv (`--shape tables\|joined` in v0.3) |
| `doctor`         | **v0.3** — concurrency stress test on the project's filesystem. **v0.6** `--id-format` scans hierarchy IDs against the schema's regex (table or TSV output, exit 1 on findings). |
| `recover`        | **v0.3** — rebuild `casetrack.db` by replaying `provenance.jsonl`            |
| `censor`         | **v0.4** — record a QC failure / consent revocation on an entity             |
| `uncensor`       | **v0.4** — resolve an active qc_events row (consent reversal gated)          |
| `qc-history`     | **v0.4** — full QC event history for one entity (or all active)              |
| `migrate-qc`     | **v0.4** — one-shot: add QC schema + port a legacy `qc_pass` column          |
| `cohort`         | **v0.4** — cohort readiness summary + paired-design view (`--pair-by`)       |
| `migrate-lineage`| **v0.6** — add assay lineage + batch tables to an existing project           |
| `add-batch`      | **v0.6** — register a sequencing/library-prep batch (manual or `--from-tsv`) |
| `link-sources`   | **v0.6** — record which run assays fed a specimen or merged assay (Mode A: run→merged-assay; Mode B: run→specimen) |
| `project`        | **v0.7** — project lifecycle (`set-status`, `status`) — active / complete / archived |
| `migrate-status` | **v0.7** — add lifecycle `status` column to `project_meta` (idempotent)       |
| `append-cohort`  | **v0.7** — register a cohort-level artifact (joint VCF, PoN, matrix) + its assay lineage (proposal 0009) |
| `cohort-artifacts`| **v0.7** — list cohort-level artifacts with read-time staleness (`--stale-only`) |
| `migrate-cohort` | **v0.7** — additive: create the cohort-artifact tables on a pre-0009 project   |
| `references`     | **v0.8** — list reference artifacts + read-time ref-staleness (`--stale-only`, `--fmt`) (proposal 0010) |
| `migrate-references` | **v0.8** — additive: create the reference-artifact tables on a pre-0010 project |
| `derived-from`   | **v0.9** — declare a derivation edge between two lineage nodes (`cohort:`, `reference:`, `analysis:`) (proposal 0011) |
| `derivation`     | **v0.9** — inspect the full derivation graph / derived-stale state (`--node`, `--stale-only`, `--fmt`) (proposal 0011) |
| `migrate-derivation` | **v0.9** — additive: create the `artifact_derivation` table on a pre-0011 project |

`casetrack <cmd> --help` for the full option list on any subcommand.

### Reference artifacts (v0.8, proposal 0010)

Declare versioned external inputs once in `casetrack.toml`, and each analysis
names the references it consumes:

```toml
[references.genome]
path = "/data1/greenbab/database/hg38/v0/Homo_sapiens_assembly38.fasta"
version = "hg38_v0"          # changing this version flags downstream outputs stale
kind = "genome"              # genome | annotation | known_variants | repeats | intervals | other

[references.dbsnp]
path = ".../dbsnp_b156.vcf.gz"
version = "dbsnp_b156"
kind = "known_variants"

[analyses.clair3]
level = "specimen"
column_prefix = "clair3"
uses = ["genome", "dbsnp"]   # append auto-snapshots each ref's current version
```

`casetrack append` records which reference version each output consumed.
When a reference is bumped, every output that used the old version reads `STALE`:

```bash
# 1. analysis ran against hg38_v0 — output is fresh
casetrack append --project-dir . --analysis clair3 --results clair3_summary.tsv

# 2. bump the genome version in casetrack.toml (hg38_v0 -> hg38_v1), then:
casetrack schema apply --project-dir .          # syncs [references], logs the move

# 3. the clair3 output is now ref-stale
casetrack references --project-dir . --stale-only
#   [STALE] specimen:S1/clair3  (genome: hg38_v0 -> hg38_v1)
```

Staleness is read-time and reversible: revert the version and outputs read
`fresh` again. It is orthogonal to cohort-artifact input-staleness (0009) — a
cohort artifact can be input-stale, ref-stale, both, or neither. Override the
declared refs per run with `--uses-references genome,dbsnp`, or skip capture with
`--no-track-references`. For cohort outputs, `append-cohort --uses-references …`.

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
- `--overwrite` replaces existing values for the target columns. **Required for
  reruns** — without it, fill-only is the default and previously populated cells
  silently win over the fresh run. The most common cause of "I patched the
  summarize script and re-ran but the DB still shows the old buggy values."
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

## Project identity & registry (v0.6+)

Every project gets a **`project_id`** at init — a DNS-label slug like `hgsoc-2026` that's stable across mount points, rsyncs, and archive/restore cycles. It's persisted in three places, all cross-checked:

1. `casetrack.toml` `[project] project_id` — the human-editable source.
2. `project_meta` table inside `casetrack.db` — the DB self-describes.
3. `~/.casetrack/registry.json` — your local lookup.

The point: an LLM, a SLURM wrapper, or you-on-tuesday can address a project by id without remembering paths.

```bash
# Auto-derived slug from --project-name (or directory basename if --project-name
# is unsuitable). Pass --project-id explicitly to override.
casetrack init --project-dir cohort/ \
    --project-name "HGSOC methylation cohort, spring 2026"
# → registers project_id "hgsoc-methylation-cohort-spring-2026"

# Address a registered project by id from anywhere — no path memorisation.
casetrack --project hgsoc-2026 query "SELECT COUNT(*) FROM patients"
casetrack --project hgsoc-2026 status --usable

# List / register / deregister entries in your local registry.
casetrack projects list                       # table | --fmt json | --fmt tsv
casetrack projects register --project-dir /restored/from/archive/cohort
casetrack projects deregister some-old-project

# Legacy filesystem-walk overview (v0.5 behavior, still supported).
casetrack projects scan --root ~/projects/    # or: casetrack projects --root ~/projects/
```

If TOML's `project_id` and the DB's `project_meta.project_id` disagree (you copied a `.db` into the wrong directory, or hand-edited TOML after init), the next command fails loudly with both values shown.

### Legacy projects must be migrated (v0.6+)

v0.6 refuses to run any command on a project that lacks `project_id` + `project_meta`. The error prints the exact migration command to run:

```
Error: This project is missing v0.6 identity wiring
([project] project_id in casetrack.toml, project_meta row in casetrack.db). Run:
    casetrack migrate-project-id --project-dir /data/old_cohort
To bypass for a one-off read or batch audit, set CASETRACK_ALLOW_LEGACY=1.
```

**Bypass for read-only audits** — if you've inherited a v0.5 project and want to inspect it before deciding to migrate, set the env var:

```bash
# One-off read:
CASETRACK_ALLOW_LEGACY=1 casetrack status --project-dir /data/old_cohort

# Audit every legacy cohort under a root before migrating:
CASETRACK_ALLOW_LEGACY=1 casetrack projects scan --root ~/projects/
```

Upgrade-path commands (`migrate-qc`, `migrate-project-id`, `recover`) bypass the gate automatically — they're designed to operate on legacy state.

### AI-agent integration (MCP server)

`casetrack-mcp` is a stdio MCP server that exposes two tools to AI agents (Claude Desktop, or any MCP client):

| Tool | Arguments | Returns |
|---|---|---|
| `casetrack_list_projects` | (none) | JSON summary of the local registry |
| `casetrack_query` | `project_id`, `sql` (SELECT / WITH only) | Rows as JSON |
| `casetrack_cohort_artifacts` | `project_id`, `stale_only?` | Cohort artifacts + input-staleness (proposal 0009) |
| `casetrack_references` | `project_id`, `stale_only?` | Reference artifacts + ref-staleness (proposal 0010) |
| `casetrack_derivation` | `project_id`, `stale_only?` | Derivation graph + derived-staleness (proposal 0011) |

Install the optional dependency + wire it into Claude Desktop:

```bash
pip install casetrack[mcp]
```

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) — equivalent path on Linux / Windows:

```json
{
  "mcpServers": {
    "casetrack": {
      "command": "casetrack-mcp"
    }
  }
}
```

Safety rails:
- **Closed-world project lookup.** `project_id` must be in the registry — unknown ids return the valid set instead of a generic "not found." Agents can't invent paths because paths never appear in the tool signature (that's the hallucination-reduction lever from [proposal 0005 §5.6](docs/proposals/0005-id-format-and-project-identity.md)).
- **Read-only SQL.** `casetrack_query` rejects non-SELECT statements. Mutations go through the CLI (which logs to `provenance.jsonl`).
- **Row cap.** Results truncated at 10,000 rows with `truncated=true` flagged in the payload.

See `casetrack_mcp/README.md` for the full install + legacy-project env override.

### Migrating legacy projects

`casetrack migrate-project-id` brings v0.5 (or pre-v0.6) projects into the identity scheme without forcing a re-init. Idempotent; safe to run as often as you like.

```bash
# Single project — interactive (Enter accepts the suggested slug).
casetrack migrate-project-id --project-dir /data/old_cohort/

# Same, non-interactive — accept the auto-suggestion.
casetrack migrate-project-id --project-dir /data/old_cohort/ --yes

# Force a specific slug instead of the suggestion.
casetrack migrate-project-id --project-dir /data/old_cohort/ \
    --project-id hgsoc-pilot-2024 --yes

# Batch: walk a tree, migrate every casetrack project that's still on v0.5.
casetrack migrate-project-id --scan ~/projects/ --yes
```

The migrate command derives a slug from `[project] name` (lowercase, hyphens), writes the `project_id` into TOML, creates the `project_meta` row, and registers in `~/.casetrack/registry.json`. It refuses to act when:
- TOML and DB already disagree on `project_id` (drift — resolve manually first)
- The chosen slug is already registered to a different directory (pass `--project-id <other>` or `casetrack projects deregister <slug>` first)

Each migration writes a `migrate_project_id` entry to `provenance.jsonl` with the list of artifacts it touched (`["toml", "project_meta", "registry"]`).

### Hierarchy ID format (`patient_id`, `specimen_id`, `assay_id`)

Validated at every INSERT path (`register`, `migrate`, `add-metadata --allow-new`) against:

```
\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z
```

ASCII alphanumeric start; then alphanumeric, underscore, hyphen, or dot; 1–64 chars. No whitespace, no shell metacharacters, no path separators. Plus a case-insensitive duplicate check within a level — `HG006` and `hg006` can't coexist by default. Read paths (`query`, `export`, `dashboard`, `recover`) tolerate pre-v0.6 malformed IDs unchanged.

**Why?** Whitespace in a `patient_id` makes `casetrack query` fail with cryptic "no rule" errors; shell metacharacters in IDs are SQL-injection material if any wrapper builds a query via string concat; path separators silently break path-template joins. Failing loudly at `register` saves hours of downstream debugging.

**Escape hatch** for cohorts with legacy LIMS IDs containing colons or other non-default characters:

```toml
[levels.patient]
key                 = "patient_id"
id_pattern          = "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"   # allow colons, 80 chars
allow_case_variants = true                                     # allow HG006 + hg006
```

Project-wide non-ASCII opt-in via `[project] allow_unicode_ids = true`. Bulk-audit existing IDs against the schema with `casetrack doctor --id-format --project hgsoc-2026` (or `--fmt tsv` for CI). Full design + escape hatches: [proposal 0005](docs/proposals/0005-id-format-and-project-identity.md).

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

### One-flag append via path inference (v0.5)

If the pipeline writes into a tool-first directory tree, phase 3 collapses
to a single flag:

```
results/
└── modkit_pileup/                    # tool name → matches [analyses.modkit_pileup]
    └── 20260418_hg38_v1/             # run_tag = {date}_{genome}_{description}
        └── P01/P01_primary/P01_primary_ONT1/
            └── modkit_summary.tsv
```

The project's `casetrack.toml` declares the layout and the tool:

```toml
[layout]
results_dir = "results"

[layout.path_templates]
assay = "{tool}/{run_tag}/{patient_id}/{specimen_id}/{assay_id}"

[analyses.modkit_pileup]
level         = "assay"
column_prefix = "modkit"
summary_tsv   = "modkit_summary.tsv"
```

Then from any leaf directory:

```bash
cd results/modkit_pileup/20260418_hg38_v1/P01/P01_primary/P01_primary_ONT1
casetrack append --infer-from-path
```

The CLI walks up to `casetrack.toml`, matches the path against the
templates, and fills `--project-dir`, `--level`, `--analysis`,
`--column-prefix`, and `--results` from the `[analyses.<tool>]` entry. The
run_tag is injected as `{prefix}_run_tag` in the target row so re-runs can
be compared later via `casetrack query`. Explicit flags still override.

## Driving the next batch from the DB

casetrack is at its most useful when it drives Nextflow / Snakemake samplesheets
**dynamically**. The DB already knows what's done and what's pending; query it
to produce the next batch. This avoids hand-curated samplesheets that drift
from reality.

The pattern depends on storing input file paths on the entity row — usually
`pod5_path` and/or `bam_path` on `assays`. Add them to your TOML:

```toml
[levels.assay.columns]
assay_id    = { type = "TEXT", required = true, unique = true }
specimen_id = { type = "TEXT", required = true }
condition   = { type = "TEXT" }   # tumor / normal / etc.
pod5_path   = { type = "TEXT" }   # raw signal — input to basecaller
bam_path    = { type = "TEXT" }   # basecalled BAM — input to downstream tools
```

Then any tool that needs work to do queries the DB:

```bash
# All normals that are ready to basecall but haven't been
casetrack query --project hgsoc-2026 --fmt csv --sql "
  SELECT a.assay_id    AS sample,
         p.patient_id  AS patient,
         s.specimen_id AS specimen,
         a.pod5_path   AS pod5_dir,
         'hg38'        AS genome
  FROM assays a
  JOIN specimens s ON a.specimen_id = s.specimen_id
  JOIN patients  p ON s.patient_id  = p.patient_id
  WHERE a.condition = 'normal'
    AND a.qc_status = 'pass'
    AND a.dorado_basecaller_done IS NULL
" > pending_normals.csv

# Submit the pipeline
nextflow run main.nf --input pending_normals.csv ...

# Resubmit safely — Nextflow -resume skips completed tasks; casetrack
# --overwrite (in CASETRACK_REGISTER) keeps the DB current. The pending
# query shrinks on every run.
```

Why this works:
- Censored samples (e.g. `qc_warn` for "pod5 rsync incomplete") are excluded
  by `qc_status = 'pass'` (or by querying the `_active` view).
- New samples added via `add-metadata` automatically appear in the next
  pending query — no pipeline config to update.
- The same pattern works at every level: pending sorts, pending pileups,
  pending SV calls. Just swap the level and the `*_done` column name.

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

Two integration paths, depending on how much pipeline you bring of your own.

### Drop-in module — `examples/nextflow/casetrack.nf`

For pipelines you've already written, include the standalone DSL2 module:

```groovy
include { casetrack_append } from './modules/casetrack.nf'

workflow {
    summarize_modkit(samples_ch)                       // emits (analysis, tsv)
    casetrack_append(summarize_modkit.out)             // register in manifest
}
```

See `examples/nextflow/README.md` for the full override matrix
(`casetrack_manifest`, `casetrack_key`, `casetrack_bin`, …), a ready-to-run
example pipeline, profiles for `standard` / `slurm` / `apptainer` / `test`, and
three integration patterns (standalone, `afterScript`, collect-and-batch). The
`casetrack_append_project` process inherits v0.4's strict-refuse + autoflag
automatically — nothing to configure. If a summarize step emits a TSV with
`qc_pass=False`, the append step turns it into a `qc_events` row in the same
transaction.

### Reusable subworkflows — [`casetrack-nf-subworkflows`](https://github.com/sahuno/casetrack-nf-subworkflows)

For new pipelines built around tracked nf-core modules (samtools sort, dorado
basecaller, modkit callmods, sniffles2, etc.), the
[`casetrack-nf-subworkflows`](https://github.com/sahuno/casetrack-nf-subworkflows)
repo ships ready-made wrappers. Each subworkflow follows the 3-phase contract
(run tool → summarize → register) and uses the canonical
`CASETRACK_REGISTER` process:

```groovy
include { SAMTOOLS_SORT_TRACKED } from 'casetrack-nf-subworkflows/subworkflows/local/samtools_sort_tracked'

workflow {
    SAMTOOLS_SORT_TRACKED(ch_bam)
    // CASETRACK_REGISTER fires automatically with --infer-from-path --overwrite
    // and writes `samtools_sort_done` + `sort_*` columns to the specimen row.
}
```

Required pipeline params: `--casetrack_project_dir`, `--run_tag`,
`--casetrack_level`. The summary TSV lands at the path declared in
`[layout.path_templates]` and `casetrack append --infer-from-path` recovers
tool / run_tag / patient / specimen / assay from the path.

Implementation notes (`executor 'local'` + `maxForks 1` for the register step,
publishDir conventions for `data/processed/`, common pitfalls): see the
subworkflows repo's README and `references/nextflow-integration.md` in the
shipped Claude Code skill.

## Claude Code integration

Two integration points, both shipped in this repo.

### Skill bundle — `.claude/skills/casetrack/`

A Claude Code skill that teaches agents the canonical casetrack patterns:
project init, TOML schema design, the `add-metadata` vs `append` distinction,
the `--overwrite` rerun gotcha, the 3-phase analysis pattern, Nextflow
`CASETRACK_REGISTER` integration, batch/incremental queries, and the QC event
system.

The skill is auto-discovered when Claude Code runs from any directory inside
this repo. To use it from anywhere on your machine, symlink it into your global
skills directory:

```bash
ln -s /path/to/casetrack/.claude/skills/casetrack ~/.claude/skills/
```

Inside the skill bundle:
- `SKILL.md` — main entry point, < 500 lines
- `references/patterns.md` — full pattern reference
- `references/toml-example.md` — annotated complete TOML
- `references/nextflow-integration.md` — `CASETRACK_REGISTER` + publishDir
- `references/common-queries.md` — SQL recipes for samplesheets, progress, QC
- `evals/evals.json` — 8 regression test scenarios

### Post-analysis QC hook — `examples/claude/`

Level 2 hook (flat-mode era, still works for v0.2 manifests): after a SLURM
analysis finishes and calls `casetrack append`, a companion shell script
invokes `claude --print`, captures a QC review as a second TSV, validates its
shape, and appends it back as its own analysis (`cc_<analysis>_review`) — so
the manifest carries both the raw numbers and an LLM verdict, fully traceable.

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
├── data/
│   ├── raw/                        # immutable inputs — never modified in place
│   ├── processed/{genome}/{patient_id}/{assay_id}/   # persistent biological outputs
│   │                                                  # — sorted BAMs, called VCFs, etc.
│   │                                                  # Filenames carry genome tag:
│   │                                                  # {assay_id}.{genome}.sorted.bam
│   │                                                  # The DB stores absolute paths to
│   │                                                  # these files so downstream tools
│   │                                                  # find them by query, not by scan.
│   ├── ref/                        # reference genome + annotations + indexes
│   └── validation/                 # truth sets / ground-truth BEDs / benchmark VCFs
├── results/                        # analysis bookkeeping — summary TSVs, trace files
│   ├── modkit/{run_tag}/{patient_id}/{specimen_id}/{assay_id}/
│   ├── samtools_sort/{run_tag}/{patient_id}/{specimen_id}/
│   └── sniffles2/{run_tag}/{patient_id}/{specimen_id}/
├── scripts/
│   ├── summarize_modkit.py         # distils raw output → assay TSV
│   ├── summarize_tldr.py           # emits qc_pass / qc_fail_reason optionally
│   └── summarize_qc.py
├── docs/                           # project-specific notes (protocol, PI briefs, README)
│   ├── research/                   # literature notes, prior-work summaries
│   └── hypothesis/                 # pre-registered hypotheses, analysis plans
├── manuscript/
│   ├── figures/
│   │   └── scripts/                # figure-making code
│   │       ├── png/                # rendered figures — PNG
│   │       ├── pdf/                # rendered figures — PDF
│   │       └── svg/                # rendered figures — SVG
│   ├── draft/                      # working manuscript drafts
│   ├── proofs/                     # journal proofs / revisions
│   └── references/                 # bib files / reference PDFs
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
