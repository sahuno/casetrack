# Proposal 0001 — Normalized Hierarchy (Patient → Specimen → Assay)

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | Draft |
| **Date** | 2026-04-15 |
| **Target release** | v0.3.0 |
| **Supersedes** | the implicit flat-sample model of v0.1–v0.2 |
| **Related** | [PR #1: `casetrack query` (DuckDB)](https://github.com/sahuno/casetrack/pull/1) — unblocks the joined-view query model used here |

## 1. Summary

Casetrack today collapses three distinct biological entities — **patient**, **specimen**, and **assay** — into a single TSV row. This works for simple projects (one library per patient) and breaks down for realistic cancer-genomics studies where one patient provides multiple tissue specimens and each specimen is profiled with several sequencing assays.

This proposal splits the flat manifest into three normalized TSVs (`patients.tsv`, `specimens.tsv`, `assays.tsv`) connected by foreign keys, declared in a new `casetrack.toml` project config. Analyses continue to append columns, but at the level appropriate to what they measure. A new `casetrack migrate` tool converts existing flat manifests. DuckDB-backed queries (PR #1) provide `JOIN`s across levels, so no user-facing ergonomics are lost — and several are gained.

## 2. Motivation

### 2.1. Pain points in the flat model

The current single-TSV design forces every row to answer one question: *"What's the status of this one sequencing library?"*. Real study designs ask other questions:

| Question | Flat model | Normalized model |
|---|---|---|
| *"Update patient 042's outcome"* | Edit N rows (one per assay), hope nothing drifts | Edit one row in `patients.tsv` |
| *"Which patients have all three assays done on both tumor specimens?"* | Parse `sample_id` strings, `GROUP BY` twice | `SELECT ... HAVING COUNT(DISTINCT assay_type) = 3` |
| *"Track specimens collected but not yet sequenced"* | Not representable — no library = no row | A row in `specimens.tsv` with no children in `assays.tsv` |
| *"Specimen-level QC (pathology review) vs assay-level QC (library complexity)"* | One column, conflated meaning | Separate columns on separate files |
| *"Patient 042 drill-down"* | Filter rows where `sample_id LIKE 'P042_%'` | `WHERE patient_id = 'P042'` |

### 2.2. A concrete example

HGSOC cohort: 50 patients, each with tumor (left ovary + right ovary + omentum if present) and matched normal (whole blood). Three assays per tumor (scRNA, ATAC, WGS), two on normal (WGS + germline panel). Patient-level clinical metadata: age, BRCA1/2 status, neoadjuvant chemo, PFS, OS.

Flat manifest:
- ~450 rows (50 patients × ~3 specimens × ~3 assays)
- ~20 clinical columns repeated ~9× per patient → ~9,000 redundant cells
- "`BRCA1 status` for patient 042" lives in 9 cells that can drift out of sync
- "specimens where WGS was collected but scRNA failed" requires string parsing `sample_id`

Normalized:
- `patients.tsv`: 50 rows, 20 columns → 1,000 cells, zero duplication
- `specimens.tsv`: ~150 rows, 5 columns → per-specimen metadata (site, timepoint)
- `assays.tsv`: ~450 rows, analysis-result columns only
- Clinical queries target `patients.tsv`; per-library queries target `assays.tsv`; cross-level queries `JOIN`

## 3. Goals and non-goals

### Goals

1. Represent patient / specimen / assay as first-class entities with enforceable foreign-key relationships.
2. Zero metadata duplication across levels.
3. Status reportable at any level: analysis, assay, specimen, patient, cohort.
4. Preserve TSV-as-source-of-truth: every file stays `cat`-able, `awk`-able, Excel-openable, git-diffable.
5. Provide a deterministic migration from existing flat manifests with a clear rollback.
6. DuckDB-backed queries (PR #1) give `JOIN` ergonomics without introducing a database.
7. Preserve the POSIX-flock append model; no new server or daemon.

### Non-goals

1. Not moving to SQLite / parquet / anything binary as the source of truth.
2. Not introducing long-format ("tidy") storage — we keep one-column-per-metric.
3. Not supporting arbitrary N-level hierarchies in v0.3 (see §12, Open Question 5).
4. Not changing the summarize-script contract — per-sample TSV with a key column as first col.
5. Not requiring projects to migrate. The flat model stays supported in a compatibility layer through v0.3.x; removal is v1.0.

## 4. Data model

### 4.1. Entities

**Patient** — the clinical/biological subject.
- **Primary key**: `patient_id` (string, unique, non-null).
- **Typical columns**: `age`, `sex`, `diagnosis`, `brca_status`, `neoadjuvant_chemo`, `pfs_months`, `os_months`, `cohort`.

**Specimen** — one tissue collection event from a patient.
- **Primary key**: `specimen_id` (string, globally unique within a project, non-null).
- **Foreign key**: `patient_id` → `patients.patient_id`.
- **Typical columns**: `tissue_site`, `timepoint`, `collection_date`, `pathology_notes`, `tumor_purity`, `storage_id`.

**Assay** — one sequencing library / experiment on a specimen.
- **Primary key**: `assay_id` (string, globally unique within a project, non-null).
- **Foreign key**: `specimen_id` → `specimens.specimen_id`.
- **Required columns**: `assay_type` (enum, configurable in `casetrack.toml`).
- **Optional columns**: `replicate`, `library_id`, `sequencing_date`, `read_count`, `qc_pass`, and **all analysis result columns + `{analysis}_done` timestamps**.

### 4.2. Constraints

| Constraint | Enforcement site |
|---|---|
| All PKs unique + non-null | `casetrack validate` and every mutation |
| `specimens.patient_id` references an existing patient | `casetrack append` / `add-metadata` / `register` |
| `assays.specimen_id` references an existing specimen | same |
| `assays.assay_type` ∈ declared enum | configurable; default: error, `--allow-new-assay-type` to bypass |
| No orphan `_done` columns (present but no paired data column) | `casetrack validate` (existing check, unchanged) |

### 4.3. Where do analyses attach?

Analyses attach to **assays** by default — that's where per-library tool output lives. `modkit`, `tldr`, `xTea`, `scRNA-QC`, `alignment`: all assay-level.

A small set of "analyses" logically attach to **specimens** (pathology review, tumor purity estimate) or **patients** (clinical outcome, centralized germline pathogenicity call). Support these by allowing `_done` columns at those levels too. The level of attachment is a column-placement choice, not a new concept in the data model.

## 5. File layout

```
project/
├── casetrack.toml                           # project config — declares schema + levels
│
├── patients.tsv                             # 1 row per patient
├── specimens.tsv                            # 1 row per specimen; FK to patients
├── assays.tsv                               # 1 row per assay run; FK to specimens
│
├── patients.tsv.provenance.jsonl            # per-level audit logs
├── specimens.tsv.provenance.jsonl
├── assays.tsv.provenance.jsonl
│
├── patients.tsv.schema.json                 # per-level column→analysis maps
├── specimens.tsv.schema.json
├── assays.tsv.schema.json
│
├── .casetrack.lock                          # project-wide lock (multi-file transactions)
│
├── results/                                 # unchanged: full per-assay raw output
├── scripts/                                 # unchanged: summarize_*.py
├── logs/                                    # unchanged: SLURM logs
└── containers/                              # unchanged: .sif files
```

Two things that **change** compared to v0.2:

1. Three TSVs instead of one; three provenance sidecars; three schema sidecars.
2. A new `casetrack.toml` at the project root identifies the directory as a casetrack project and declares its schema. Commands now take `--project-dir` instead of `--manifest`.

Everything else (per-assay `results/` dirs, SLURM scripts, summarize scripts) is unchanged.

## 6. `casetrack.toml`

```toml
[project]
name = "msk_hgsoc_2026"
version = "1"                                # casetrack schema version (for migrations)
created = "2026-04-15T10:00:00"

[levels.patient]
file             = "patients.tsv"
key              = "patient_id"
required_columns = ["patient_id"]

[levels.specimen]
file             = "specimens.tsv"
key              = "specimen_id"
parent           = "patient"
parent_key       = "patient_id"
required_columns = ["specimen_id", "patient_id", "tissue_site"]

[levels.assay]
file             = "assays.tsv"
key              = "assay_id"
parent           = "specimen"
parent_key       = "specimen_id"
required_columns = ["assay_id", "specimen_id", "assay_type"]
assay_types      = ["scRNA", "ATAC", "WGS", "WES", "ONT", "Visium", "CODEX"]

[analysis_defaults]
# default level an appended analysis attaches to unless overridden
default_level = "assay"
```

Parsed once per CLI invocation and memoized. Writes are performed only by `casetrack init`, `casetrack migrate`, and explicit `casetrack config` edits (future).

## 7. CLI changes

### 7.1. Modified commands

| Command | v0.2 (flat) | v0.3 (normalized) |
|---|---|---|
| `init` | `init --manifest X --samples Y` | `init --project-dir X [--from-template NAME]` |
| `append` | `append --manifest X --results R --analysis A` | `append --project-dir X --level {patient,specimen,assay} --results R --analysis A` (default level: `assay`) |
| `add-metadata` | `add-metadata --manifest X --metadata M` | `add-metadata --project-dir X --level LEVEL --metadata M` |
| `status` | `status --manifest X --analysis A` | `status --project-dir X --group-by {analysis,assay,specimen,patient}` |
| `validate` | single file | all three + FK + enum |
| `log` | single file | merged across levels by timestamp; `--level` filter |
| `schema` | one schema.json | three schemas; `--level` filter |
| `dashboard` | single file HTML | nested HTML (cohort → patient → specimen → assay) |
| `rerun` | operates on manifest | operates on `assays.tsv` (default); `--level` override |
| `export` | flat TSV → xlsx/csv/... | joined view → xlsx/csv/...; `--level` exports a single file |
| `projects` | scans for `manifest.tsv` | scans for `casetrack.toml` |
| `query` | `_` = the one manifest | `_` = `patients ⋈ specimens ⋈ assays`; individual levels as named tables |

### 7.2. New commands

**`casetrack migrate`** — convert a flat v0.2 manifest into a v0.3 project.

```
casetrack migrate --flat manifest.tsv \
                  --patient-col patient_id \
                  --specimen-col specimen_id \
                  --assay-col assay_id \
                  --out-dir migrated_project/
```

- User nominates which columns are the PKs at each level.
- Metadata columns are routed by the heuristic below, with `--metadata-map` to override.
- Produces a routing report TSV so the user can audit every column's placement.

**Column routing heuristic:**

1. For each non-key column, compute `is_constant_within(level)` — does the value never vary across rows sharing the same `level_id`?
2. Route to the highest (coarsest) level where it's constant.
3. Columns not constant at any level → assay.
4. Columns that are `{prefix}_done` → go with their prefix's analysis columns (treated as a unit).
5. `--metadata-map "patient:age,brca_status;specimen:site,timepoint"` overrides the heuristic.

**`casetrack register`** — add a single row at a given level interactively / from flags.

```
casetrack register --project-dir ./my_proj --level specimen \
                   --id P001-LOV --parent P001 \
                   --meta tissue_site=left_ovary,timepoint=pre-op
```

Useful for *"specimen collected, register before any assay exists."* Without `register`, you'd need to write a one-row TSV and `add-metadata --allow-new --yes` it.

### 7.3. Deprecated

- `--manifest PATH` → flat-mode fallback. Warns once per process. Removed in v1.0.
- `--samples FILE` (on `init`): replaced by `register` or `add-metadata --level patient --metadata initial_cohort.tsv`.

## 8. Provenance

### 8.1. Per-level logs

Three provenance files, one per level. Each entry records the level it operated on.

```json
{
  "action": "append",
  "level": "assay",
  "transaction_id": "txn_20260415T174235_a1b2c3",
  "analysis": "modkit_methylation",
  "results_file": "modkit_summary.tsv",
  "results_checksum": "f4e1b7c9…",
  "columns_added": ["modkit_mean_meth", "modkit_done"],
  "samples_updated": 1,
  "timestamp": "2026-04-15T17:42:35",
  "user": "sahuno",
  "slurm_job_id": "12345",
  "git": { "commit": "...", "branch": "main", "dirty": false, "toplevel": "..." }
}
```

### 8.2. Multi-level transactions

Operations that mutate more than one file (e.g., `migrate`, `register` that auto-creates missing parents, delete-with-cascade) write a single `transaction_id` to every affected log. `casetrack log` can group by `transaction_id` to show atomic operations.

### 8.3. Viewing

```
casetrack log --project-dir X                    # merged, ordered by timestamp
casetrack log --project-dir X --level specimen   # one level only
casetrack log --project-dir X --transaction TX   # one atomic operation
```

## 9. Concurrency

### 9.1. Locking strategy

- **Single-level mutations** (the overwhelming majority — `append` at `assay` level) take a per-file POSIX `flock` on `{level}.tsv.lock`. Behavior identical to today.
- **Multi-level mutations** (migrate, register-with-parent-creation, delete-cascade) take the project-wide `.casetrack.lock` *first*, then per-file locks in a fixed order (`patient → specimen → assay`) to prevent deadlock, then release in reverse.
- Read-only commands (`status`, `log`, `query`, `dashboard`, `export`, `schema`) take no locks.

### 9.2. Backward compatibility

All per-file semantics of `fcntl.LOCK_EX` are preserved. Existing SLURM-array append patterns work unchanged; only the number of lock files scales from one to three.

## 10. DuckDB query model

### 10.1. Auto-built views

```sql
-- Constructed at query time from casetrack.toml
CREATE VIEW patients   AS SELECT * FROM read_csv('patients.tsv',   delim='\t', header=true, sample_size=-1);
CREATE VIEW specimens  AS SELECT * FROM read_csv('specimens.tsv',  delim='\t', header=true, sample_size=-1);
CREATE VIEW assays     AS SELECT * FROM read_csv('assays.tsv',     delim='\t', header=true, sample_size=-1);

CREATE VIEW _ AS
  SELECT p.*,
         s.* EXCEPT(patient_id),
         a.* EXCEPT(specimen_id)
  FROM       patients  p
  LEFT JOIN  specimens s USING (patient_id)
  LEFT JOIN  assays    a USING (specimen_id);
```

Users querying `_` see the full denormalized cohort. Users who want per-level views reference `patients`, `specimens`, `assays` directly.

### 10.2. Example queries

```bash
# Patients with complete data across three assay types on at least one tumor specimen
casetrack query --project-dir ./hgsoc "
  SELECT DISTINCT patient_id
  FROM _
  WHERE tissue_site LIKE '%ovary%'
    AND qc_pass
  GROUP BY patient_id
  HAVING COUNT(DISTINCT assay_type) = 3"

# Outcome vs TMB correlation (joins patient-level outcome with assay-level TMB)
casetrack query --project-dir ./hgsoc --fmt tsv --output tmb_vs_os.tsv "
  SELECT patient_id, os_months, AVG(tmb) AS mean_tmb
  FROM _
  WHERE assay_type = 'WGS' AND tissue_site LIKE '%tumor%'
  GROUP BY patient_id, os_months"

# Specimen inventory
casetrack query --project-dir ./hgsoc "
  SELECT patient_id, COUNT(specimen_id) AS n_specimens,
         COUNT(assay_id) AS n_assays
  FROM patients
  LEFT JOIN specimens  USING (patient_id)
  LEFT JOIN assays     USING (specimen_id)
  GROUP BY patient_id
  ORDER BY patient_id"
```

## 11. Dashboard UX

### 11.1. Cohort overview page

```
┌─────────────────────────────────────────────────────────────────┐
│ casetrack dashboard — msk_hgsoc_2026                            │
│ Generated 2026-04-15 · 50 patients · 147 specimens · 421 assays │
├─────────────────────────────────────────────────────────────────┤
│ Overall completion (assay × analysis grid)                       │
│   modkit_methylation    380/421   ██████████████████░ 90.3%      │
│   tldr_insertions       312/421   █████████████░░░░░  74.1%      │
│   xtea_somatic_l1       128/421   ██████░░░░░░░░░░░░  30.4%      │
│   scRNA_qc               98/110   ████████████████░░  89.1%      │
│                                                                   │
│ By specimen                                                       │
│   147 specimens, 112 with ≥3 assays, 35 with <3                  │
│                                                                   │
│ By patient                                                        │
│   42 patients complete across all specimens; 8 incomplete        │
└─────────────────────────────────────────────────────────────────┘

(expandable per-patient drill-down below)
```

### 11.2. Per-patient panel

A collapsible `<details>` per patient, each showing:
- Patient-level metadata table
- List of specimens with their metadata + per-assay completion strip
- Click-through to assay-level raw values

### 11.3. Heatmap reorganization

Rows become **specimens** grouped by patient (blank row separators between patients). Columns split into `{assay_type} × {analysis}` for clarity. Same self-contained, no-JS, XSS-safe properties as today's dashboard.

## 12. Open questions and risks

### Q1 — FK strictness on append

**Question**: reject appends that reference a non-existent parent, or auto-create?

**Proposal**: reject by default (exit 2, preview the would-be parent). Offer `--allow-new-parent --yes` pairing (same safety rail as `--allow-new` today) to create missing parent rows inline.

### Q2 — Assay-type enum enforcement

**Question**: strict or advisory?

**Proposal**: strict by default (error if `assay_type` not in `casetrack.toml`'s declared list). `--allow-new-assay-type` opts out. First use of a new type prompts the user to add it to config permanently.

### Q3 — Technical replicates

**Question**: same `(specimen_id, assay_type)` run twice → two rows or one with a replicate column?

**Proposal**: two rows, each with a globally unique `assay_id` and an optional `replicate` integer. Keeps assays.tsv shape uniform.

### Q4 — Analysis output that spans multiple levels

**Question**: some tools produce per-specimen output from multiple per-assay inputs (e.g., a CN-consensus across WGS replicates). Where does it land?

**Proposal**: at the specimen level. The summarize script decides; the user passes `--level specimen` to `append`. Document the pattern in `examples/`.

### Q5 — Is three levels the right number?

**Question**: some studies have aliquot (between specimen and assay), cohort (above patient), or fundamentally different hierarchies (mouse model studies: colony → animal → tissue → library).

**Proposal**: three levels hardcoded in v0.3. In v0.4, generalize to an N-level hierarchy declared entirely in `casetrack.toml` with ordered `[[levels]]` entries and per-level parent references. Treat the patient/specimen/assay triple as a preset template. Validate the design on three levels first before generalizing.

### Q6 — Dashboard scale

**Question**: 500 patients × 3 specimens × 3 assays × 10 analyses = 45K heatmap cells. Current dashboard is DOM-heavy.

**Proposal**: ship as-is for v0.3. Add virtualization (CSS `content-visibility: auto` plus pagination) in a follow-up if real deployments hit it. Note this in release notes.

### Q7 — Backward compatibility horizon

**Question**: how long do we maintain the flat-manifest code path?

**Proposal**: v0.3.x supports flat manifests via `[compatibility] flat_manifest = "manifest.tsv"` in `casetrack.toml`. Deprecation warning on load. Removed in v1.0 (estimated ~6 months post-0.3.0).

## 13. Migration strategy

### 13.1. Tooling

```bash
casetrack migrate \
    --flat old_manifest.tsv \
    --patient-col patient_id \
    --specimen-col specimen_id \
    --assay-col assay_id \
    --metadata-map "patient:age,brca_status;specimen:tissue_site,timepoint" \
    --out-dir migrated_project/
```

Emits:
- `migrated_project/patients.tsv`, `specimens.tsv`, `assays.tsv`, `casetrack.toml`
- `migrated_project/.migration_report.tsv` — one row per source column, indicating target level, rationale (heuristic vs manual), value-cardinality stats
- `migrated_project/.migration_report.md` — human-readable summary with any warnings

The source flat manifest is untouched. Migration is additive.

### 13.2. Codebase rollout

| Phase | Version | Scope |
|---|---|---|
| Phase 1 | v0.3.0-alpha | `casetrack.toml` parser; `init --project-dir`; `migrate` tool; flat mode still default |
| Phase 2 | v0.3.0-beta | Multi-level `append` / `add-metadata` / `validate` / `status --group-by`; flat flagged as deprecated on load |
| Phase 3 | v0.3.0 | `dashboard` + `query` + `rerun` + `export` + `log` + `schema` fully multi-level; `register` command; Nextflow + Claude hooks updated |
| Phase 4 | v1.0.0 | Flat manifest support removed |

### 13.3. For existing consumers

- **Nextflow module** (`examples/nextflow/casetrack.nf`): update `casetrack_append` to use `--project-dir` + `--level assay`. Ship both v0.2 and v0.3 module files during overlap.
- **Claude Code QC hook** (`examples/claude/post_analysis_hook.sh`): replace `MANIFEST` env with `PROJECT_DIR`, append under `cc_<analysis>_review` at assay level. Hook's prompt template unchanged.
- **SLURM `run_modkit.sh`** and peers: update the phase-3 `casetrack append` line.

## 14. Test plan

Net-new test files:

| File | Coverage |
|---|---|
| `tests/test_toml_loader.py` | parse / validate / error messages |
| `tests/test_project_init.py` | `init --project-dir` creates all files; `--from-template` presets |
| `tests/test_multilevel_append.py` | append at each level; analyses attach correctly |
| `tests/test_foreign_keys.py` | FK enforcement on `specimen → patient`, `assay → specimen`; violations blocked |
| `tests/test_assay_type_enum.py` | enum enforcement + `--allow-new-assay-type` |
| `tests/test_status_grouping.py` | `--group-by {analysis,assay,specimen,patient}` correctness |
| `tests/test_migrate.py` | flat → multi-level conversion; routing heuristic; report TSV shape |
| `tests/test_register.py` | single-row registration at each level |
| `tests/test_multilevel_provenance.py` | per-level logs + `transaction_id` for multi-file ops |
| `tests/test_multilevel_query.py` | `_` joined view; per-level tables; FK-driven `JOIN` |
| `tests/test_multilevel_dashboard.py` | nested HTML render; per-patient `<details>`; heatmap reorg |
| `tests/test_compat_flat.py` | v0.2 flat manifests still load; deprecation warning emitted |

Existing tests: adapted to the new default project-dir model with a flat-mode compatibility layer so the baseline 169 continue to pass.

**Target**: ≥ 220 total tests (169 preserved + ~55 new).

## 15. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Migration heuristic mis-routes a metadata column | Medium | The routing report surfaces every decision; user can override via `--metadata-map` and re-run. Migration is additive (doesn't touch the source flat manifest). |
| Users confuse "level" with "analysis" in CLI | Medium | Help text, examples, and a one-page concept diagram in README. `--group-by` defaults that match existing user intuition. |
| Three files harder to eyeball than one | Medium | `casetrack export` produces a single joined TSV on demand. `casetrack query` is the primary cohort-view path. |
| Performance regression for small projects | Low | Three tiny TSVs read sequentially is still microseconds; pandas + DuckDB both trivially handle it. |
| Nextflow / Claude hook users break silently on upgrade | High if unmanaged | Ship v0.3 module files with obvious `--project-dir` flags; loud deprecation warning when loaded by v0.2-style callers; CHANGELOG marks this as a breaking change. |
| Lock contention increases | Low | Per-level locks parallelize better than one global lock. Project-wide lock only taken for rare multi-file ops. |

## 16. Rollout and versioning

- **v0.3.0-alpha** — ~1 week. Schema loader, `init --project-dir`, `migrate`. Flat mode default; new mode opt-in via `--project-dir`.
- **v0.3.0-beta** — ~1 week. Multi-level `append` / `add-metadata` / `status` / `validate` / `log`. New projects default to multi-level when created.
- **v0.3.0** — ~1 week. `dashboard`, `query`, `rerun`, `export`, `schema`, `register` all multi-level. Nextflow + Claude hooks updated. Docs + migration guide.
- **v0.3.x** — bug-fix only.
- **v1.0.0** — flat manifest support removed; announcement 3 months prior.

Total: **~3 weeks of focused engineering time** for the normalized model itself, plus integration updates.

## 17. Appendix A — example end-to-end workflow

### Day 0: project kickoff

```bash
casetrack init --project-dir msk_hgsoc_2026 --from-template hgsoc
# Creates patients.tsv, specimens.tsv, assays.tsv, casetrack.toml
# Preset: assay_types = ["scRNA", "ATAC", "WGS", "WES", "ONT"]

casetrack add-metadata --project-dir msk_hgsoc_2026 --level patient \
    --metadata cohort_enrollment.tsv --yes
# Bulk-registers 50 patients from a clinical CSV.
```

### Day 14: first specimen arrives

```bash
casetrack register --project-dir msk_hgsoc_2026 --level specimen \
    --id P001-LOV --parent P001 \
    --meta tissue_site=left_ovary,timepoint=pre-op,collection_date=2026-04-29
```

### Day 21: first assay completes

```bash
# Phase 1–2 of the SLURM pattern unchanged; summarize_modkit.py produces modkit_summary.tsv
# Phase 3:
casetrack append --project-dir msk_hgsoc_2026 --level assay \
    --results modkit_summary.tsv --analysis modkit_methylation
```

### Day 60: cohort status check

```bash
casetrack status --project-dir msk_hgsoc_2026 --group-by patient
# Per-patient completion rollup.

casetrack status --project-dir msk_hgsoc_2026 --group-by analysis
# Per-analysis progress (same shape as today).
```

### Day 90: publication table

```bash
casetrack query --project-dir msk_hgsoc_2026 --fmt tsv --output table_s1.tsv "
  SELECT p.patient_id, p.brca_status, p.os_months,
         COUNT(DISTINCT s.specimen_id) AS n_specimens,
         COUNT(DISTINCT a.assay_type) AS n_assay_types,
         AVG(a.tmb) FILTER (WHERE a.assay_type='WGS') AS mean_wgs_tmb
  FROM      patients  p
  LEFT JOIN specimens s USING (patient_id)
  LEFT JOIN assays    a USING (specimen_id)
  GROUP BY  p.patient_id, p.brca_status, p.os_months
  ORDER BY  p.os_months"
```

## 18. Appendix B — integration impact summary

### `examples/nextflow/casetrack.nf` (DSL2 module)

- New param: `casetrack_project_dir` (replaces `casetrack_manifest`).
- New param: `casetrack_level` (default `assay`).
- Process body: `--project-dir ${params.casetrack_project_dir} --level ${params.casetrack_level}`.
- Backward-compatible shim detects `casetrack_manifest` and warns.

### `examples/claude/post_analysis_hook.sh`

- `MANIFEST` env var renamed to `PROJECT_DIR`.
- `casetrack append` invocation updated to `--project-dir "$PROJECT_DIR" --level assay`.
- Prompt template unchanged.

### `examples/run_modkit.sh`

- Phase-3 line becomes:
  ```bash
  casetrack append --project-dir "$PROJECT_DIR" --level assay \
      --results summary.tsv --analysis modkit_methylation
  ```

### `scripts/generate_demo_dashboard.py` (Pages workflow)

- Rewritten to build a small 3-level project instead of a flat manifest, to exercise the new dashboard layout.

## 19. Decision requested

This proposal is a draft. Before implementation starts, I'd like alignment on:

1. **Three levels vs N levels** (Q5): hardcode patient/specimen/assay for v0.3, or generalize day-one?
2. **Strict vs advisory FK enforcement** (Q1): reject-by-default with opt-in, or warn-by-default?
3. **Flat-manifest deprecation horizon** (Q7): remove in v1.0 (~6 months) acceptable, or longer?
4. **Migration heuristic tolerance**: should ambiguous columns fail the migration and require `--metadata-map`, or route to assay + warn?

Sign off on these four and I'll open an implementation tracking issue with a task breakdown matching §16.
