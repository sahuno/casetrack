# Proposal 0001 — SQLite-backed Normalized Hierarchy

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | Draft |
| **Date** | 2026-04-15 |
| **Target release** | v0.3.0 |
| **Supersedes** | the implicit flat-TSV model of v0.1–v0.2 |
| **History** | An earlier draft (closed PR #2) kept TSV as the source of truth. Direct feedback ("TSV is good for human view, not for backend") pushed this design toward a real embedded database with TSV as an on-demand export. |
| **Related** | [PR #1: `casetrack query` (DuckDB)](https://github.com/sahuno/casetrack/pull/1) — the query engine used here |

## 1. Summary

This proposal reworks casetrack's storage layer around three ideas:

1. **Data model.** Replace the flat "one row = one sequencing library" manifest with three normalized entities: **patient → specimen → assay**. Each is a table with a primary key; children reference parents by foreign key.
2. **Source of truth.** Replace the TSV file(s) with a single embedded **SQLite** database per project. Types, primary keys, foreign keys, and indexes are enforced by the storage layer, not by hand-rolled Python.
3. **TSV as export only.** The user-facing "flat, git-diffable, Excel-openable" properties are preserved via `casetrack export` — invoked explicitly, not maintained in lockstep. The TSV stops being load-bearing for correctness.

Provenance stays as a separate `provenance.jsonl` (append-only immutable log). DuckDB-backed queries (PR #1) still work — DuckDB attaches the SQLite file directly, so no conversion cost.

## 2. Motivation

### 2.1 Why a normalized hierarchy

Casetrack today collapses three distinct biological entities into one row. Real cancer-genomics studies look like this:

```
Patient              — clinical subject; diagnosis, age, BRCA status, outcome
  └─ Specimen        — one tissue collection; L-ovary tumor, blood, R-ovary tumor
       └─ Assay      — one sequencing library; scRNA-Seq, ATAC-Seq, WGS
            └─ Analysis — tools run on that assay's output; modkit, tldr, xtea
```

An HGSOC cohort of 50 patients × ~3 specimens × ~3 assays in the flat model means:

- ~450 rows in one manifest
- ~20 clinical columns (age, BRCA, chemo, PFS, OS, cohort, …) **repeated ~9× per patient** → ~9,000 redundant cells
- "Update patient 042's outcome" means editing 9 rows and hoping nothing drifts
- "Which patients have all three assays on both tumor specimens?" becomes a string-parsing exercise on `sample_id`
- "Specimens collected but not yet assayed" is unrepresentable — no library means no row

### 2.2 Why a real database (and not more TSVs)

A naïve normalization — three TSVs with foreign keys — improves the data model but worsens the storage story. The initial draft of this proposal (closed PR #2) took that path. The reconsideration that led here:

- **FK enforcement in TSV** has to be hand-rolled in Python on every mutation. SQLite enforces it at the storage layer, for free, with clear errors at `INSERT` time.
- **Multi-table atomicity** (migrate, delete-cascade, cross-level metadata edits) in TSV needs a project-wide lock and a custom rollback path. SQLite has `BEGIN; …; COMMIT` and automatic rollback.
- **Type preservation** in TSV drifts (int → float when NaN arrives, bool → string round-trip). SQLite declares types at column creation and rejects bad writes.
- **Row-level updates** in TSV require rewriting the whole file. SQLite updates one row in place.
- **Indexing.** A cohort status lookup across 500 patients × 10 analyses is O(N·M) file scan in TSV, O(log N) in SQLite with an index on `specimen_id`.
- **Dependency surface.** `sqlite3` is Python **stdlib**. SQLite actually *reduces* our pip footprint compared to the current pandas-heavy approach (pandas becomes needed only for export and summarize scripts, not the core).

The properties TSV did buy us — `cat`, `grep`, `awk`, `git diff`, Excel — are **inspection ergonomics**, not workflow-critical. Preserve them via `casetrack export` instead of making them load-bearing.

## 3. Goals and non-goals

### Goals

1. Represent patient / specimen / assay as first-class tables with enforced primary and foreign keys.
2. Zero metadata duplication across levels.
3. Atomic multi-table mutations via SQLite transactions.
4. Typed columns that can't silently drift.
5. `casetrack query` via DuckDB's sqlite_scanner attaches the SQLite file directly (no import/export round-trip).
6. TSV available via `casetrack export` on demand: per-table, joined, or specific projection.
7. Status reportable at any level (analysis, assay, specimen, patient, cohort).
8. Deterministic migration tool from v0.2 flat manifests.
9. Preserve the append-on-job-completion workflow — existing SLURM three-phase scripts change only their final command.

### Non-goals

1. Not a server / daemon. SQLite is file-based; casetrack stays a local CLI.
2. Not mandating DuckDB. SQLite alone answers every question casetrack needs; DuckDB is the query ergonomics layer (optional install).
3. Not moving provenance into SQLite. Keeps JSONL append-only + cheap to tail.
4. Not auto-refreshing TSV exports on every mutation (per explicit decision — see §6 and §17).
5. Not supporting arbitrary N-level hierarchies in v0.3 (see §12 Q5).
6. Not changing the summarize-script contract (per-sample TSV with a key column first).

## 4. Data model

### 4.1 Entities (tables)

**`patients`** — clinical/biological subject.
- **Primary key**: `patient_id` (TEXT, unique, non-null)
- **Typical columns**: `age INTEGER`, `sex TEXT`, `diagnosis TEXT`, `brca_status TEXT` (enum), `neoadjuvant_chemo BOOLEAN`, `pfs_months REAL`, `os_months REAL`, `cohort TEXT`

**`specimens`** — one tissue collection event.
- **Primary key**: `specimen_id` (TEXT, unique, non-null)
- **Foreign key**: `patient_id` → `patients.patient_id` (ON DELETE RESTRICT)
- **Typical columns**: `tissue_site TEXT`, `timepoint TEXT`, `collection_date DATE`, `pathology_notes TEXT`, `tumor_purity REAL`, `storage_id TEXT`

**`assays`** — one sequencing library / experiment.
- **Primary key**: `assay_id` (TEXT, unique, non-null)
- **Foreign key**: `specimen_id` → `specimens.specimen_id` (ON DELETE RESTRICT)
- **Required**: `assay_type TEXT` (enum), `replicate INTEGER DEFAULT 1`
- **Optional**: `library_id TEXT`, `sequencing_date DATE`, `read_count INTEGER`, `qc_pass BOOLEAN`, and **all analysis result columns** (added dynamically by `casetrack append` via `ALTER TABLE ADD COLUMN`)

### 4.2 Constraints

All enforced by SQLite with `PRAGMA foreign_keys = ON`:

| Constraint | Mechanism |
|---|---|
| PK unique + non-null | `PRIMARY KEY (col)` + `NOT NULL` |
| FK references valid parent | `FOREIGN KEY (col) REFERENCES parent(pk)` |
| Cascade/restrict policy | `ON DELETE RESTRICT` (default); user can opt into cascade |
| Enum values (`assay_type`, `brca_status`) | `CHECK (col IN ('A', 'B', …))`, list sourced from `casetrack.toml` |
| Typed columns | column declarations with `INTEGER` / `REAL` / `TEXT` / `BOOLEAN` / `DATE` (stored as ISO string) |

### 4.3 Where analyses attach

Same as the previous draft — default is **assay** level; specimens and patients can host analysis columns too (pathology review, clinical outcome) by passing `--level` on `casetrack append`. The storage impact is that `ALTER TABLE` runs against whichever table the level points to.

## 5. File layout

```
project/
├── casetrack.db                     — authoritative embedded SQLite database
├── casetrack.toml                   — declared schema (source for init + drift check)
├── provenance.jsonl                 — append-only immutable event log (single file)
│
├── exports/                         — populated on demand by `casetrack export`
│   ├── patients.tsv                 — stale until `casetrack export` regenerates
│   ├── specimens.tsv
│   └── assays.tsv
│
├── results/                         — unchanged: raw per-assay tool output
├── scripts/                         — unchanged: summarize_*.py
├── logs/                            — unchanged: SLURM logs
└── containers/                      — unchanged: .sif files
```

Three top-level files matter to casetrack:

- **`casetrack.db`** — the truth. Binary SQLite, should be in `.gitignore` for most teams (see §6 on schema-as-code).
- **`casetrack.toml`** — declared schema intent. Git-tracked. If lost, can be regenerated from the DB via `casetrack schema dump`.
- **`provenance.jsonl`** — append-only, grep-friendly, human-readable. Git-tracked (or not — team decision).

The `exports/` directory is optional and entirely user-driven. If the team wants git-diff-able snapshots, `casetrack export --fmt tsv --output-dir exports/` after significant mutations produces them. No auto-sync.

## 6. `casetrack.toml` — declared schema

The TOML is the **human-facing source of schema truth**. SQLite holds the runtime structure. `casetrack init` reads the TOML and issues `CREATE TABLE` DDL; `casetrack schema check` validates that the live DB matches the declared schema.

```toml
[project]
name       = "msk_hgsoc_2026"
schema_v   = 1                          # bump on breaking schema changes
created    = "2026-04-15T10:00:00"

[levels.patient]
key = "patient_id"

[levels.patient.columns]
patient_id       = { type = "TEXT",    required = true, unique = true }
age              = { type = "INTEGER" }
sex              = { type = "TEXT",    enum = ["F", "M", "intersex", "unknown"] }
diagnosis        = { type = "TEXT" }
brca_status      = { type = "TEXT",    enum = ["brca1", "brca2", "wt", "vus"] }
neoadjuvant      = { type = "BOOLEAN" }
pfs_months       = { type = "REAL" }
os_months        = { type = "REAL" }

[levels.specimen]
key        = "specimen_id"
parent     = "patient"
parent_key = "patient_id"

[levels.specimen.columns]
specimen_id      = { type = "TEXT",    required = true, unique = true }
patient_id       = { type = "TEXT",    required = true }
tissue_site      = { type = "TEXT",    required = true }
timepoint        = { type = "TEXT" }
collection_date  = { type = "DATE" }
tumor_purity     = { type = "REAL" }

[levels.assay]
key        = "assay_id"
parent     = "specimen"
parent_key = "specimen_id"

[levels.assay.columns]
assay_id         = { type = "TEXT",    required = true, unique = true }
specimen_id      = { type = "TEXT",    required = true }
assay_type       = { type = "TEXT",    required = true, enum = ["scRNA", "ATAC", "WGS", "WES", "ONT", "Visium"] }
replicate        = { type = "INTEGER", default = 1 }
qc_pass          = { type = "BOOLEAN" }

# Analysis-produced columns are NOT declared here — they're added dynamically
# by `casetrack append` via ALTER TABLE and tracked in the provenance log.

[analysis_defaults]
default_level = "assay"

[engine]
# Concurrency knobs (see §9)
wal              = true
busy_timeout_ms  = 30000
```

Two kinds of columns:

1. **Declared columns** — written into the TOML, enforced by SQLite at `CREATE TABLE`. Change them by editing TOML and running `casetrack schema apply` (which runs `ALTER TABLE` and bumps `schema_v`).
2. **Analysis columns** — added dynamically by `casetrack append`. The column types are inferred from the summary TSV; the column name is the analysis result. These are stored in `assays` (by default) and tracked in `provenance.jsonl` + an `analysis_columns` metadata view.

## 7. CLI changes

### 7.1 Modified commands

| Command | v0.2 (flat TSV) | v0.3 (SQLite) |
|---|---|---|
| `init` | `init --manifest X.tsv --samples Y.txt` | `init --project-dir DIR [--from-template hgsoc]` — creates `casetrack.db`, `casetrack.toml`, empty `provenance.jsonl` |
| `append` | `append --manifest X.tsv --results R --analysis A` | `append --project-dir DIR [--level assay] --results R --analysis A` — issues `INSERT … ON CONFLICT UPDATE` inside a transaction |
| `add-metadata` | `add-metadata --manifest X.tsv --metadata M` | `add-metadata --project-dir DIR --level LEVEL --metadata M` |
| `status` | `status --manifest X.tsv` | `status --project-dir DIR [--group-by {analysis,assay,specimen,patient}]` — single SQL query |
| `validate` | single file checks | PK/FK enforced by engine; `validate` now checks TOML↔DB drift + logical invariants |
| `log` | reads one JSONL | unchanged — still reads `provenance.jsonl` |
| `schema` | shows JSON sidecar | `schema show` dumps current DDL; `schema apply` applies TOML changes as `ALTER`; `schema dump` regenerates TOML from DB |
| `dashboard` | flat HTML | nested HTML — cohort → patient → specimen → assay |
| `rerun` | reads manifest | reads `assays` table; same sbatch-command output |
| `export` | TSV/CSV/XLSX/Parquet | same formats + new options: `--shape tables` (3 TSVs), `--shape joined` (denormalized), `--tables patients,specimens` (subset), `--sql "…"` (arbitrary query) |
| `projects` | scans for `manifest.tsv` | scans for `casetrack.toml` |
| `query` | single manifest as `_` | `_` = `patients ⋈ specimens ⋈ assays`; named tables `patients`, `specimens`, `assays` — via DuckDB `ATTACH 'casetrack.db' (TYPE sqlite)` |

### 7.2 New commands

- **`casetrack migrate --flat X.tsv --patient-col … --specimen-col … --assay-col … --out-dir NEW/`** — convert a v0.2 flat manifest into a v0.3 project. Routing heuristic unchanged from the earlier draft.
- **`casetrack register --project-dir DIR --level LEVEL --id ID --parent PARENT_ID --meta k=v,k=v`** — add a single row at any level, useful for "specimen collected, register before any assay exists".
- **`casetrack schema {show,apply,dump,check}`** — manage the TOML↔DB schema lifecycle.

### 7.3 Deprecated

- `--manifest PATH` → still accepted in v0.3.x with a loud deprecation warning; the CLI synthesizes a temporary SQLite from the flat TSV. Removed in v1.0.

## 8. Provenance model

`provenance.jsonl` stays exactly as it is today: one immutable append-only file per project, one JSON object per line.

Additions to the schema:

- `level` — which logical table was mutated (`patient`, `specimen`, `assay`)
- `sql` — the DDL or DML statement(s) executed (so a reviewer can reproduce exactly what ran)
- `rows_affected` — from `cursor.rowcount`
- `schema_v_before` / `schema_v_after` — tracks schema evolution
- `transaction_id` — groups multi-statement atomic operations

Example entry:

```json
{
  "action": "append",
  "level": "assay",
  "analysis": "modkit_methylation",
  "transaction_id": "txn_20260415T174235_a1b2c3",
  "sql": [
    "ALTER TABLE assays ADD COLUMN modkit_mean_meth REAL",
    "ALTER TABLE assays ADD COLUMN modkit_done TEXT",
    "UPDATE assays SET modkit_mean_meth = 0.72, modkit_done = '2026-04-15' WHERE assay_id = 'P001-LOV-WGS-1'"
  ],
  "rows_affected": 1,
  "columns_added": ["modkit_mean_meth", "modkit_done"],
  "results_file": "modkit_summary.tsv",
  "results_checksum": "f4e1b7c9…",
  "schema_v_before": 4,
  "schema_v_after": 5,
  "timestamp": "2026-04-15T17:42:35",
  "user": "sahuno",
  "slurm_job_id": "12345",
  "git": {"commit": "...", "branch": "main", "dirty": false, "toplevel": "..."}
}
```

**Why JSONL and not a SQLite table**: provenance should survive DB corruption. Keeping it outside the DB gives us a rebuild path — we can reconstruct the DB by replaying the log. Also, `tail -f provenance.jsonl` is ergonomically nice during a pipeline run.

## 9. Concurrency

### 9.1 Write model

- SQLite in **WAL mode** (`PRAGMA journal_mode = WAL`) — readers don't block writers.
- **`PRAGMA busy_timeout = 30000`** — writer retries for up to 30s if another writer holds the lock.
- Every mutation runs inside a `BEGIN IMMEDIATE; … ; COMMIT;` block so the provenance write and the data write atomically succeed or fail together.
- `provenance.jsonl` is append-only, written **after** the SQLite commit (so if the DB commit fails, we don't log a ghost event).

### 9.2 HPC shared-filesystem reality

SQLite + WAL on NFS/Lustre/GPFS is a known cautious case. POSIX advisory locks aren't universally reliable across nodes. For casetrack's target scale (24–200 samples, dozens of concurrent SLURM array tasks), empirical reports say this works fine in practice, but corruption is possible under adversarial concurrency.

**Mitigation tiers** — ship with tier 1, escalate if real pipelines hit issues:

1. **Tier 1 (v0.3.0)**: WAL + `busy_timeout=30000` + documented caveat. Works for the common case. Failure mode is a 30s hang then a clear error; bad job can retry.
2. **Tier 2 (follow-up if needed)**: per-task JSONL deltas flushed to SQLite by a merger step at end of job. Eliminates cross-node concurrent writes entirely. Adds a sync-lag property.
3. **Tier 3 (research-scale only)**: broker process holding the DB, jobs communicate via a socket/pipe.

### 9.3 Read model

`casetrack status`, `casetrack query`, `casetrack dashboard` are read-only, so WAL lets them run concurrently with any number of writers. No locking needed.

## 10. Query model — DuckDB via `sqlite_scanner`

PR #1's `casetrack query` subcommand stays almost unchanged. The only adjustment: the `_` view now targets the SQLite-backed tables through DuckDB's `sqlite_scanner` extension:

```sql
-- Performed once per query invocation
INSTALL sqlite;  LOAD sqlite;
ATTACH 'casetrack.db' AS ct (TYPE sqlite);

CREATE VIEW _ AS
  SELECT p.*,
         s.* EXCEPT (patient_id),
         a.* EXCEPT (specimen_id)
  FROM      ct.patients  p
  LEFT JOIN ct.specimens s USING (patient_id)
  LEFT JOIN ct.assays    a USING (specimen_id);
```

Users write the same SQL they'd write against a TSV union; DuckDB pushes down predicates and JOINs into the SQLite tables via zero-copy scanning. Cross-project queries (`casetrack query --root ~/projects/`) attach each project's DB in turn and `UNION ALL BY NAME`.

Benefits over PR #1's pure-TSV path:

- No CSV parsing cost per query — SQLite tables are already structured.
- FK-consistent joins, guaranteed by the storage layer.
- Indexes on PK/FK make `WHERE patient_id = '…'` instant.

## 11. Dashboard UX

Same structure as the earlier draft — cohort summary, per-level progress bars, specimens × analyses heatmap grouped by patient, per-patient `<details>` drill-down, merged provenance timeline.

Implementation gets simpler: a single SQLite query produces the data for each section. No need to load three TSVs and join them in Python. `_render_dashboard_html` takes a connection, runs ~5 `SELECT` statements, renders.

## 12. Open questions

| # | Question | Proposed answer |
|---|---|---|
| Q1 | FK strictness on append (reject unknown parents) | **Reject by default**, `--allow-new-parent --yes` to create inline |
| Q2 | Assay-type enum enforcement | **Strict** via SQLite `CHECK`; `--allow-new-assay-type` extends the enum in TOML |
| Q3 | Technical replicates | Two rows with distinct `assay_id` + `replicate` integer |
| Q4 | Analyses spanning multiple assays (CN-consensus across WGS replicates) | Attach at **specimen** level via `--level specimen` |
| Q5 | Three levels vs N levels | Hardcode 3 for v0.3; generalize to N in v0.4 if a real project needs it |
| Q6 | Where does analysis-column **type** come from when `casetrack append` runs? | Inferred from the summary TSV (pandas dtype → SQLite affinity); override via `--col-type modkit_mean_meth:REAL` |
| Q7 | HPC shared-FS concurrency | Tier 1 (WAL + busy_timeout + documented caveat) for v0.3; escalate if needed |
| Q8 | Git-track `casetrack.db`? | **No** by default (binary, huge diffs). Track `casetrack.toml` + `provenance.jsonl`. DB reproducible from those + `casetrack migrate`. |
| Q9 | Flat-manifest deprecation horizon | Supported through v0.3.x with a deprecation warning; removed in v1.0 (~6 months) |
| Q10 | Export cadence | **On demand via `casetrack export` only.** No auto-refresh on mutation. |

## 13. Migration strategy

### 13.1 Per-project (for an existing v0.2 flat manifest)

```bash
casetrack migrate \
    --flat old_manifest.tsv \
    --patient-col patient_id \
    --specimen-col specimen_id \
    --assay-col assay_id \
    --metadata-map "patient:age,brca_status;specimen:tissue_site,timepoint" \
    --out-dir migrated_project/
```

Does:

1. Parse the flat TSV with pandas.
2. For each non-key column, determine its level via the routing heuristic (constant-within-group) or `--metadata-map`.
3. Write `casetrack.toml` declaring patients / specimens / assays with the discovered columns.
4. Run `casetrack init --project-dir migrated_project/` against that TOML.
5. `INSERT` all rows into the right tables inside a single transaction. FK violations during migration stop the run with a clear error.
6. Write `.migration_report.tsv` (per-column placement) and `.migration_report.md` (human-readable summary).
7. Copy the source flat TSV to `migrated_project/sandbox/source_manifest.tsv` as an audit artifact — source is never deleted.

### 13.2 Codebase rollout

| Phase | Version | Scope |
|---|---|---|
| α | v0.3.0-alpha | TOML parser; `init --project-dir`; `migrate`; SQLite wiring. Flat mode still the default. |
| β | v0.3.0-beta | Multi-level `append` / `add-metadata` / `validate` / `status` / `log`. Deprecation warning on flat load. |
| release | v0.3.0 | `dashboard`, `query` (via sqlite_scanner), `rerun`, `export`, `schema`, `register` all multi-level. Nextflow + Claude hooks updated. Migration guide published. |
| major | v1.0.0 | Flat manifest support removed. |

### 13.3 Integration consumer updates

- `examples/nextflow/casetrack.nf` — swap `casetrack_manifest` for `casetrack_project_dir`; default `level=assay`.
- `examples/claude/post_analysis_hook.sh` — swap `MANIFEST` env for `PROJECT_DIR`; invocation becomes `casetrack append --project-dir … --level assay …`.
- `examples/run_modkit.sh` — update the phase-3 line.
- `scripts/generate_demo_dashboard.py` — rebuild atop the new init/append path.

## 14. Test plan

Net-new test files (target ≥220 total, keeping current 169 green via compatibility layer):

| File | Coverage |
|---|---|
| `tests/test_toml_schema.py` | parse, validate, enum handling, error messages |
| `tests/test_sqlite_init.py` | `init --project-dir` produces a DB matching the TOML |
| `tests/test_sqlite_append.py` | append at each level; `ALTER TABLE` for new analysis columns; transactional semantics |
| `tests/test_foreign_keys.py` | FK violations blocked by SQLite `CHECK`/`FOREIGN KEY` |
| `tests/test_enum_check.py` | `CHECK (assay_type IN …)` enforcement; `--allow-new-assay-type` opt-out |
| `tests/test_status_grouping.py` | `--group-by {analysis,assay,specimen,patient}` correctness |
| `tests/test_migrate.py` | flat → SQLite conversion; routing; audit report; round-trip equivalence |
| `tests/test_register.py` | single-row register at each level |
| `tests/test_provenance_transactions.py` | provenance entries group by `transaction_id`; schema-version bumps recorded |
| `tests/test_query_via_sqlite.py` | DuckDB `ATTACH` + sqlite_scanner; JOIN correctness; cross-project UNION |
| `tests/test_dashboard_nested.py` | nested HTML render from SQLite |
| `tests/test_concurrency_wal.py` | concurrent appends on a local filesystem (multiprocess) under WAL; confirm no corruption |
| `tests/test_compat_flat.py` | v0.2 flat manifest still loads via the shim; deprecation warning |
| `tests/test_export_shapes.py` | `--shape tables`, `--shape joined`, `--sql` projections |

## 15. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| SQLite corruption on shared filesystem at SLURM-array scale | Medium | Tier 1 concurrency (WAL + busy_timeout) for v0.3; document caveat prominently; add Tier 2 (per-task JSONL deltas) if real deployments hit it |
| Binary `casetrack.db` loses git-diff visibility of schema changes | Medium | `casetrack.toml` is the git-tracked source; `provenance.jsonl` captures every `ALTER TABLE`; `casetrack schema diff` shows TOML→DB drift |
| Migration heuristic mis-routes a metadata column | Medium | Migration report surfaces every decision; `--metadata-map` overrides; source TSV preserved in sandbox |
| Users confused by "level" as a CLI concept | Medium | Help text, examples, one-page concept diagram in README; `--group-by` defaults chosen to match current intuition |
| Loss of `cat manifest.tsv` / `grep` ergonomics | Medium | `casetrack export` + `casetrack query` cover the real use cases; add a thin `casetrack show --table assays --head 10` for casual inspection |
| SQLite import path varies under conda vs system Python on HPC | Low | stdlib, so generally fine; smoke-test during init |
| DuckDB sqlite_scanner unavailable on air-gapped HPC | Low | sqlite_scanner auto-downloads by default; `--offline` mode skips query subcommand cleanly |

## 16. Rollout

- **v0.3.0-alpha** (~1 week): TOML parser, `init --project-dir`, `migrate`, SQLite wiring. Existing flat mode still default.
- **v0.3.0-beta** (~1 week): multi-level `append` / `add-metadata` / `validate` / `status` / `log`. Deprecation banner on flat.
- **v0.3.0** (~1 week): dashboard + query + rerun + export + schema commands all multi-level. Nextflow + Claude hooks updated. Migration docs.
- **v0.3.x**: bug fixes only.
- **v1.0.0** (~3–6 months): flat manifest support removed.

Total: **~3 weeks** of focused engineering for the backend rework, plus 1 week for integration updates.

## 17. Appendix A — example end-to-end workflow

### Day 0: project kickoff

```bash
casetrack init --project-dir msk_hgsoc_2026 --from-template hgsoc
# Writes casetrack.toml, creates casetrack.db with the three tables declared,
# seeds an empty provenance.jsonl.

casetrack add-metadata --project-dir msk_hgsoc_2026 \
    --level patient --metadata cohort_enrollment.tsv --yes
# Bulk-inserts 50 patients from a clinical CSV (one INSERT per row, all in
# one transaction).
```

### Day 14: first specimen arrives

```bash
casetrack register --project-dir msk_hgsoc_2026 --level specimen \
    --id P001-LOV --parent P001 \
    --meta tissue_site=left_ovary,timepoint=pre-op
# INSERT INTO specimens (specimen_id, patient_id, tissue_site, timepoint)
#   VALUES ('P001-LOV', 'P001', 'left_ovary', 'pre-op')
```

### Day 21: first assay completes

```bash
# Phase 1–2 unchanged; summarize_modkit.py emits modkit_summary.tsv.
# Phase 3:
casetrack append --project-dir msk_hgsoc_2026 --level assay \
    --results modkit_summary.tsv --analysis modkit_methylation
# Inside a transaction:
#   ALTER TABLE assays ADD COLUMN modkit_mean_meth REAL;
#   ALTER TABLE assays ADD COLUMN modkit_done TEXT;
#   UPDATE assays SET … WHERE assay_id = 'P001-LOV-WGS-1';
```

### Day 60: cohort status

```bash
casetrack status --project-dir msk_hgsoc_2026 --group-by patient
# Single SQL: GROUP BY patient with nested counts per assay type.
```

### Day 90: publication-ready extract

```bash
casetrack query --project-dir msk_hgsoc_2026 --fmt tsv --output table_s1.tsv "
  SELECT p.patient_id, p.brca_status, p.os_months,
         COUNT(DISTINCT s.specimen_id) AS n_specimens,
         COUNT(DISTINCT a.assay_type) AS n_assay_types
  FROM      patients  p
  LEFT JOIN specimens s USING (patient_id)
  LEFT JOIN assays    a USING (specimen_id)
  GROUP BY  p.patient_id, p.brca_status, p.os_months
  ORDER BY  p.os_months"
```

### Ad-hoc: export everything for a collaborator

```bash
casetrack export --project-dir msk_hgsoc_2026 --shape tables --output-dir share/
# Writes share/patients.tsv, share/specimens.tsv, share/assays.tsv.
# These are plain TSVs; opens in Excel, greppable, sendable via email.
```

## 18. Appendix B — integration impact

### `examples/nextflow/casetrack.nf`

```groovy
process casetrack_append {
    input:
      tuple val(analysis), path(results_tsv)

    script:
    """
    ${params.casetrack_bin} append \\
        --project-dir '${params.casetrack_project_dir}' \\
        --level '${params.casetrack_level ?: "assay"}' \\
        --results '${results_tsv}' \\
        --analysis '${analysis}'
    """
}
```

### `examples/claude/post_analysis_hook.sh`

Env rename: `MANIFEST` → `PROJECT_DIR`. Invocation line:

```bash
"$CASETRACK_BIN" append \
    --project-dir "$PROJECT_DIR" \
    --level assay \
    --results "$review_tsv" \
    --analysis "cc_${ANALYSIS}_review"
```

Prompt template unchanged.

### `examples/run_modkit.sh`

Phase 3 becomes:

```bash
casetrack append \
    --project-dir "$PROJECT_DIR" \
    --level assay \
    --results summary.tsv \
    --analysis modkit_methylation
```

### `scripts/generate_demo_dashboard.py`

Rewritten to:

1. `init --project-dir tmpdir` (creates `casetrack.db` + TOML).
2. Seed 24 patients / 24 specimens / 24 assays via `register` or bulk insert.
3. `append --level assay` six times for the six stub analyses.
4. `dashboard --project-dir tmpdir --output …`.

Same deterministic seed, same synthetic cohort shape as today.

## 19. Decisions requested

Four things to sign off before implementation (same four as the previous draft; answers carry over):

1. **Hierarchy depth (Q5)**: hardcode 3 levels in v0.3, or generalize to N levels day-one?
2. **FK enforcement posture (Q1)**: strict-by-default + opt-in, or warn-and-continue?
3. **Flat-manifest deprecation horizon (Q9)**: remove in v1.0 (~6 months) acceptable?
4. **Migration heuristic tolerance**: fail-closed on ambiguous columns, or route-to-assay + warn?

Plus three new decisions specific to the SQLite backend:

5. **Concurrency tier (Q7)**: ship Tier 1 (WAL + documented caveat) only, or pre-invest in Tier 2 (per-task JSONL deltas) before v0.3.0?
6. **Git-track `casetrack.db` (Q8)**: `.gitignore` by default (recommended), or expose as a per-team choice in the template?
7. **Analysis-column type source (Q6)**: pure-infer-from-TSV (simplest), or require an optional `--col-type name:TYPE` flag for each column in the summarize step?

Sign off and I'll open the implementation tracking issue with phase-by-phase tasks matching §16.
