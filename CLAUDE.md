# CLAUDE.md — casetrack

## What this project is

Manifest-centric case management CLI for bioinformatics pipelines on HPC (SLURM). Tracks which analyses have been completed for which samples across a project. Built for cancer genomics studies with multi-specimen, multi-assay cohorts.

- **Repo**: https://github.com/sahuno/casetrack (private)
- **Author**: Samuel Ahuno (ekwame001@gmail.com / sahuno@mskcc.org)
- **Current release**: v0.2.0 (single-file `casetrack.py`, flat TSV manifest)
- **Next release**: v0.3.0 (SQLite backend, normalized patient/specimen/assay hierarchy)
- **HPC target**: IRIS @ MSKCC (SLURM, WekaFS shared storage, Apptainer containers)

## Key context files (read these to resume work)

| File | What it tells you |
|---|---|
| `docs/proposals/0001-sqlite-normalized-backend.md` | **The accepted v0.3 design.** Data model, file layout, CLI changes, concurrency strategy, migration plan. §0 has the seven locked-in decisions. |
| `gh issue view 4` | **Implementation tracking.** Phase-by-phase task checklist (α → β → release → v1.0). |
| `CHANGELOG.md` | What's already shipped in v0.2.0. |
| `casetrack.py` | All current CLI commands in one file (~750 lines). |
| `README.md` | User-facing docs for the 12 current commands. |

## How to run

```bash
# Install (user-level)
pip install -e ".[all]" --user

# Run tests (169 passing, ~15s)
python3 -m pytest tests/ -q

# Optional: install duckdb for `casetrack query`
pip install duckdb
```

## Architecture (v0.2 — current)

Single-file CLI (`casetrack.py`). One flat TSV manifest per project. Append-only columns, fill-only cells. POSIX flock for concurrent SLURM job safety. Provenance as JSONL sidecar. Git commit hash captured in every provenance entry.

12 subcommands: `init`, `append`, `add-metadata`, `status`, `validate`, `log`, `schema`, `rerun`, `dashboard`, `projects`, `query`, `export`.

## Architecture (v0.3 — in progress)

**Source of truth moves from TSV to SQLite.** Three normalized tables: `patients` → `specimens` → `assays` with enforced foreign keys. TSV becomes an on-demand export (`casetrack export`). Provenance stays as `provenance.jsonl`. DuckDB queries attach the SQLite file via `sqlite_scanner`.

New commands planned: `migrate`, `register`, `doctor`, `recover`, `schema {show,apply,dump,check}`.

## Accepted design decisions (v0.3)

1. **Three levels hardcoded** (patient/specimen/assay). N-level deferred to v0.4.
2. **Strict FK enforcement** — unknown parent → exit 2; `--allow-new-parent --yes` opt-in.
3. **Flat-manifest deprecated** in v0.3.x, removed in v1.0 (~6 months).
4. **Migration fail-closed** on ambiguous columns — must use `--metadata-map`.
5. **Concurrency Tier 1** — SQLite WAL + `busy_timeout=30000` + `casetrack doctor` (stress test) + `casetrack recover` (rebuild from provenance). WekaFS supports POSIX locks across nodes.
6. **`casetrack.db` gitignored** by default. Schema tracked in `casetrack.toml`, history in `provenance.jsonl`.
7. **Analysis-column types**: infer from summary TSV by default, accept `--col-type` overrides.

## Conventions

- **`_done` columns**: every analysis gets a `{analysis}_done` timestamp. This is how `status` and `rerun` know what's complete.
- **`--allow-new --yes` pair**: adding new sample/assay/specimen IDs requires double opt-in to prevent typos from silently expanding the manifest.
- **Summarize scripts**: each analysis has a small Python script that produces a per-sample TSV with `sample_id` (or `assay_id` in v0.3) as the first column.
- **Three-phase SLURM pattern**: (1) run tool, (2) summarize to TSV, (3) `casetrack append`.
- **Provenance is mandatory**: every mutation logs to JSONL with user, timestamp, SLURM job ID, git commit, file checksum.

## Test suite

169 tests across 12 files. Run with `python3 -m pytest tests/ -q`.

Tests cover: every subcommand, smart-merge correctness, 5000-sample perf regression, concurrent appends via multiprocessing, git provenance capture, Nextflow module shell contract (extract-and-execute), Claude Code hook (stubbed `claude` on PATH), and DuckDB query paths.

CI: GitHub Actions matrix over Python 3.10–3.13 on push + PR.

## Project layout

```
casetrack/
├── casetrack.py              # all CLI commands (single file)
├── setup.py                  # pip entry_points + extras (excel, parquet, query)
├── CLAUDE.md                 # you are here
├── README.md                 # user-facing docs
├── CHANGELOG.md              # release notes
├── docs/
│   ├── CASETRACK_SYNOPSIS.md        # original design intent
│   ├── manifest_case_management_architecture.svg
│   └── proposals/
│       └── 0001-sqlite-normalized-backend.md   # accepted v0.3 design
├── examples/
│   ├── run_modkit.sh                # example SLURM script
│   ├── scripts/                     # summarize_modkit.py, summarize_tldr.py
│   ├── nextflow/                    # DSL2 module + demo pipeline + config
│   └── claude/                      # post-analysis QC hook + prompt template
├── scripts/
│   └── generate_demo_dashboard.py   # builds a synthetic demo for Pages
├── tests/                           # 169 pytest tests
└── sandbox/                         # egg-info leftovers, delivery zip
```

## GitHub state

| Item | Link |
|---|---|
| Repo | https://github.com/sahuno/casetrack |
| Release | [v0.2.0](https://github.com/sahuno/casetrack/releases/tag/v0.2.0) |
| Pages | https://sahuno.github.io/casetrack/ (demo dashboard, public) |
| Tracking issue | [#4 — v0.3.0 implementation](https://github.com/sahuno/casetrack/issues/4) |
| CI | `.github/workflows/tests.yml` (Python 3.10–3.13) |
| Pages workflow | `.github/workflows/pages.yml` (regenerates demo dashboard on push) |

## What to tell a new session

> Read `docs/proposals/0001-sqlite-normalized-backend.md` and `gh issue view 4`. Start Phase α.
