---
name: casetrack
description: Use this skill whenever the user works with **casetrack** — the manifest-centric bioinformatics case management CLI that tracks which analyses have run across multi-patient, multi-specimen, multi-assay cohorts (cancer genomics, ONT/Illumina WGS, RNA-seq). Trigger it for anything involving casetrack projects, including: initializing a project, registering patients/specimens/assays, running `casetrack init | add-metadata | append | schema apply | query | status | censor | uncensor`, recording analysis results in the cohort database, generating Nextflow or Snakemake samplesheets from the DB, tracking which samples still need basecalling/sort/variant calling/methylation, managing QC events and consent flags, integrating `CASETRACK_REGISTER` into a Nextflow pipeline, querying a `casetrack.db` via SQL, or registering **cohort-level artifacts** that span many assays (joint-genotyped VCFs, panels-of-normals, cohort matrices) with `append-cohort` / `cohort-artifacts` / `migrate-cohort` and checking their staleness. Trigger also when the user asks about **reference artifacts** (proposal 0010): declaring versioned genome/annotation/dbSNP/interval references in TOML, bumping a reference version, checking whether an output is stale after a reference update, running `casetrack references` / `migrate-references`, or asking "is my VCF stale after the genome update", "reference version changed", "genome bumped to hg38_v1", "GTF or dbSNP version tracking". Trigger even when the user says "cohort database", "sample tracker", "case manifest", "sample manifest", "joint VCF tracking", "panel of normals tracking", "is my cohort matrix stale", "reference artifact", or describes a tumor-normal cohort with per-sample analysis progress — those are all casetrack territory. Do not trigger for generic Snakemake/Nextflow questions, unless they involve casetrack integration.
---

# casetrack skill

casetrack is a **tracker**, not a scheduler. It records what has been done, what is pending, what is blocked. Pipelines (Nextflow/Snakemake/SLURM) run the analyses; casetrack stores the facts afterwards.

The single most common failure mode for agents is confusing `add-metadata` with `append`, or forgetting `--overwrite`. Both produce silent failures (no error, wrong DB state). Internalize §3 and §5 before generating any command.

## 1. When to use what

Ask yourself: am I registering an entity (adding a new patient/specimen/assay row), or am I recording that an analysis ran (filling in analysis columns)?

| Intent | Command | Flag pattern |
|---|---|---|
| Create a new project directory | `casetrack init` | `--project-dir <path> --project-name <name>` |
| Add columns to the schema after edits to `casetrack.toml` | `casetrack schema apply` | `--project-dir .` |
| Register NEW patients/specimens/assays | `casetrack add-metadata` | `--level {patient|specimen|assay} --metadata file.tsv --allow-new --yes` |
| Update metadata on EXISTING rows | `casetrack add-metadata` | `--level ... --metadata file.tsv --overwrite` |
| Record that an analysis ran (fill stats columns) | `casetrack append` | `--analysis <name> --results summary.tsv --overwrite` |
| Flag a sample with a QC hold | `casetrack censor` | `--level ... --id ... --kind qc_warn --reason "..."` |
| Lift a QC hold | `casetrack uncensor` | `--level ... --id ... --reason "..."` |
| Register a cohort-level output (joint VCF, PoN, matrix) | `casetrack append-cohort` | `--analysis ... --run-tag ... --path ... --inputs a,b,c` |
| List cohort artifacts + staleness | `casetrack cohort-artifacts` | `--project-dir . [--stale-only]` |
| Add cohort-artifact tables to a pre-0009 project | `casetrack migrate-cohort` | `--project-dir . [--dry-run]` |
| List reference artifacts + staleness (v0.8 / proposal 0010) | `casetrack references` | `--project-dir . [--stale-only] [--fmt table\|tsv\|json]` |
| Add reference-artifact tables to a pre-0010 project (v0.8 / proposal 0010) | `casetrack migrate-references` | `--project-dir . [--dry-run]` |
| Load all three levels from one wide sample sheet (v0.10 / proposal 0012) | `casetrack register-cohort` | `--project-dir . --samplesheet cohort.tsv [--dry-run] [--overwrite]` |
| See overall progress | `casetrack status` | `--project-dir .` |
| Run arbitrary SQL | `casetrack query` | `--project-dir . --sql "..."` |
| Inspect current DB schema | `casetrack schema show` | `--project-dir .` |

Always use `--project-dir <path>` (project mode). `--manifest <tsv>` is legacy flat mode — never use it for new work.

## 2. The 3-level hierarchy (ontology)

```
patients           ← clinical/demographic metadata (sex, cohort, sample_id, timepoint, age…)
  └── specimens    ← biological sample (tumor / normal / cell line); most analysis tracking lives here
        └── assays ← individual sequencing run (per-run basecalling + QC lives here)
```

Key columns per level: `patient_id`, `specimen_id`, `assay_id`. Parent FK is required for new rows — a `specimen` row needs its `patient_id`, an `assay` row needs its `specimen_id`. Registration order is **patient → specimen → assay**.

Analysis placement:
- **Per-run** analyses (basecalling) → `assay` level
- **Per-sample** analyses (sort, methylation pileup, SV calling, merge) → `specimen` level
- **Per-patient** analyses (cohort-level summary, family-level SV) → `patient` level

Each analysis declared in TOML writes a `{analysis}_done` timestamp + its result columns to the declared level. Check `[analyses.<name>].level` in `casetrack.toml` when in doubt.

An output that spans **many** assays at once (a joint-genotyped VCF, a panel-of-normals, a cohort matrix) does **not** fit this single-parent tree — it has no one owner. Those are tracked separately as **cohort-level artifacts** (§15), not as a 4th hierarchy level.

## 3. `add-metadata` vs `append` — the core distinction

This is the most common source of confusion. Get this right and most errors disappear.

| | `add-metadata` | `append` |
|---|---|---|
| Purpose | Entity metadata (native schema columns) | Analysis result columns |
| Requires `--analysis` flag | No | Yes (must match `[analyses.<name>]` in TOML) |
| Can create new rows | Yes, with `--allow-new --yes` | No (IDs must already exist) |
| Column prefix behavior | Columns land as-is (respecting TOML schema) | Every result column gets prefixed with `{column_prefix}_` |
| Writes `{analysis}_done` | No | Yes |
| Typical TSV key column | `patient_id` / `specimen_id` / `assay_id` | same, matching the analysis's declared level |

**Rule of thumb:** If you're building the cohort (populating the patient list, declaring specimens, listing assay runs), use `add-metadata`. If the cohort already exists and you're recording the output of a tool (samtools sort, modkit pileup, dorado basecaller, sniffles), use `append`.

## 4. Schema management — TOML is the contract

The `casetrack.toml` file declares the schema. The SQLite DB mirrors it. Changes always flow TOML → DB, never the other way.

```bash
# After editing [levels.*.columns] or adding a new [analyses.<name>] block:
casetrack schema apply --project-dir .
# Emits: "Applied N schema change(s); schema_v X → Y"
# Runs non-destructive ALTER TABLE statements.
```

**Never edit `casetrack.db` directly** and never let anyone run ad-hoc ALTER TABLE. The TOML is the source of truth; the DB is a cached materialization.

See `references/toml-example.md` for a complete annotated TOML.

## 5. `--overwrite` — the silent gotcha

`casetrack append` (and `CASETRACK_REGISTER` in Nextflow) defaults to **fill-only**: existing non-NULL cells are never updated. Without `--overwrite`, a rerun with fresh stats silently no-ops at the DB level.

**Always pass `--overwrite` for analysis results.** The only time fill-only is correct is when you're specifically backfilling historical data into empty cells without touching anything that's already filled.

```bash
# Rerun a sample and expect the DB to reflect the new values:
casetrack append --project-dir . --analysis modkit_pileup \
  --results modkit_summary.tsv --overwrite
```

For `add-metadata`, the equivalent gotcha: without `--overwrite`, existing values stay; with `--overwrite`, they get replaced.

## 6. The 3-phase analysis pattern

Every analysis follows the same shape:

```
Phase 1: Run the tool        (Nextflow / SLURM / local script)
Phase 2: Summarize to a TSV  (one row per entity at the analysis's level)
Phase 3: casetrack append    (with --overwrite)
```

The summary TSV must:
- Have the level's key column as its first column (or at least present): `patient_id`, `specimen_id`, or `assay_id`
- Have result columns whose names (after prefixing) match what you want in the DB
- Optionally include `qc_pass` / `qc_fail_reason` / `qc_warn` columns — casetrack auto-flags QC events when these appear (v0.4+)

Example: after samtools sort on 6 specimens:
```bash
# summary.tsv:
#   specimen_id   sorted_bam_path                      sorted_bam_size_bytes   n_reads   sort_order
casetrack append \
  --project-dir /path/to/project \
  --analysis samtools_sort \
  --results /tmp/sort_summary.tsv \
  --overwrite
# DB writes: sort_sorted_bam_path, sort_sorted_bam_size_bytes, sort_n_reads, sort_sort_order,
#            samtools_sort_done (= now())
# Prefix "sort" comes from [analyses.samtools_sort].column_prefix in TOML.
```

## 7. Registering a new cohort from scratch

Canonical sequence for a fresh project:

```bash
# 1. Create project
casetrack init --project-dir /path/to/cohort_X --project-name cohort_X

# 2. Edit cohort_X/casetrack.toml — add any extra columns you need at patient/specimen/assay
#    Common additions: sample_id, internal_id, tube_id, timepoint, collection_date,
#                      age_at_collection, pod5_path (on assays — critical for NF samplesheets)
#    Declare analyses: [analyses.<tool>] { level, column_prefix, summary_tsv }

# 3. Apply schema
cd /path/to/cohort_X
casetrack schema apply --project-dir .

# 4. Register patients (must go first — FK)
casetrack add-metadata --project-dir . --level patient \
  --metadata patients.tsv --allow-new --yes

# 5. Register specimens
casetrack add-metadata --project-dir . --level specimen \
  --metadata specimens.tsv --allow-new --yes

# 6. Register assays
casetrack add-metadata --project-dir . --level assay \
  --metadata assays.tsv --allow-new --yes

# 7. (Optional) flag any pending/incomplete samples with qc_warn
casetrack censor --project-dir . --level assay --id <assay_id> \
  --kind qc_warn --reason "pod5 rsync still pending"
```

## 8. Nextflow integration — `CASETRACK_REGISTER`

The canonical pattern, implemented in `casetrack-nf-subworkflows`:

```groovy
include { CASETRACK_REGISTER } from '../modules/local/casetrack_register'

workflow FOO_TRACKED {
    take:
    inputs

    main:
    TOOL_PROCESS(inputs)                    // Phase 1
    SUMMARIZE_TOOL(TOOL_PROCESS.out.files)  // Phase 2 — produces one-row TSV
    CASETRACK_REGISTER(                     // Phase 3
        SUMMARIZE_TOOL.out.summary.map { meta, tsv ->
            tuple(meta, 'samtools_sort', 'samtools_sort_summary.tsv', tsv)
        }
    )
}
```

`CASETRACK_REGISTER` places the summary TSV at the path template declared in `[layout.path_templates.<level>]`, then runs `casetrack append --infer-from-path --overwrite` from that directory. The tool name, run_tag, patient, specimen, and assay_id are recovered from the path — no explicit flags needed.

Required pipeline parameters on every NF run:
```
--casetrack_project_dir   absolute path (outside the NF work dir)
--run_tag                 {YYYYMMDD}_{genome}_{description}
--casetrack_level         assay | specimen | patient (matches the analysis declaration)
--casetrack_bin           defaults to `casetrack`
```

`CASETRACK_REGISTER` must run on the `local` executor with `maxForks = 1` — casetrack's SQLite is WAL-mode but serializing writes keeps provenance logs readable.

See `references/nextflow-integration.md` for the full module implementation and publishDir conventions for `data/processed/`.

## 9. Batch/incremental workflow — DB as work queue

casetrack is most powerful when used to drive Nextflow samplesheets dynamically. The DB already knows what's done and what's pending; query it to produce the next batch.

```bash
# Generate a samplesheet for all normals that still need basecalling
casetrack query --project-dir /path/to/project \
  --sql "SELECT a.assay_id, p.patient_id, s.specimen_id, a.pod5_path, 'hg38' as genome
         FROM assays a
         JOIN specimens s ON a.specimen_id = s.specimen_id
         JOIN patients p ON s.patient_id = p.patient_id
         WHERE a.condition = 'normal'
           AND a.qc_status = 'pass'
           AND a.dorado_basecaller_done IS NULL"
```

Pipe to CSV, submit to Nextflow. Resubmit any time — NF `-resume` skips completed tasks, `casetrack append --overwrite` keeps the DB current. The pending query shrinks on every run.

`qc_status = 'pass'` is important: it excludes censored samples. Use the `_active` view for a stricter filter that also cascades parent censoring.

## 10. QC event system

QC events are **append-only**. `censor` writes an event + sets `qc_status`. `uncensor` writes a `resolved_at` event but never deletes. The `_active` view excludes any entity whose latest event is active (or whose parent is censored).

```bash
# Temporary hold (pod5 rsync pending, QC review needed)
casetrack censor --project-dir . --level assay --id <id> \
  --kind qc_warn --reason "pod5 rsync incomplete 2026-04-21"

# Permanent exclusion
casetrack censor --project-dir . --level assay --id <id> \
  --kind qc_fail --reason "basecalling accuracy <80%"

# Lift a hold when resolved
casetrack uncensor --project-dir . --level assay --id <id> \
  --reason "pod5 rsync confirmed complete"

# Show full QC history for an entity
casetrack qc-history --project-dir . --id <id>
```

QC kinds: `qc_fail`, `qc_warn`, `consent_revoked`, `sequencing_run_failed`, `library_prep_failed`, `basecall_accuracy_low`, `contamination`, `protocol_deviation`, `batch_effect_flagged`, `superseded`, `other`.

`qc_status` values on entities: `pass` | `warn` | `fail` | `censored`.

`consent_revoked` is patient-level only and cascades to all specimens/assays under that patient. Reversal requires `--ethics-override --yes` and an IRB reference in the reason.

## 11. Querying the DB

```bash
# Quick overview
casetrack status --project-dir .

# Arbitrary SQL
casetrack query --project-dir . --sql "SELECT ..."

# Via MCP inside Claude Code
mcp__casetrack__casetrack_list_projects
mcp__casetrack__casetrack_query(project_id="<slug>", sql="...")
```

The MCP tool is usually faster inside Claude Code — no shell escaping. `project_id` is the DNS-slug form (e.g. `project-17424`), which is the canonical identifier used across the casetrack CLI, MCP, and any downstream tooling.

For read queries, always prefer the `_active` view over raw tables — it filters out censored entities automatically. Raw tables are for auditing only.

See `references/common-queries.md` for query recipes (progress matrix, samplesheet generation, orphan detection, QC timeline).

## 12. Results layout convention

```
{project_dir}/
├── casetrack.toml
├── casetrack.db
├── data/
│   └── processed/{genome}/{patient_id}/{assay_id}/      ← primary biological files (persistent)
│         {assay_id}.{genome}.sorted.bam
│         {assay_id}.{genome}.basecalled.bam
│         {assay_id}.{genome}.sniffles.vcf.gz
└── results/
    └── {tool}/{run_tag}/{patient_id}/{specimen_id}/{assay_id}/  ← summary TSVs, per-run outputs
```

The DB stores the `data/processed/` path (from the analysis summary TSV) so downstream tools can find files by query rather than by filesystem scan.

Run tag convention: `{YYYYMMDD}_{genome}_{description}` (e.g. `20260421_hg38_normal_basecalling`).

## 13. Common pitfalls — quick reference

| Pitfall | Symptom | Fix |
|---|---|---|
| Forgot `--overwrite` on append | Rerun doesn't update DB values | Add `--overwrite`. Always. |
| Wrong `--analysis` name | "no such analysis" error | Must match `[analyses.<name>]` key in TOML |
| Schema column not declared in TOML | `no such column` | Add to TOML → `casetrack schema apply` |
| Registration order violated | FK constraint error | patient → specimen → assay |
| Used `append` for new entities | "ID not found" | Use `add-metadata --allow-new --yes` |
| Used `add-metadata` for analysis results | No `_done` timestamp | Use `append --analysis <name>` |
| `pod5_path` missing from assays | Can't drive NF samplesheet from DB | Add `pod5_path` to `[levels.assay.columns]` |
| Queried raw table instead of `_active` | Got censored samples | Use `_active` view |
| `column_prefix = ""` in TOML | Validation error | Prefix must be a non-empty identifier |
| Wrong `--level` flag | Command operates on wrong table | Level must match the entity's parent FK shape |
| Forgot to `schema apply` | New TOML columns invisible in DB | Run `schema apply` after every TOML edit |
| Tried to track a joint VCF / PoN / cohort matrix at a level | No single owning patient/specimen/assay | Use `append-cohort` (§15), not `append` |
| `append-cohort` on a pre-0009 project | "no such table: cohort_artifacts" | Run `casetrack migrate-cohort` once first |
| Reused `run_tag` for a re-genotyping run | New artifact overwrites/clashes with the old | Give each run a distinct `--run-tag`; both coexist in the audit trail |
| Output shows `STALE` after bumping a reference version | Expected — reference version changed | Re-run the analysis with a new `run_tag` once references are stable |
| Bumped reference content but not the `version` string | Staleness not detected | Staleness keys on version string only; bump `version` in TOML to trigger detection |
| `casetrack references` on a pre-0010 project | "no such table: reference_artifacts" | Run `casetrack migrate-references` once first |

## 14. Key command cheatsheet

```bash
# Project lifecycle
casetrack init --project-dir PATH --project-name NAME
casetrack schema apply --project-dir .
casetrack schema show  --project-dir .

# Entity registration (cohort building)
casetrack add-metadata --project-dir . --level patient  --metadata patients.tsv  --allow-new --yes
casetrack add-metadata --project-dir . --level specimen --metadata specimens.tsv --allow-new --yes
casetrack add-metadata --project-dir . --level assay    --metadata assays.tsv    --allow-new --yes

# Analysis tracking
casetrack append --project-dir . --analysis NAME --results summary.tsv --overwrite

# QC events
casetrack censor    --project-dir . --level LEVEL --id ID --kind qc_warn --reason "..."
casetrack uncensor  --project-dir . --level LEVEL --id ID --reason "..."
casetrack qc-history --project-dir . --id ID

# Cohort-level artifacts (proposal 0009)
casetrack migrate-cohort   --project-dir .                       # once, on pre-0009 projects
casetrack append-cohort    --project-dir . --analysis joint_genotype \
  --run-tag 20260521_hg38_jointgt --path cohort.vcf.gz --inputs assayA,assayB,assayC
casetrack cohort-artifacts --project-dir . [--stale-only]

# Reference artifacts (proposal 0010)
# 1. Declare in casetrack.toml:
#   [references]
#   genome   = { path = "/data/hg38_v1/genome.fa",   version = "hg38_v1" }
#   dbsnp    = { path = "/data/hg38_v1/dbsnp155.vcf", version = "155" }
#   [analyses.variant_call]
#   uses = ["genome", "dbsnp"]
#
# 2. Apply schema (materialises reference_artifacts table from TOML):
casetrack schema apply --project-dir .
#
# 3. Append records the references used automatically from TOML:
casetrack append --project-dir . --analysis variant_call --results summary.tsv --overwrite
# Override which refs were used at call time:
casetrack append --project-dir . --analysis variant_call --results summary.tsv --overwrite \
  --uses-references genome,dbsnp
# Opt out of reference tracking entirely:
casetrack append --project-dir . --analysis variant_call --results summary.tsv --overwrite \
  --no-track-references
# Cohort artifact with reference tracking:
casetrack append-cohort --project-dir . --analysis joint_genotype \
  --run-tag 20260521_hg38_jointgt --path cohort.vcf.gz --inputs assayA,assayB \
  --uses-references genome
#
# 4. Check for reference staleness:
casetrack references --project-dir .              # list all, with fresh/STALE/untracked flag
casetrack references --project-dir . --stale-only # only outputs with a changed reference
casetrack references --project-dir . --fmt json   # table (default) | tsv | json
#
# 5. Migrate a pre-0010 project:
casetrack migrate-references --project-dir . [--dry-run]

# Queries
casetrack status --project-dir .
casetrack query  --project-dir . --sql "SELECT ... FROM _active WHERE ..."

# Validation
casetrack validate --project-dir .
casetrack doctor   --project-dir .
```

## 15. Cohort-level artifacts (proposal 0009)

Some outputs are built from **many** assays at once and have no single owning entity: a joint-genotyped VCF, a panel-of-normals, a cohort methylation/expression matrix. The three-level hierarchy (§2) can't represent these — a level is biological, single-parent, and static. So casetrack adds **two additive sibling tables** (the same pattern as `qc_events`; the three-level core is untouched):

- `cohort_artifacts` — one row per cohort output, keyed by `(analysis, run_tag)`.
- `cohort_artifact_inputs` — many-to-many lineage to each contributing `assay_id`.

A 4th hierarchy level was **explicitly rejected** (proposal 0009 §7). When a user asks to track a joint VCF / PoN / cohort matrix, reach for these commands — never try to wedge it into a patient/specimen/assay row.

### Registering an artifact

`(analysis, run_tag)` is the unique key, so a re-genotyping run uses a **new** `run_tag` and coexists with the prior artifact in the audit trail — never overwrites it.

```bash
# One-time on a project created before 0009 (additive; safe; --dry-run to preview):
casetrack migrate-cohort --project-dir .

# Register a joint-genotyped VCF built from three assays:
casetrack append-cohort --project-dir . \
  --analysis joint_genotype \
  --run-tag 20260521_hg38_jointgt \
  --path data/processed/hg38/cohort/joint.vcf.gz \
  --inputs assayA,assayB,assayC \
  --stats stats.json \           # optional: JSON of cohort-level summary numbers
  --checksum sha256:...          # optional
# --inputs-from FILE  accepts one assay_id per line (an 'assay_id' header + extra TSV columns are tolerated)
```

### Staleness is read-time, not stored

An artifact is **`STALE`** when *any* contributing assay is currently censored or consent-revoked — derived live from the QC/consent cascade (§10, proposal 0002 §4.4). There is no stored flag, so it tracks `censor` / `uncensor` automatically. Staleness is **flagged, not auto-fixed**: re-running is the operator's call.

```bash
casetrack cohort-artifacts --project-dir .              # list all, with a fresh/STALE flag
casetrack cohort-artifacts --project-dir . --stale-only # only artifacts with a censored input
casetrack cohort-artifacts --project-dir . --fmt json   # table | tsv | json
```

Staleness is surfaced in every read path, so you rarely need a bespoke query:

- `casetrack status` — appends a cohort-artifact section (count + per-artifact fresh/STALE).
- `casetrack query` — exposes a `_cohort_artifacts` DuckDB view with the derived staleness column.
- `casetrack export --include-cohort-artifacts` — adds them to the TSV/JSON export.
- The HTML dashboard — a dedicated section.
- MCP — `mcp__casetrack__casetrack_cohort_artifacts(project_id="<slug>", stale_only=False)`.

### Nextflow

The cohort equivalent of `CASETRACK_REGISTER` is the `casetrack_append_cohort` process, wrapped by the `COHORT_ARTIFACT_TRACKED` subworkflow (`examples/nextflow/subworkflows/local/cohort_artifact_tracked.nf`). Stats are optional — the process drops `--stats` when handed `[]`; there's no `{}` placeholder file. Same `local` / `maxForks = 1` discipline as `CASETRACK_REGISTER`.

See `references/cohort-artifacts.md` for the full table schema, the staleness cascade in detail, and the Nextflow wiring.

## 16. Reference artifacts (proposal 0010)

Some analyses depend on **external versioned inputs**: a genome FASTA, a GTF annotation, a dbSNP VCF, a repeat-masker BED, an interval list. These aren't outputs of the pipeline — they're inputs. When the reference version changes (hg38_v0 → hg38_v1, dbsnp154 → dbsnp155), any existing output that used the old version is no longer reproducible against the new reference. casetrack 0010 tracks this with **two additive sibling tables** (`reference_artifacts` + `reference_usage`) so that bumping a version in TOML + running `schema apply` immediately marks downstream outputs as stale.

### How it works — TOML contract

References are declared in a `[references]` block; each analysis declares which refs it uses:

```toml
[references]
genome   = { path = "/data/hg38_v1/genome.fa",    version = "hg38_v1" }
dbsnp    = { path = "/data/hg38_v1/dbsnp155.vcf", version = "155" }
intervals = { path = "/data/hg38_v1/wgs.intervals.bed", version = "hg38_v1_wgs" }

[analyses.variant_call]
level          = "specimen"
column_prefix  = "vc"
summary_tsv    = "variant_call_summary.tsv"
uses           = ["genome", "dbsnp"]
```

`casetrack schema apply` **materialises** the `[references]` block into the `reference_artifacts` table (one row per `ref_key`). Staleness is keyed on the `version` string: if the `version` in TOML no longer matches the `version_used` recorded in `reference_usage`, the output is `STALE`.

### The two sibling tables

- `reference_artifacts` — one row per declared reference key (`ref_key`); carries `path`, `version`, `updated_at`. This is the **canonical current set**, materialized from TOML on every `schema apply`. It never accumulates history — `schema apply` is idempotent (upsert by `ref_key`).
- `reference_usage` — the many-to-many edge: which analysis output used which reference at which `version_used`. `scope` is `'analysis'` (per assay/specimen/patient row) or `'cohort'` (cohort artifact). One row per `(output_id, ref_key, scope)` — written by `append` / `append-cohort`.

### Three-state staleness (read-time, not stored)

Every output tracked in `reference_usage` carries one of three states, derived live:

| State | Meaning |
|---|---|
| `fresh` | All referenced `version_used` strings match the current `reference_artifacts.version` |
| `STALE` | At least one `version_used` no longer matches — includes the reason, e.g. `genome: hg38_v0 -> hg38_v1` |
| `untracked` | The output was registered without reference tracking (pre-0010 project, or `--no-track-references`) |

The staleness reason names the specific reference(s) that changed (`genome: hg38_v0 -> hg38_v1`) or were removed entirely (`reference removed: dbsnp`). There is no stored flag — staleness is recomputed on every read, so reverting a version bump in TOML + `schema apply` restores outputs to `fresh` automatically.

### Bump → STALE → revert flow

```
1. Bump version in TOML:  genome.version = "hg38_v0"  →  "hg38_v1"
2. casetrack schema apply  →  reference_artifacts.version updated to "hg38_v1"
3. casetrack references --stale-only  →  outputs that used hg38_v0 appear as STALE
4. Re-run the analysis with the new reference  →  append records version_used = "hg38_v1"
5. casetrack references  →  those outputs now show fresh
```

### Orthogonality with 0009

A cohort artifact (0009) can have **two independent staleness flags**:

- `input_stale` (0009) — a contributing assay was censored or consent-revoked.
- `ref_stale` (0010) — a reference version changed since the artifact was built.

Both are derived live; both appear in `casetrack cohort-artifacts` output and the `_cohort_artifacts` DuckDB view. An artifact can be `input_stale`, `ref_stale`, both, or neither — they don't interact.

### Capture paths

| Scenario | How references are captured |
|---|---|
| Normal `append` (analysis has `uses` in TOML) | Auto-captured from TOML — no extra flags needed |
| Override which refs to record at call time | `--uses-references genome,dbsnp` |
| Opt out entirely | `--no-track-references` (records `untracked`) |
| Cohort artifact via `append-cohort` | Same auto-capture from `[analyses.<name>].uses`; override with `--uses-references genome` |

### Read-path surfacing

Staleness is visible in every read path:

| Surface | How |
|---|---|
| `casetrack references [--stale-only] [--fmt table\|tsv\|json]` | Primary command — lists all outputs with their ref state + staleness reason |
| `casetrack status` | Reference-artifacts section (count fresh/STALE/untracked) |
| `casetrack query` | `_reference_usage` DuckDB view with derived state; `_cohort_artifacts` gains `ref_stale` column |
| `casetrack export --include-references` | Adds reference usage rows to TSV/JSON export |
| HTML dashboard | Dedicated reference-artifacts section |
| MCP (Claude Code) | `mcp__casetrack__casetrack_references(project_id="<slug>", stale_only=False)` |
| `casetrack validate` | Reports orphan `reference_usage` rows (ref_key in usage but absent from `reference_artifacts`) |

### Migration

Projects created before 0010 need a one-time migration:

```bash
casetrack migrate-references --project-dir .            # create the two tables + indexes
casetrack migrate-references --project-dir . --dry-run  # print the plan, change nothing
```

Projects created by a current `casetrack init` already have the tables (no migrate needed).

### Documented limitations

- **Staleness keys on the version string only.** If the reference file changes on disk but the `version` string in TOML is not bumped, staleness is not detected. A future `casetrack doctor --references` command will compare checksums, but checksumming is not in 0010.
- **Content drift without a version bump fires nothing** — documented in proposal 0010 §6.2.
- **Orphan rows** accumulate if a `ref_key` is removed from TOML entirely (the `reference_artifacts` row is deleted by the next `schema apply`, but `reference_usage` rows pointing to that key become orphans). `validate` reports them; `doctor` will prune them in a future release.

See `references/reference-artifacts.md` for the full table schema, the staleness algorithm in detail, and worked examples. Proposal: `docs/proposals/0010-reference-artifacts.md` (§6.2 staleness, §7 rejected alternatives).

## 17. When to read the references

Default to handling requests directly from this SKILL.md. Read reference files when:

- User wants a fully-worked TOML for a new project → `references/toml-example.md`
- User wants to integrate casetrack into a Nextflow pipeline → `references/nextflow-integration.md`
- User needs a specific SQL query (cohort progress matrix, samplesheet generation, QC timeline) → `references/common-queries.md`
- User is registering or troubleshooting **cohort-level artifacts** (joint VCFs, PoNs, matrices, staleness) → `references/cohort-artifacts.md`
- User is declaring/bumping/checking **reference artifacts** (genome, GTF, dbSNP, intervals) → `references/reference-artifacts.md`
- User hits an error not in §13 — full deep-dive patterns file → `references/patterns.md`