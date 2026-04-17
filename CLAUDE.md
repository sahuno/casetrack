# CLAUDE.md — casetrack

## What this project is

Manifest-centric case management CLI for bioinformatics pipelines on HPC (SLURM). Tracks which analyses have been completed — and which samples remain **usable** — across a multi-patient, multi-specimen, multi-assay cohort. Built for cancer genomics studies.

- **Repo**: https://github.com/sahuno/casetrack (private)
- **Author**: Samuel Ahuno (ekwame001@gmail.com / sahuno@mskcc.org)
- **Current release**: v0.4.0 — QC / censoring / consent subsystem on the v0.3 SQLite backend
- **Next release**: v0.5.x (assay-level `batch_id`) → v1.0 (flat-mode removal)
- **HPC target**: IRIS @ MSKCC (SLURM, WekaFS shared storage, Apptainer containers)

## Key context files (read these to resume work)

| File | What it tells you |
|---|---|
| `docs/proposals/0002-qc-events-and-censoring.md` | **The shipped v0.4 design.** QC events, cascade semantics, consent rules, cohort `--pair-by`. §0 has the 13 locked-in decisions. |
| `docs/proposals/0001-sqlite-normalized-backend.md` | **The shipped v0.3 design.** Three-level hierarchy, SQLite backend, concurrency strategy. |
| `docs/MIGRATION_v0.3_to_v0.4.md` | Step-by-step upgrade guide (one command: `casetrack migrate-qc`). |
| `CHANGELOG.md` | Release notes (most recent = v0.4.0). |
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

## Commands

20 subcommands total (16 from v0.3 + 4 new in v0.4):

| v0.3 | v0.4 |
|---|---|
| `init`, `append`, `status`, `validate`, `log`, `schema`, `rerun`, `dashboard`, `add-metadata`, `projects`, `query`, `export`, `migrate`, `register`, `doctor`, `recover` | `censor`, `uncensor`, `qc-history`, `migrate-qc`, `cohort` |

## Accepted design decisions (v0.4)

See §0 of proposal 0002 for the full list. Key ones:

1. **Hybrid storage** — `qc_events` audit + materialized `qc_status` cache.
2. **Three-level whole-entity QC only** — per-analysis censoring deferred to a future proposal.
3. **Consent distinct from QC** — separate `--include-consent-revoked` flag; ethics-override gate on reversal.
4. **SLURM auto-flag via summary TSV** — if `qc_pass` / `qc_fail_reason` / `qc_warn` columns appear, `append` consumes them and emits events inside the same transaction.
5. **Append-only reversal** — `uncensor` writes `resolved_at`, never deletes.
6. **Strict refuse on append to censored** — `--force-append-on-censored --yes` override.
7. **N-partition `--pair-by`** — same code path for tumor/normal, longitudinal, multi-region.

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

522 pytest tests across 27 files (~2 min full run). Run with `python3 -m pytest tests/ -q`.

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
│   ├── cohort.py             # cmd_cohort + pair-by N-partition
│   ├── migrate.py            # cmd_migrate_qc
│   ├── recover.py            # replay helpers for QC actions
│   └── cli.py                # argparse wiring helpers
├── setup.py                  # pip entry_points + packages=["casetrack_qc"]
├── CLAUDE.md                 # you are here
├── README.md                 # user-facing docs
├── CHANGELOG.md              # release notes
├── docs/
│   ├── CASETRACK_SYNOPSIS.md
│   ├── MIGRATION_v0.2_to_v0.3.md
│   ├── MIGRATION_v0.3_to_v0.4.md   # ← new
│   └── proposals/
│       ├── 0001-sqlite-normalized-backend.md
│       └── 0002-qc-events-and-censoring.md
├── examples/
│   ├── run_modkit.sh
│   ├── scripts/
│   ├── nextflow/
│   ├── claude/
│   └── giab_chr21/
├── scripts/
│   └── generate_demo_dashboard.py
├── tests/                    # 27 test files, 522 tests
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
