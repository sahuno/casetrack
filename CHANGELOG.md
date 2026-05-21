# Changelog

All notable changes to `casetrack` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] — 2026-05-21

Reference artifacts — versioned, per-file external inputs (genome, annotation,
known-variant sets, repeats, intervals) with read-time downstream staleness:
bump a reference's version and every output that consumed the old version reads
`STALE`. The mirror image of cohort-artifact staleness (0009 cascades *up* from
censored samples; 0010 cascades *down* from changed references). See
[proposal 0010](docs/proposals/0010-reference-artifacts.md).

### Added

- **`reference_artifacts` + `reference_usage` tables** — two additive sibling
  tables (the `qc_events` / 0009 pattern), created by `casetrack init`; the
  three-level core and the 0009 tables are untouched. `reference_artifacts` is
  the canonical "current" set, materialized from a new TOML `[references]`
  block on `schema apply`; `reference_usage` is the edge recording which output
  (sample-level analysis result or cohort artifact) consumed which reference at
  which version. The 4th-hierarchy-level and full-version-history alternatives
  were rejected (proposal 0010 §7).
- **TOML `[references]` block + `[analyses.<tool>].uses`** — declare each
  reference once (`path` / `version` / optional `kind` / optional `checksum`);
  each analysis declares the `ref_key`s it consumes. `append` auto-snapshots the
  current version of each at production time.
- **Read-time three-state staleness** — `fresh` / `STALE` / `untracked`, derived
  live with a named reason (`genome: hg38_v0 -> hg38_v1`, or `reference removed:
  dbsnp`). No stored flag — flip a version in TOML + `schema apply` and outputs
  read `STALE`; revert and they read `fresh` again. Orthogonal to 0009's
  input-staleness: a cohort artifact can be input-stale, ref-stale, both, or
  neither (the `_cohort_artifacts` view gains a distinct `ref_stale` column).
- **`casetrack migrate-references`** — additive, idempotent retrofit of the two
  tables onto a pre-0010 project (mirrors `migrate-cohort`).
- **`casetrack references [--fmt table|tsv|json] [--stale-only]`** — list the
  canonical reference set; `--stale-only` drills into which outputs are stale
  against which references (`used -> current`).
- **`append` / `append-cohort` capture** — `append` auto-records usage from
  `[analyses.<tool>].uses`; `--uses-references genome,dbsnp` overrides for ad-hoc
  runs and `--no-track-references` opts out. `append-cohort --uses-references`
  records cohort-scope usage (cohort analyses aren't always in `[analyses]`).
- **Read-path surfacing** — a "References" section in `status` and the HTML
  `dashboard`; a `_reference_usage` DuckDB view (with derived `current_version` /
  `is_stale`) in `query`; `export --include-references` (auto-enabled for XLSX);
  a `validate` invariant flagging orphan usage rows; and the `casetrack_references`
  MCP tool.
- **Nextflow** — `casetrack_append_cohort` and the `COHORT_ARTIFACT_TRACKED`
  subworkflow gain an optional `uses_references` input (the `[]`-means-none
  pattern, mirroring the `stats` slot).

### Notes

- `schema apply` writes a `reference_version_change` provenance entry on every
  version/path move — the audit of reference evolution lives in
  `provenance.jsonl`, not the DB (`reference_artifacts` holds only current).
- Staleness keys on the `version` string only; bumping a file's content without
  bumping its `version` fires no staleness (a future `doctor --references` will
  compare the stored `checksum`).

## [0.7.0] — 2026-05-21

Cohort-level artifacts — a first-class home for analysis outputs that span many
samples (joint-genotyped VCFs, panels-of-normals, cohort matrices). See
[proposal 0009](docs/proposals/0009-cohort-level-artifacts.md).

### Added

- **`cohort_artifacts` + `cohort_artifact_inputs` tables** — additive sibling
  tables (the `qc_events` pattern), created by `casetrack init`; the three-level
  core is untouched. One row per cohort output, keyed by `(analysis, run_tag)`,
  with a many-to-many join to contributing `assay_id`s. The
  4th-hierarchy-level alternative was rejected (proposal 0009 §7).
- **`casetrack append-cohort`** — register a cohort artifact + its assay lineage
  in one transaction (`--inputs a,b` or `--inputs-from FILE`, optional `--stats`
  JSON and `--checksum`). Writes an `action='append_cohort'` provenance entry.
  A distinct `run_tag` gives v1/v2 of a re-genotyping run separate identity.
- **`casetrack cohort-artifacts`** — list artifacts with **read-time staleness**:
  an artifact is flagged `STALE` when any contributing assay is currently
  censored or consent-revoked, derived live from the QC/consent cascade
  (proposal 0002 §4.4) with no stored flag. `--stale-only`, `--fmt table|tsv|json`.
- **Staleness surfaced in the existing read paths** (proposal 0009 §4):
  `casetrack status` appends a cohort-artifact section (count + per-artifact
  fresh/STALE) in the human view; `casetrack query` exposes a `_cohort_artifacts`
  DuckDB view with derived `n_censored_inputs` / `stale` columns; `casetrack
  export --include-cohort-artifacts` writes the `cohort_artifacts` (with those
  derived columns) and `cohort_artifact_inputs` tables (auto-enabled for XLSX).
- **`casetrack migrate-cohort`** — additive migration to create the two tables on
  a pre-0009 project (`--dry-run` supported).
- **Dashboard** — `casetrack dashboard` renders a "Cohort artifacts" section with
  per-artifact `fresh`/`STALE` badges and the censored inputs behind each STALE.
- **MCP** — new `casetrack_cohort_artifacts` tool surfaces artifacts + derived
  staleness to AI agents (companion to the CLI command), so an agent doesn't have
  to hand-write the cascade SQL. The `casetrack_query` schema now points at the
  `cohort_artifacts` / `cohort_artifact_inputs` tables.
- **Packaged Nextflow subworkflow** `subworkflows/local/cohort_artifact_tracked.nf`
  (`COHORT_ARTIFACT_TRACKED`) — wraps the gather (`collectFile` lineage manifest)
  + `casetrack_append_cohort` registration as a reusable named DSL2 subworkflow.
  Verified end-to-end against real Nextflow.
- **`casetrack_append_cohort` stats are now optional** — pass `[]` in the stats
  slot and the `--stats` flag is dropped (no `{}` placeholder file needed); the
  `append-cohort` CLI was already stats-optional.
- **`casetrack_append_cohort` Nextflow process** (`examples/nextflow/casetrack.nf`)
  — the fan-in companion to `casetrack_append_project`; registers a cohort
  artifact + its assay lineage (passed as a `collectFile` inputs manifest).
- **`examples/giab_chr21/run_cohort_demo.sh`** — runnable cohort demo with two
  cheap engines (`--engine mock` zero-compute; `--engine bcftools` real
  multi-sample merge), ending on the censor → STALE cascade punchline.

## [0.6.1] — 2026-04-20

Completes proposal
[0006](docs/proposals/0006-assay-lineage-and-batch-tracking.md) (steps 4–6):
the lineage tables added in 0.6.0 now flow through the read paths.

### Added

- **Lineage-aware reads.** `casetrack rerun` resolves a derived assay's pending
  work back through its `assay_sources` so contributing source assays are visible
  to the scan; `casetrack status` surfaces per-entity lineage; `casetrack export
  --include-lineage` writes the `assay_sources` + `batches` tables (auto-enabled
  for XLSX as additional sheets).

## [0.6.0] — 2026-04-20

Project identity, hierarchy-ID enforcement, an MCP server, assay lineage, and
project lifecycle status. Implements proposals
[0005](docs/proposals/0005-id-format-and-project-identity.md),
[0006](docs/proposals/0006-assay-lineage-and-batch-tracking.md), and
[0007](docs/proposals/0007-project-lifecycle-status.md).

### Added

- **Project identity (proposal 0005 Part B).** Every project gets a stable
  `project_id` (DNS-slug form, e.g. `project-17424`), persisted in a `project_meta`
  row and recorded in a user-level registry at `~/.casetrack/registry.json`.
  `casetrack projects` gains `scan` / `list` / `register` / `deregister`
  subactions. `casetrack migrate-project-id` brings legacy v0.5 projects into the
  scheme; a hard-error gate refuses end-user commands on un-migrated projects
  (env-var bypass during the alpha rollout).
- **Hierarchy ID format enforcement (proposal 0005 Part A).** patient / specimen /
  assay IDs are validated against a declared pattern at registration; `[project]
  allow_unicode_ids` and `allow_case_variants` opt out. `casetrack doctor
  --id-format` scans an existing project for non-conforming IDs.
- **MCP server `casetrack_mcp/` (proposal 0005 §5.6).** A stdio server exposing
  `casetrack_list_projects` and `casetrack_query` tools to AI agents, installed via
  the optional `mcp` extra.
- **Assay lineage + batch tracking (proposal 0006 steps 1–3).** New `assay_sources`
  (many-to-many source → derived assay links) and `batches` tables model
  pre-merge flowcell/lane runs feeding a merged specimen-level assay, populated by
  `casetrack migrate-lineage`, `add-batch`, and `link-sources` (in the
  `casetrack_lineage` subpackage). The lineage-aware read paths follow in 0.6.1.
- **Project lifecycle status (proposal 0007).** The `casetrack_lifecycle` subpackage
  adds a `casetrack project` command (with `set-status` / `status`) tracking each
  project as `active` / `complete` / `archived`, plus `casetrack migrate-status` to
  backfill the lifecycle state on existing projects; `casetrack projects --status`
  filters the cross-project list by lifecycle state.

### Changed

- `setup.py` advertises the optional `casetrack-mcp` stdio entry point (requires
  the `mcp` extra).

## [0.5.0] — 2026-04-18

Tool-first results directory convention + one-flag `append` via path inference.

### Added

- **Optional `[layout]` and `[analyses]` sections in `casetrack.toml`.**
  `[layout.path_templates]` declares per-level directory templates for
  results (default:
  `assay = "{tool}/{run_tag}/{patient_id}/{specimen_id}/{assay_id}"`).
  `[analyses.<tool>]` declares each tool the pipeline runs, with its
  `level`, `column_prefix`, and `summary_tsv`. Both sections are additive
  and validated; existing projects keep working without them.
- **`casetrack append --infer-from-path [PATH]`** — walks up from PATH
  (default: `$PWD`) to find the project root, matches the path against
  `[layout.path_templates]`, and populates `--project-dir`, `--level`,
  `--analysis`, `--column-prefix`, and `--results` from the matched
  `[analyses.<tool>]` declaration. Explicit flags still override. A
  `run_tag` column is injected into the summary TSV so the run identifier
  flows through the normal `--column-prefix` pathway
  (→ `{prefix}_run_tag`). Provenance captures `run_tag` as a first-class
  field.
- All three shipped templates (`blank`, `hgsoc`, `giab_ont`) include a
  default `[layout]` block plus commented `[analyses.<tool>]` examples
  appropriate to the cohort.

### Changed

- `append`'s mutually-exclusive `--manifest` / `--project-dir` pair is no
  longer required at argparse level, since `--infer-from-path` can supply
  `--project-dir`. `cmd_append` enforces "one of the three is set".
- `--analysis` and `--results` are no longer required at argparse level
  when `--infer-from-path` is used.

### Tests

- `tests/test_path_infer.py` — 18 new tests covering `[layout]` /
  `[analyses]` validation, `find_project_root` walk-up, `infer_from_path`
  happy and error paths (unknown tool, level mismatch, outside results
  root, deepest-first template resolution), and the end-to-end `append
  --infer-from-path` flow including explicit-flag overrides.
- Full suite: 566 passing.

## [0.4.2] — 2026-04-18

Full project-directory scaffold on `casetrack init` (proposal 0003).

### Added

- **`casetrack init` scaffolds a full project tree by default.** Besides the
  four enforced files (`casetrack.toml`, `casetrack.db`, `provenance.jsonl`,
  `.gitignore`), init now lays out 16 leaf directories — `data/{raw,ref,
  validation}/`, `results/`, `scripts/`, `docs/{research,hypothesis}/`,
  `manuscript/{figures/scripts/{png,pdf,svg},draft,proofs,references}/`,
  `logs/`, `containers/`, `sandbox/` — with a `.gitkeep` in each leaf so the
  tree round-trips through git. Scaffolding is idempotent: re-running init
  on an existing project fills in missing leaves and leaves existing
  `.gitkeep` mtimes alone.
- **`casetrack init --bare`** opts out of the scaffold for users
  retrofitting projects with their own layout. Emits only the four
  enforced files.
- Provenance entry for `init_project` now records `scaffold: full|bare`
  and the list of leaves created.

### Changed

- Default `.gitignore` expanded to cover large analysis artifacts
  (`data/raw/*`, `containers/*.sif`, `results/**/*.{bam,cram,bedMethyl.gz,
  vcf.gz,fastq.gz}`, `sandbox/*`) with `!…/.gitkeep` negations so empty
  scaffold leaves stay visible to git.

### Docs

- `docs/proposals/0003-init-scaffold.md` — full design doc.

## [0.4.1] — 2026-04-18

One new flag + a worked real-cohort example.

### Added

- **`casetrack append --column-prefix P`** — rename every analysis column
  in the results TSV to `{P}_{name}` on the way in, so two analyses at
  different scopes (e.g. `merged` vs `chr17`) can't silently clobber each
  other under fill-only COALESCE. Key column, v0.4 autoflag columns
  (`qc_pass` / `qc_fail_reason` / `qc_warn`), and the `{analysis}_done`
  timestamp are never prefixed. `--col-type` still matches the TSV's
  original (pre-prefix) names. Prefix is validated as a plain identifier
  and recorded in provenance (`column_prefix`, `prefix_rename` fields).
  +7 tests (542 passing, was 535).
- **`examples/patterns/premerge_runs/`** — reusable pattern for cohorts
  with per-specimen pre-merge BAMs (flowcell runs, lanes). Pre-merge QC
  at assay level → `_active` cascade excludes bad flowcells → `samtools
  merge` at specimen level → downstream analyses (modkit, sniffles, …)
  run on the merged BAM. Ships with a `subset_chr` helper for fast
  per-chromosome iteration. Verified end-to-end against Project_17424
  (6 patients × 2 flowcells, real ONT data).
- **`examples/project_17424/`** — bootstrap + README for the MSKCC
  Project_17424 tumor cohort, wired to the pre-merge-runs pattern.

### Changed

- `examples/patterns/premerge_runs/`: summarizers write pre-prefixed
  columns by convention (merged_*, chr17_*) to avoid collisions. The new
  `--column-prefix` flag is the recommended approach for new work; both
  coexist.

## [0.4.0] — 2026-04-17

QC / censoring / consent subsystem. Implements
[proposal 0002](docs/proposals/0002-qc-events-and-censoring.md). New
commands + QC-aware defaults on every existing read path. 522 pytest tests
(112 new). Flat-manifest mode unchanged (still slated for removal in v1.0).

### Added

- **New subpackage `casetrack_qc/`** — hybrid layout next to the existing
  single-file `casetrack.py`. Existing v0.3 commands live in the monolith
  untouched; the new subsystem lives in its own package.
- **`qc_events` table** — append-only audit log of every censor / uncensor /
  migrate_qc action. Indexed by `(level, entity_id)` and by the active
  subset. Every row links to a `provenance.jsonl` entry via `transaction_id`.
- **Materialized `qc_status` columns** on `patients` / `specimens` / `assays`
  for fast filters. Rebuildable from events via `casetrack recover`.
- **Consent columns on `patients`** — `consent_status` (enum:
  `consented`, `consented_limited_use`, `pending`, `revoked`, `withdrawn`,
  `consent_expired`, `deceased_pre_consent`), `consent_date`,
  `withdrawal_date`. Enforces the `consent_status='revoked'` ↔ active
  `qc_events` invariant.
- **`casetrack censor`** — manual or bulk censoring. Kinds default to the
  ONT-HGSOC-leaning set in §0 #12. `--from FILE` does bulk import with
  one provenance entry per event, all sharing one `transaction_id`.
- **`casetrack uncensor`** — resolve an active event. Consent reversal
  requires `--ethics-override --yes` AND a reason that mentions IRB ref /
  re-consent / ISO date. Logs `action='ethics_override'` with `ethics: true`.
- **`casetrack qc-history`** — per-entity or project-wide event list.
- **`casetrack migrate-qc`** — one-shot v0.3 → v0.4 upgrade. Migrates a
  legacy `qc_pass BOOLEAN` on assays into `qc_status` + `qc_events`, drops
  the legacy column, appends the default `[qc]` block to `casetrack.toml`.
  `--dry-run` previews the plan without touching the DB.
- **`casetrack cohort`** — readiness summary (§8.2) and paired-design view
  (§8.3). `--pair-by COL` partitions any `specimens` column into N buckets
  (tumor/normal via `tissue_site`, longitudinal via `timepoint`, multi-
  region, …). Status terminology: `complete`, `broken`, `incomplete`,
  `singleton`. `--require N` for N-of-M partial completeness.
- **SLURM auto-flag** — if a summary TSV contains `qc_pass` /
  `qc_fail_reason` / `qc_warn`, `casetrack append` consumes them in the
  same transaction, emitting `qc_events` with `source='slurm'` and
  `created_by='slurm:$SLURM_JOB_ID'`.
- **`_active` DuckDB view** on the `query` connection — same shape as `_`
  but with the §4.4 cascade applied.
- **`docs/MIGRATION_v0.3_to_v0.4.md`** — step-by-step upgrade guide.

### Changed

- **`casetrack init`** — adds the QC schema (table + columns) and the
  `[qc]` TOML block as part of project creation.
- **`casetrack append`** — strict-refuse on censored entities (exit 2);
  `--force-append-on-censored --yes` override. Autoflag columns are
  consumed, never promoted to analysis columns.
- **`casetrack rerun`** — default skips censored / consent-revoked;
  `--force-censored` includes them with a loud stderr warning.
- **`casetrack status`** — new `--usable` breakdown (§8.1); default counts
  exclude fail + consent-revoked; `--include-censored` /
  `--include-consent-revoked` opt back in.
- **`casetrack export`** — default excludes fail + consent-revoked and
  prints a stderr audit line summarizing filters.
- **`casetrack validate`** — now also checks the consent invariant, orphan
  active events, and `qc_status` ↔ active-events consistency.
- **`casetrack dashboard`** — QC chips next to the cohort metrics and a
  dedicated "Excluded (active QC events)" section; no chips on v0.3-era
  projects that haven't been migrated (backward-compatible).
- **`casetrack recover`** — replays `censor`, `uncensor`, `ethics_override`,
  `migrate_qc` provenance actions into byte-equivalent state.
- **`setup.py`** — bumps version to 0.4.0 and installs the
  `casetrack_qc` subpackage alongside the `casetrack` module.

### Deferred (future proposals)

- Per-analysis censoring (the "fine for modkit, bad for xtea on the same
  assay" case) — whole-entity only in v0.4.
- `assays.batch_id` first-class column + `batches` table —
  `batch_effect_flagged` ships as a free-text-reason kind for now.

## [0.3.1] — 2026-04-16

Docs, demo, and SLURM-wrapper polish. No library behavior changes; the
v0.3 CLI surface is unchanged.

### Added

- **`examples/giab_chr21/`** — real-data demo on the Genome-in-a-Bottle
  ONT cohort (HG002 + HG006 × two flowcells × chr21-restricted BAMs).
  Runs both paths:
  - **Mock demo** (`run_mock_demo.sh`): end-to-end in under a minute,
    no cluster required — init, register, synthesize deterministic
    flagstat/modkit/sniffles summaries, append, dashboard, example query.
  - **Real SLURM pipeline** (`slurm/run_{flagstat,modkit,sniffles}.sh` +
    `submit_all.sh`): three-phase wrappers with apptainer-or-native
    tool support and `DEMO_SCRIPTS_DIR` to survive SLURM's
    `/var/spool/slurmd/scripts/` staging. Committed + verified against
    the real GIAB chr21 BAMs end-to-end (3 analyses × 4 assays).
- **`giab_ont` TOML template** — patient/specimen/assay schema tuned for
  ONT reference cohorts: trio_role, reference_source, cell_line,
  chemistry (R9/R10 enum), basecaller_model, flowcell_id, bam_path.
- **`examples/giab_chr21/scripts/summarize_{flagstat,modkit,sniffles}.py`**
  — real parsers for each tool's output; mock equivalents live alongside.
- **`submit_all.sh` snapshots wrapper scripts** into `<project>/scripts/`
  along with the casetrack git commit hash (`.source_commit`) and a
  `.source_dirty` marker. Makes the project dir self-documenting when
  read in isolation. Convenience for demo/repo-adjacent use cases; not
  required for users with their own versioned pipeline repos.
- **`tests/test_giab_ont_demo.py`** (+11 tests, total 410) — template
  parse, bootstrap idempotency, deterministic mock summarizers, real
  parsers exercised against canned fixtures (flagstat text, bedMethyl,
  sniffles VCF plain + gzipped).

### Changed

- **SLURM wrappers**: `#SBATCH --account=greenbab --partition=componc_cpu`
  defaults; `run_modkit.sh` bumped to 64 GB / 8 CPUs / 8 h walltime.
- **`run_modkit.sh`**: uses modkit 0.6+ syntax — `--modified-bases
  5mC 5hmC --cpg --reference REF` (was the older `--ref --cpg` form).
- **`README.md`**: new "How people actually use this" section with the
  three-layer usage model (package / project / pipeline) and three
  recommended patterns by user shape.
- **`docs/MIGRATION_v0.2_to_v0.3.md`**: prepended "First — how the
  pieces fit together" to make the same model explicit up front.

### Fixed

- `run_flagstat.sh` / `run_modkit.sh` / `run_sniffles.sh` now require
  `DEMO_SCRIPTS_DIR` (exported by `submit_all.sh`) to locate the
  summarizer scripts, since SLURM copies the submitted run script
  out of the repo to `/var/spool/slurmd/scripts/` and
  `${BASH_SOURCE[0]}` no longer points at the repo path. Root-cause
  fix caught by four early GIAB test jobs that failed at Phase 2.

## [0.3.0] — 2026-04-16

SQLite-backed project mode. 399 pytest tests. Flat-manifest mode remains
supported but prints a loud deprecation warning; slated for removal in
v1.0 (~6 months post-v0.3).

### Added

- **Project mode.** `casetrack init --project-dir DIR [--from-template
  {blank,hgsoc}]` creates a directory with `casetrack.toml` (declared
  schema), `casetrack.db` (SQLite, WAL + FK enforcement + busy_timeout),
  `provenance.jsonl` (append-only audit log), and a `.gitignore` that
  excludes the DB + WAL/SHM.
- **`casetrack migrate`** — one-shot conversion of a v0.2 flat manifest
  into a v0.3 project. Column routing is "constant-within-group, coarsest
  level wins"; `--metadata-map` overrides. Writes
  `.migration_report.{tsv,md}` and preserves the source TSV under
  `sandbox/`.
- **`casetrack register`** — single-row INSERT at any level with strict
  foreign-key enforcement. Missing parent → exit 2; opt in to inline
  creation with `--allow-new-parent --yes`.
- **`casetrack append --project-dir`** — dynamic `ALTER TABLE ADD COLUMN`
  inside a BEGIN IMMEDIATE transaction; Q7 hybrid type inference with
  `--col-type name:TYPE,…` overrides; fill-only default via COALESCE,
  `--overwrite` for unconditional writes.
- **`casetrack add-metadata --project-dir`** — bulk UPDATE + opt-in bulk
  INSERT (`--allow-new --yes`) against declared schema columns.
- **`casetrack status --project-dir`** — `--group-by {analysis,assay,
  specimen,patient}` with table/TSV/JSON output.
- **`casetrack validate --project-dir`** — TOML↔DB drift detection, FK
  integrity via `PRAGMA foreign_key_check`, orphan `_done` columns
  cross-referenced against provenance.
- **`casetrack log --project-dir`** — provenance viewer with `--level L`,
  `--transaction TX`, `--last N` filters.
- **`casetrack schema --project-dir {show,dump,check,apply}`** — lifecycle
  for the TOML↔DB schema: show current TOML, regenerate from DB, check
  drift, apply declared changes and bump `schema_v`.
- **`casetrack query --project-dir`** — DuckDB ATTACH of casetrack.db
  READ_ONLY, with `patients`/`specimens`/`assays` views plus
  `_ = assays ⋈ specimens ⋈ patients` for the cohort view.
- **`casetrack export --project-dir`** — `--shape {tables,joined}`,
  `--tables p,s,a` subset, `--sql "SELECT …"` passthrough; writes TSV/
  CSV/JSON/XLSX/Parquet (format inferred from `--output` extension).
- **`casetrack dashboard --project-dir`** — nested HTML: one `<details>`
  per patient → per specimen → assay table with per-analysis completion
  cells. Fully self-contained (no external CSS/JS).
- **`casetrack rerun --project-dir`** — lists/dispatches sbatch jobs for
  rows missing an analysis, with `--level` to pick which table to scan.
- **`casetrack projects --root`** — now detects v0.3 projects
  (`casetrack.toml` + `casetrack.db`) alongside v0.2 flat manifests.
- **`casetrack doctor --project-dir`** — Tier-1 concurrency stress test.
  Forks N workers × M INSERTs; exits non-zero on CORRUPT / MISUSE /
  partial commit.
- **`casetrack recover --project-dir`** — rebuild `casetrack.db` by
  replaying `provenance.jsonl`. Register/init entries self-contained;
  append/add-metadata/migrate entries re-read the recorded source TSV
  with checksum verification. `--permit-partial` allows partial rebuilds.

### Changed

- `casetrack.py` version bumped to 0.3.0 in `setup.py`.
- `setup.py` pins duckdb as a required install dep (not optional); adds
  `tomli>=2.0` for Python 3.10 (tomllib is stdlib on 3.11+).
- `python_requires` raised to `>=3.10`.
- Every `--manifest` invocation prints a one-shot deprecation warning to
  stderr. Silence with `CASETRACK_NO_DEPRECATION=1`.
- `casetrack projects` TSV output adds a `kind` column ("v0.2" / "v0.3").

### Fixed (from v0.3-alpha hardening)

- `cmd_append` (flat mode) no longer emits a pandas FutureWarning when
  assigning timestamp strings into an all-NaN float64 column.
- `casetrack projects` now fails hard on unparseable manifests instead of
  warn-and-continue.

### Documentation

- `docs/MIGRATION_v0.2_to_v0.3.md` — migration guide with CLI cheatsheet.
- `README.md` refreshed with dual-mode quick starts and updated command
  table.
- Nextflow module gains `casetrack_append_project`,
  `casetrack_register_project`, and `casetrack_add_metadata_project`
  alongside the v0.2 processes.
- Claude post-analysis hook accepts `PROJECT_DIR` in place of `MANIFEST`.
- `examples/run_modkit.sh` — phase-3 line now branches on `PROJECT_DIR`.

## [0.2.0] — 2026-04-15

First release past the prototype. 152 pytest tests, eight signed commits,
same single-file `casetrack.py` module.

### Added

- **`casetrack rerun`** — emit or submit sbatch commands for samples
  missing a given analysis. `--submit` dispatches and captures each
  SLURM job id into the provenance log; `--list-only` prints bare IDs
  for piping; `--extra` appends extra sbatch args.
- **`casetrack dashboard`** — generate a self-contained HTML report
  (summary metrics, per-analysis progress bars with expandable missing-
  samples lists, sample × analysis heatmap, provenance timeline with
  git short-hash). Fully offline, no external URLs, XSS-safe.
- **`casetrack add-metadata`** — attach metadata columns post-init. No
  `_done` timestamp, no schema entry. Strict collision policy by
  default; opt in via `--fill-only` or `--overwrite`.
- **`casetrack projects`** — cross-project overview. Walks a root for
  manifests matching `--pattern` up to `--max-depth`, skips hidden dirs
  and `sandbox/`. Table (with progress bars), tsv, or json output. One
  corrupted manifest warns + is skipped; does not abort.
- **Git provenance** — every log entry now carries a `git` block with
  `{commit, branch, dirty, toplevel}` of the process CWD. Fail-safe
  (missing git / non-repo / opt-out → `null`). Per-process cache keeps
  parallel appends fast. Dashboard surfaces the short hash inline.
- **`--yes` safety rail on `--allow-new`** — `append` and
  `add-metadata` now refuse to commit new sample rows unless `--yes`
  is also passed, previewing the IDs that would be added. Prevents
  typo'd sample IDs from silently expanding the manifest. Nextflow
  module pairs `--allow-new --yes` whenever
  `params.casetrack_allow_new = true`.
- **Nextflow DSL2 integration** (`examples/nextflow/`) — reusable
  `casetrack_append` + `casetrack_add_metadata` processes, a demo
  pipeline fanning two summarize steps through a single append gate,
  profiles for `standard` / `slurm` / `apptainer` / `test`, and a
  README covering three integration patterns (standalone,
  `afterScript`, collect-and-batch).
- **Claude Code QC hook** (`examples/claude/`) — Level 2 post-analysis
  shell hook. Invokes `claude --print` on a freshly-appended result,
  validates the returned TSV header, and appends the verdict as a
  `cc_<analysis>_review` analysis. Editable prompt template with
  `__SAMPLE_ID__` / `__ANALYSIS__` / `__RESULTS_TSV__` placeholders.
- **pytest suite** (`tests/`) — 152 tests covering every subcommand,
  concurrency (real multi-process `append`), git provenance, Nextflow
  module shell contract, Claude hook end-to-end with stubbed `claude`,
  and a 5000-sample smart-merge perf regression.

### Changed

- **Smart-merge is vectorized.** The `iterrows()` loop in the NaN-fill
  code path became `fill_nan_cells()`, a column-wise `merged_keys.map()`.
  A 5000 sample × 10 column fill went from minutes to ~0.05s.
- **Project tree reorganized** to match the synopsis layout:
  `docs/` for design notes, `examples/{scripts,nextflow,claude}/` for
  runnable artifacts, `sandbox/` for egg-info leftovers (gitignored
  for regenerables).
- **README rewritten** to document every current command, the three-
  phase SLURM pattern, the `--yes` rail, provenance shape, and the two
  integrations.

### Fixed

- `--allow-new` no longer silently admits typo'd sample IDs (was known
  issue #7 in the synopsis).

### Infrastructure

- Initial `git init` and first remote push to
  https://github.com/sahuno/casetrack (private).
- `.gitignore` for pyc / egg-info / pytest caches.

## [0.1.0] — prototype (pre-2026-04-15)

Single-file prototype with seven subcommands (`init`, `append`, `status`,
`validate`, `log`, `schema`, `export`), POSIX-flock-protected concurrent
append, and an append-only manifest model with a JSONL provenance sidecar
and a JSON schema sidecar. No tests.
