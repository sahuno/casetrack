# CLAUDE.md — casetrack

## What this project is

Manifest-centric case management CLI for bioinformatics pipelines on HPC (SLURM). Tracks which analyses have been completed — and which samples remain **usable** — across a multi-patient, multi-specimen, multi-assay cohort. Built for cancer genomics studies.

- **Repo**: https://github.com/sahuno/casetrack (private)
- **Author**: Samuel Ahuno (ekwame001@gmail.com / sahuno@mskcc.org)
- **Current release**: v0.9.0 — artifact-to-artifact lineage + transitive derived-staleness (proposal 0011)
- **Next release**: v1.0 (flat-mode removal)
- **HPC target**: IRIS @ MSKCC (SLURM, WekaFS shared storage, Apptainer containers)

## Key context files (read these to resume work)

| File | What it tells you |
|---|---|
| `docs/proposals/0011-artifact-to-artifact-lineage.md` | **The shipped artifact-lineage design.** Generic `derived-from` edge between any two lineage nodes; transitive `derived_stale` flag; `artifact_derivation` sibling table. §0 = nine locked decisions; §7 = rejected alternatives. |
| `docs/proposals/0010-reference-artifacts.md` | **The shipped reference-artifacts design.** Versioned upstream inputs (genome, GTF, dbSNP) with read-time downstream staleness via two additive sibling tables. §6.2 = three-state staleness + orthogonality to 0009; §7 = why no 4th level / no version-history table. |
| `docs/proposals/0009-cohort-level-artifacts.md` | **The shipped cohort-artifacts design.** One output from many assays (joint VCFs, PoNs, cohort matrices) via two additive sibling tables. §7 records *why a 4th hierarchy level was rejected* (Option A vs B). §9 = nothing open; fully implemented. |
| `docs/proposals/0003-init-scaffold.md` | **The shipped v0.4.2 design.** `casetrack init` now scaffolds 16 leaf directories + expanded .gitignore. `--bare` opts out. |
| `docs/proposals/0002-qc-events-and-censoring.md` | **The shipped v0.4 design.** QC events, cascade semantics, consent rules, cohort `--pair-by`. §0 has the 13 locked-in decisions. |
| `docs/proposals/0001-sqlite-normalized-backend.md` | **The shipped v0.3 design.** Three-level hierarchy, SQLite backend, concurrency strategy. |
| `docs/MIGRATION_v0.3_to_v0.4.md` | Step-by-step upgrade guide (one command: `casetrack migrate-qc`). |
| `CHANGELOG.md` | Release notes (most recent = v0.9.0). |
| `casetrack.py` | v0.3 commands, in one file (~5.6K lines). |
| `casetrack_qc/` | v0.4 QC subsystem — lives next to the monolith, never merged into it. |
| `README.md` | User-facing command table. |

## How to run

```bash
# Install (user-level)
pip install -e ".[all]" --user

# Run tests (~2 min)
python3 -m pytest tests/ -q
```

## Architecture

**v0.3**: Source of truth is SQLite. Three normalized tables: `patients` → `specimens` → `assays` with enforced foreign keys. TSV becomes an on-demand export (`casetrack export`). Provenance stays as `provenance.jsonl`. DuckDB queries attach the SQLite file via `sqlite_scanner`.

**v0.4**: Adds a QC / censoring / consent subsystem on top of v0.3:

- `qc_events` — append-only audit log of every censor / uncensor action.
- `qc_status` columns — fast-filter cache on each level; derivable from events.
- Consent columns on `patients` — constrained enum + cascade at read.
- All read paths (`status`, `rerun`, `export`, `query`, `dashboard`) are QC-aware by default.

v0.4 lives in `casetrack_qc/` (a new subpackage alongside `casetrack.py`). The monolith stays untouched except for integration hooks in `init` / `append` / `rerun` / `status` / `export` / `validate` / `recover` / `dashboard` and the argparse dispatch in `main()`.

**Cohort-level artifacts (proposal 0009)**: a first-class home for analysis outputs that span **many** assays (joint-genotyped VCFs, panels-of-normals, cohort matrices) — which the three-level hierarchy can't represent. Two **additive sibling tables** (the `qc_events` pattern; the three-level core is untouched):

- `cohort_artifacts` — one row per cohort output, keyed by `(analysis, run_tag)`.
- `cohort_artifact_inputs` — many-to-many lineage to contributing `assay_id`s.

**Staleness is read-time, not stored**: an artifact is `STALE` when any contributing assay is currently censored / consent-revoked, derived live from the §4.4 cascade (`cohort_artifacts.artifact_staleness`). Surfaced everywhere — the `cohort-artifacts` command, `status` (section), `query` (`_cohort_artifacts` DuckDB view), `export --include-cohort-artifacts`, the HTML dashboard, and the `casetrack_cohort_artifacts` MCP tool. The 4th-hierarchy-level alternative was **rejected** (cohort artifacts are derived / many-to-many / dynamically-membered; a level is biological / single-parent / static — see proposal 0009 §7). Code lives in `casetrack_qc/cohort_artifacts.py` (+ `_cli.py`); Nextflow side is `casetrack_append_cohort` + the `COHORT_ARTIFACT_TRACKED` subworkflow.

**Reference artifacts (proposal 0010)**: the mirror image of cohort artifacts — **upstream** versioned external inputs (genome, annotation, known-variant sets, repeats, intervals) whose version changes cascade *down* to invalidate the outputs that consumed them (0009 cascades *up* from censored samples). Two more **additive sibling tables**:

- `reference_artifacts` — the canonical "current" set, keyed by `ref_key`, materialized from a new TOML `[references]` block on `schema apply`.
- `reference_usage` — the edge: which output (sample-level analysis result, `scope='analysis'`; or cohort artifact, `scope='cohort'`) consumed which `ref_key` at which `version_used`.

Each `[analyses.<tool>]` declares `uses = [...]`; `append` auto-snapshots the current version of each. **Ref-staleness is read-time, three-state** (`fresh`/`STALE`/`untracked`) with a named reason, derived live in `casetrack_qc/reference_artifacts.py` (`output_staleness` / `all_stale_outputs`) — **orthogonal** to 0009's input-staleness (a cohort artifact carries both a `stale` and a distinct `ref_stale` flag). Surfaced in `references` + `migrate-references` commands, `status` (section), `query` (`_reference_usage` view + `_cohort_artifacts.ref_stale`), `export --include-references`, the dashboard, `validate` (orphan-usage), and the `casetrack_references` MCP tool. Version history lives in `provenance.jsonl` (`reference_version_change`), not the DB. The 4th-level and full-version-history alternatives were **rejected** (proposal 0010 §7). Code: `casetrack_qc/reference_artifacts.py` (+ `_cli.py`); Nextflow `casetrack_append_cohort` gains an optional `uses_references` input.

**Artifact-to-artifact lineage (proposal 0011)**: the recursive layer that 0009 and 0010 each lack — a generic `derived-from` edge between any two lineage nodes (`cohort:`, `reference:`, `analysis:`), making the lineage a multi-hop DAG and making staleness **transitive**. One more **additive sibling table**:

- `artifact_derivation` — `(down_node, up_node, note, recorded_at)` where each endpoint is a canonical node-ref string over the three node types.

**`derived_stale` is a third orthogonal flag**: a node is derived-stale when any upstream node it derives from is stale by *any* cause (input-stale, ref-stale, or derived-stale — recursively). Computed at read-time via visited-set traversal with a cycle guard (`casetrack_qc/artifact_derivation.py`). Surfaced in `derived-from` + `derivation` + `migrate-derivation` commands, `status` (section), `query` (`_artifact_derivation` view + `derived_stale` on `_cohort_artifacts`), `export --include-derivation`, the dashboard, `validate` (dangling + acyclic invariants), and the `casetrack_derivation` MCP tool. Edges declared in TOML via `[references.<key>].derived_from` or at registration time via `--derived-from` on `append`/`append-cohort`. History in `provenance.jsonl` (`artifact_derivation_link`); the table holds only current edges. The `derivation` command name was chosen because `lineage` is taken by proposal 0006 (assay-merge subsystem). Code: `casetrack_qc/artifact_derivation.py` (+ `_cli.py`); Nextflow `casetrack_append_cohort`/`casetrack_append_project` gain an optional `derived_from` input.

## Commands

| Group | Subcommands |
|---|---|
| v0.3 | `init`, `append`, `status`, `validate`, `log`, `schema`, `rerun`, `dashboard`, `add-metadata`, `projects`, `query`, `export`, `migrate`, `register`, `doctor`, `recover` |
| v0.4 QC | `censor`, `uncensor`, `qc-history`, `migrate-qc`, `cohort` (readiness view) |
| later | `migrate-lineage`, `add-batch`, `link-sources`, `project`, `migrate-status` |
| cohort artifacts (0009) | `append-cohort`, `cohort-artifacts`, `migrate-cohort` |
| reference artifacts (0010) | `references`, `migrate-references` (+ `append`/`append-cohort` `--uses-references`) |
| derivation / lineage (0011) | `derived-from`, `derivation`, `migrate-derivation` (+ `append`/`append-cohort` `--derived-from`) |

Note: `cohort` (v0.4) is the paired-design *readiness* view; `cohort-artifacts` (0009) lists cohort-level *output artifacts* with staleness — different things.

## Accepted design decisions (v0.4)

See §0 of proposal 0002 for the full list. Key ones:

1. **Hybrid storage** — `qc_events` audit + materialized `qc_status` cache.
2. **Three-level whole-entity QC only** — per-analysis censoring deferred to a future proposal.
3. **Consent distinct from QC** — separate `--include-consent-revoked` flag; ethics-override gate on reversal.
4. **SLURM auto-flag via summary TSV** — if `qc_pass` / `qc_fail_reason` / `qc_warn` columns appear, `append` consumes them and emits events inside the same transaction.
5. **Append-only reversal** — `uncensor` writes `resolved_at`, never deletes.
6. **Strict refuse on append to censored** — `--force-append-on-censored --yes` override.
7. **N-partition `--pair-by`** — same code path for tumor/normal, longitudinal, multi-region.

## Accepted design decisions (cohort artifacts, proposal 0009)

1. **Sibling tables, not a 4th level** — `cohort_artifacts` + `cohort_artifact_inputs`, mirroring the `qc_events` additive pattern. The three-level core (`LEVEL_ORDER`) is untouched. (§7 has the full Option-A-vs-B reasoning.)
2. **`(analysis, run_tag)` is the unique key** — a re-genotyping run uses a new `run_tag` and coexists with the prior artifact in the audit trail.
3. **Read-time staleness, no stored flag** — derived live from the QC/consent cascade, so it tracks censor/uncensor automatically.
4. **Staleness is flagged, not auto-fixed** — re-running is the operator's call.
5. **Nextflow stats are optional** — `casetrack_append_cohort` drops `--stats` when handed `[]`; no `{}` placeholder file.

## Accepted design decisions (v0.3)

Still all valid. Three levels hardcoded, strict FK, WAL + busy_timeout Tier-1 concurrency, `casetrack.db` gitignored, schema-in-TOML, infer-then-override analysis column types.

## Conventions

- **`_done` columns**: every analysis gets a `{analysis}_done` timestamp.
- **`--allow-new --yes` pair**: adding new IDs requires double opt-in.
- **`--force-append-on-censored --yes` pair** (v0.4): landing data on a censored entity requires double opt-in.
- **`--ethics-override --yes` pair** (v0.4): reversing a `consent_revoked` event requires double opt-in AND a reason with an IRB ref / re-consent phrasing / ISO date.
- **Summarize scripts**: each analysis produces a per-assay TSV keyed on `assay_id`. Add `qc_pass` / `qc_fail_reason` / `qc_warn` columns to the summary TSV to auto-emit QC events on append.
- **Three-phase SLURM pattern**: (1) run tool, (2) summarize to TSV, (3) `casetrack append`.
- **Provenance is mandatory**: every mutation logs to JSONL. v0.4 adds `censor` / `uncensor` / `ethics_override` / `migrate_qc` actions.

## Test suite

940 pytest tests (~10 min full run on a loaded node). Run with `python3 -m pytest tests/ -q`.

Tests cover: every subcommand, smart-merge correctness, 5000-sample perf regression, concurrent appends via multiprocessing, git provenance capture, Nextflow module shell contract, Claude Code hook, DuckDB query paths, **every v0.4 QC path (schema, events CRUD, censor/uncensor/qc-history CLI, autoflag, strict-refuse, rerun/status/export filters, validate invariants, ethics override, recover round-trip, migrate-qc, cohort + pair-by)**.

CI: GitHub Actions matrix over Python 3.10–3.13 on push + PR.

## Project layout

```
casetrack/
├── casetrack.py              # v0.3 commands (single file, ~5.6K lines)
├── casetrack_qc/             # v0.4 QC subsystem (new subpackage)
│   ├── __init__.py
│   ├── schema.py             # qc_events DDL + qc_status cols + TOML parsing
│   ├── events.py             # insert / query / resolve; derive_status
│   ├── censor.py             # cmd_censor, cmd_uncensor, cmd_qc_history
│   ├── consent.py            # consent updates + ethics regex + invariant
│   ├── autoflag.py           # SLURM summary-TSV convention
│   ├── reader.py             # _active cascade (§4.4) + DuckDB view
│   ├── cohort.py             # cmd_cohort + pair-by N-partition (readiness view)
│   ├── cohort_artifacts.py   # 0009: sibling-table DDL/CRUD + read-time staleness
│   ├── cohort_artifacts_cli.py # 0009: append-cohort / migrate-cohort / cohort-artifacts
│   ├── reference_artifacts.py # 0010: reference DDL/CRUD + ref-staleness
│   ├── reference_artifacts_cli.py # 0010: references / migrate-references
│   ├── artifact_derivation.py # 0011: derived-from edges + transitive derived-staleness
│   ├── artifact_derivation_cli.py # 0011: derived-from / derivation / migrate-derivation
│   ├── migrate.py            # cmd_migrate_qc
│   ├── recover.py            # replay helpers for QC actions
│   └── cli.py                # argparse wiring helpers
├── casetrack_mcp/            # MCP server (list_projects / query / cohort_artifacts tools)
├── setup.py                  # pip entry_points + packages=["casetrack_qc"]
├── CLAUDE.md                 # you are here
├── README.md                 # user-facing docs
├── CHANGELOG.md              # release notes
├── docs/
│   ├── CASETRACK_SYNOPSIS.md
│   ├── MIGRATION_v0.2_to_v0.3.md
│   ├── MIGRATION_v0.3_to_v0.4.md   # ← new
│   └── proposals/
│       └── 0001 … 0011  (0009 = cohort artifacts, 0010 = reference artifacts, 0011 = artifact-to-artifact lineage)
├── examples/
│   ├── run_modkit.sh
│   ├── scripts/
│   ├── nextflow/             # casetrack.nf module + subworkflows/local/cohort_artifact_tracked.nf
│   ├── claude/
│   └── giab_chr21/           # run_cohort_demo.sh (mock + bcftools engines)
├── scripts/
│   └── generate_demo_dashboard.py
├── tests/                    # 940 tests (incl. cohort/reference/derivation artifacts: schema/CRUD/staleness/CLI/read-paths/nf)
└── sandbox/
```

## GitHub state

| Item | Link |
|---|---|
| Repo | https://github.com/sahuno/casetrack |
| Release | v0.4.0 (planned tag after this phase merges) |
| Pages | https://sahuno.github.io/casetrack/ |
| Tracking issue | [#10 — v0.4.0 implementation](https://github.com/sahuno/casetrack/issues/10) |
| CI | `.github/workflows/tests.yml` (Python 3.10–3.13) |
| Pages workflow | `.github/workflows/pages.yml` |

## What to tell a new session

> Read `docs/proposals/0002-qc-events-and-censoring.md` (§0 has the locked decisions) and `docs/MIGRATION_v0.3_to_v0.4.md`. The v0.4 QC code lives in `casetrack_qc/`. Integration points in `casetrack.py`: `cmd_init_project`, `cmd_append_project`, `cmd_rerun_project`, `cmd_status_project`, `cmd_export_project`, `cmd_validate_project`, `cmd_recover_project`, `cmd_dashboard_project`, plus the argparse dispatch in `main()`.
>
> For cohort-level artifacts: read `docs/proposals/0009-cohort-level-artifacts.md` (§7 = why no 4th level; §9 = nothing open). Code is `casetrack_qc/cohort_artifacts.py` (+ `_cli.py`); `casetrack_mcp/` has the agent-facing tool. casetrack.py read-path hooks: `cmd_dashboard_project` (section), `_prepare_v03_query_connection` (`_cohort_artifacts` view), `cmd_status_project` + `cmd_export_project`. The Nextflow process/subworkflow live in `examples/nextflow/`.
>
> For reference artifacts: read `docs/proposals/0010-reference-artifacts.md` (§6.2 = three-state staleness + orthogonality to 0009; §7 = rejected alternatives). Code is `casetrack_qc/reference_artifacts.py` (+ `_cli.py`); the `_reference_usage` view + `_cohort_artifacts.ref_stale` live in `casetrack_qc/reader.py`. casetrack.py hooks: init schema in `cmd_init_project`, sync in `_schema_apply`, capture in `cmd_append_project` (`capture_reference_usage`), `_emit_references_section` (status), export `--include-references`, dashboard `_references_html`, validate orphan check. MCP tool `casetrack_references` in `casetrack_mcp/`. The version contract is TOML `[references]` → `reference_artifacts`; bump a `version` + `schema apply` to flip outputs stale.
>
> For artifact-to-artifact lineage: read `docs/proposals/0011-artifact-to-artifact-lineage.md` (§0 = nine locked decisions; §7 = rejected alternatives). Code is `casetrack_qc/artifact_derivation.py` (+ `_cli.py`); the `_artifact_derivation` view + `derived_stale` on `_cohort_artifacts` live in `casetrack_qc/reader.py`. casetrack.py hooks: init schema in `cmd_init_project`, capture via `--derived-from` in `cmd_append_project`/`cmd_append_cohort`, `_emit_derivation_section` (status), export `--include-derivation`, dashboard `_derivation_html`, validate dangling + acyclic checks. MCP tool `casetrack_derivation` in `casetrack_mcp/`. The `derivation` command inspects the DAG; `derived-from` adds edges; `migrate-derivation` retrofits. Command name is `derivation` (not `lineage`) because 0006 already owns `casetrack lineage`.
