# Casetrack — design synopsis

## What this document is

The **design-intent** companion to [`README.md`](../README.md) (user-facing)
and [`docs/MIGRATION_v0.2_to_v0.3.md`](MIGRATION_v0.2_to_v0.3.md) (upgrade
path). This document answers *why* casetrack looks the way it does — the
decisions behind the surface, and the tradeoffs that got us there. It
reflects the shipped **v0.3** architecture. The v0.2 flat-TSV model is
preserved as a deprecated compatibility layer and described in the
migration doc; it is not the architecture going forward.

For a worked end-to-end example on real data, see
[`examples/giab_chr21/`](../examples/giab_chr21/README.md).

## Problem statement

A computational biologist runs many analyses per project on HPC (IRIS at
MSKCC, SLURM) against **cohort-scale** data — multiple patients, each with
one or more specimens, each sequenced on one or more flowcells. Results
end up scattered across directories; "is modkit done on every tumor
specimen of every brca1 patient?" is a string-parsing exercise on
filenames.

Casetrack is the lightweight, local-first answer to that question. It
doesn't try to be a Terra or a LIMS. It's one CLI that every pipeline,
script, or agent calls to register its results into a per-project SQLite
database. That database is the source of truth; everything else —
dashboards, exports, cross-cohort rollups — is a read of it.

Inspiration: the Broad's Firehose/FireCloud/Terra lineage — but sized for
a single lab, versioned in git, and deployable without a control plane.

## The v0.3 data model

Three hardcoded entities, one tier each of a hierarchy:

```
patient   ← clinical/biological subject        (age, sex, BRCA status, outcome)
  └─ specimen   ← one tissue collection        (site, timepoint, tumor purity)
       └─ assay    ← one sequencing library    (WGS / ATAC / ONT / …)
            └─ analysis-produced columns       (modkit_mean_meth, n_snvs, …)
```

Each entity is a SQLite table with a primary key (`patient_id`,
`specimen_id`, `assay_id`). Children reference parents by foreign key,
enforced by `PRAGMA foreign_keys = ON`. Analysis results live as columns
on the appropriate level (almost always `assay`); `ALTER TABLE ADD
COLUMN` adds them dynamically on first append.

### Three file artifacts per project

```
cohort/
├── casetrack.toml        — declared schema, human-facing, git-tracked
├── casetrack.db          — SQLite, WAL journal, source of truth (not git-tracked)
├── provenance.jsonl      — append-only audit log, git-trackable
└── .gitignore            — excludes casetrack.db{,-wal,-shm}, exports/
```

`casetrack.toml` is the **contract**. `casetrack.db` is the **runtime**.
`provenance.jsonl` is the **replay log**. Any two of the three can
reconstruct the missing one (the `recover` command replays the log).

### The three-phase SLURM pattern

Every analysis follows the same shape:

```bash
# 1. Run tool
apptainer exec container.sif tool input output

# 2. Summarize to per-assay TSV
#    Contract: first column is the level's key (assay_id / specimen_id / patient_id)
python3 summarize_tool.py --assay-id "$ASSAY_ID" --input output --output summary.tsv

# 3. Register with casetrack
casetrack append --project-dir "$PROJECT_DIR" \
    --analysis tool_name --results summary.tsv
```

The pattern isolates the tool call (phase 1) from the summarization logic
(phase 2) from the registration (phase 3). Each is independently
testable and restartable.

## Core design decisions

### 1. Why SQLite, not TSV

v0.2 shipped on a flat TSV. It worked for single-level cohorts, ran into
three walls at scale:

- **Row-level updates** require rewriting the whole file. At 500
  assays × 40 columns, file I/O starts dominating the three-phase latency.
- **FK enforcement in TSV** is hand-rolled Python on every mutation.
  SQLite does it at storage layer with clear errors at `INSERT` time.
- **Multi-table atomicity** (migration, cross-level metadata edits) in
  TSV requires a project-wide lock and a custom rollback path. SQLite
  has `BEGIN IMMEDIATE; … ; COMMIT;` and automatic rollback.

Why not Postgres? Requires a server, a deployment story, and network
access — none of which a local-first tool can assume on a shared HPC
filesystem. SQLite is stdlib; Postgres isn't.

Why not parquet/HDF5? Both optimize for columnar analytical reads at the
expense of row-level updates. Casetrack's workload is dominated by
row-level writes (one per finished SLURM job).

### 2. Why hardcode three levels (patient → specimen → assay)

Real cancer-genomics cohorts are naturally three-tiered. A generic
N-level tree complicates the schema, the query model (how many CTEs to
walk up?), and the CLI (what level is this `--level` on this command?).

Deferred to v0.4 if a real project genuinely needs a fourth level
(replicate? region? timepoint as an entity rather than a column?). Three
levels shipped with a clean FK story is better than five levels shipped
with bugs.

### 3. Why `casetrack.toml` as the schema source, not a Python class

Schema decisions are decisions the *domain expert* makes, not the
engineer. TOML is readable at arm's length, reviewable in a PR, and
validated at parse time. Python classes would bury schema choices in
code and force a redeploy to change them.

`schema_v` lives in the TOML and bumps on every `schema apply` — this
doubles as a cheap version stamp in provenance entries.

### 4. Why provenance outside the DB (append-only JSONL)

`provenance.jsonl` is written **after** the SQLite commit succeeds. That
sequencing means:

- A crashed commit leaves no ghost provenance entry
- A corrupted DB can be rebuilt from the log (`casetrack recover`)
- `tail -f provenance.jsonl` works during a long SLURM run for live audit
- The log survives DB format changes

The alternative — provenance as a SQLite table — couples the audit trail
to the thing it audits. If the DB dies you lose both.

### 5. Why Tier-1 concurrency (WAL + busy_timeout), not a broker

Realistic workloads are dozens of SLURM jobs appending a few seconds each.
Tier 1 is:

- `PRAGMA journal_mode = WAL` — readers don't block writers
- `PRAGMA busy_timeout = 30000` — writers retry up to 30 s if locked
- `BEGIN IMMEDIATE` up front — deterministic lock ordering
- `casetrack doctor` — stress-test the filesystem's POSIX lock semantics
  at project kickoff

Target filesystem is **WekaFS**, which implements cross-node POSIX locks
correctly. Tier 2 (per-task JSONL + merger) and Tier 3 (broker daemon)
are defined in proposal 0001 §9 but only escalated to if `doctor` fails.

### 6. Why DuckDB for queries, not SQLite's own query planner

DuckDB has:

- Columnar query execution (faster on analytical reads)
- A rich SQL dialect (window functions, PIVOT, list/struct types)
- The `sqlite_scanner` extension that attaches a SQLite DB read-only
- No server, no daemon — a Python import

`casetrack query --project-dir D "SQL"` ATTACHes `casetrack.db` READ_ONLY
so a running query can never corrupt a live writer's WAL. Helper views
are published:

- `patients`, `specimens`, `assays` — pass-throughs
- `_` = `assays ⋈ specimens ⋈ patients` — cohort view, one row per assay
  with all ancestor metadata inlined

### 7. Why fill-only merge by default

Multiple SLURM array tasks commonly finish simultaneously and call
`append` on the same table. Fill-only via `COALESCE(col, ?)` lets them
converge deterministically — the first writer wins for every cell. Users
who want to overwrite an earlier (perhaps bad) result pass `--overwrite`
explicitly.

### 8. Why strict FK enforcement with opt-in escape hatches

"I mistyped a patient_id and now there's a phantom specimen" is the most
common data-integrity failure mode. SQLite catches it at `INSERT` with
`ON DELETE RESTRICT` foreign keys. The escape hatch is a deliberate
double opt-in (`--allow-new-parent --yes`) for operators who genuinely
want to bootstrap new parents inline. At the assay level, inline parent
creation is refused entirely: creating a specimen stub without a
patient_id is unresolvable.

### 9. Why `casetrack migrate` rather than a background converter

Flat-to-v0.3 migration requires routing every column into the right
level — a decision that benefits from human review. `migrate` runs the
heuristic (coarsest level at which a column is constant-within-group),
emits `.migration_report.{tsv,md}`, preserves the source TSV under
`sandbox/`, and accepts `--metadata-map` overrides. It is a one-shot
per project, not a sync.

### 10. Why a three-layer usage model

casetrack the **package** ≠ casetrack **projects** ≠ user **pipeline
code**. See [`README.md`](../README.md) → "How people actually use this"
for the full table. The short version: you install casetrack once, you
create many projects (one per cohort), your analysis code lives in your
own git repo and calls the CLI. You do not clone this repo per project.

## Dependencies

Runtime:

- Python ≥ 3.10
- `pandas ≥ 1.5.0`, `duckdb ≥ 0.9`, `tomli` (backport on 3.10; tomllib is
  stdlib on 3.11+)
- Optional: `openpyxl` (xlsx export), `pyarrow` (parquet export)

No daemon, no network, no external database. `sqlite3` is stdlib.

## Integration patterns

**SLURM (the common case)**: ship `run_<analysis>.sh` scripts that
follow the three-phase pattern. `examples/giab_chr21/slurm/` is the
reference — verified against real ONT BAMs in the GIAB chr21 cohort.

**Nextflow**: `examples/nextflow/casetrack.nf` ships
`casetrack_append_project`, `casetrack_register_project`, and
`casetrack_add_metadata_project` processes. `params.casetrack_project_dir`
is the only config knob most workflows need.

**Claude Code**: `examples/claude/post_analysis_hook.sh` ships a hook
that runs a QC review after every completed analysis and appends the
review as its own analysis (`cc_<analysis>_review`) — a concrete
application of "the manifest is the agent's scratchpad".

## What's not in scope for v0.3

Explicitly out:

- **N-level hierarchy** — patient/specimen/assay stays hardcoded.
  Deferred to v0.4 if real projects need it (replicate? region?
  longitudinal timepoints as entities?).
- **Cross-project joins at the DB layer** — `casetrack query --root`
  unions TSV exports, but there is no cohort-of-cohorts SQLite. Users
  who need that run their own DuckDB scripts.
- **Real-time sync between a project and a central registry** — casetrack
  is local-first by design.
- **GUI** — everything is CLI + static HTML. The dashboard is a
  `casetrack dashboard` rendering of the current DB state; there is no
  live server.

## Repository layout

```
casetrack/
├── casetrack.py          # single-file CLI (all subcommands)
├── setup.py              # pip-installable, entry_points=[casetrack=...]
├── README.md             # user-facing docs
├── CHANGELOG.md          # release history
├── CLAUDE.md             # project instructions for Claude Code agents
├── docs/
│   ├── CASETRACK_SYNOPSIS.md       — you are here
│   ├── MIGRATION_v0.2_to_v0.3.md   — upgrade guide
│   └── proposals/
│       └── 0001-sqlite-normalized-backend.md   — accepted design
├── examples/
│   ├── giab_chr21/       — real-data demo (ONT, Genome-in-a-Bottle)
│   ├── nextflow/         — casetrack.nf DSL2 module
│   └── claude/           — post-analysis hook
├── scripts/              — one-shot tooling (legacy demo renderer)
└── tests/                — pytest suite (410 tests as of v0.3.1)
```

## References

- **Proposal 0001** — the accepted v0.3 design:
  [`docs/proposals/0001-sqlite-normalized-backend.md`](proposals/0001-sqlite-normalized-backend.md).
  Canonical for all design-decision rationale. When in doubt about *why*
  something works the way it does, check §0 (the seven accepted
  decisions) and §19 (the Q&A that produced them).
- **Migration guide** —
  [`docs/MIGRATION_v0.2_to_v0.3.md`](MIGRATION_v0.2_to_v0.3.md) covers
  the v0.2 → v0.3 path in detail.
- **README** — [`README.md`](../README.md) has the user-facing quick
  start, the command table, and the three-layer usage model.
- **CHANGELOG** — [`CHANGELOG.md`](../CHANGELOG.md) tracks what shipped
  when; entries are written for humans scanning for a particular change.
