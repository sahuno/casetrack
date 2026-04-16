# Changelog

All notable changes to `casetrack` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
