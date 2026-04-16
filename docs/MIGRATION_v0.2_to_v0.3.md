# Migrating from casetrack v0.2 → v0.3

v0.3 replaces the flat-manifest TSV with a SQLite-backed project directory
that stores the three biological levels of a cancer-genomics cohort
(**patient → specimen → assay**) as normalized tables with enforced
foreign keys. The TSV becomes an on-demand export via `casetrack export`
rather than the source of truth.

This guide walks through the one-time migration and the command-surface
changes you'll see after it.

---

## TL;DR — happy-path migration

```bash
# 1. Upgrade casetrack.
pip install --upgrade casetrack

# 2. Migrate your existing flat manifest.
casetrack migrate \
    --flat old_manifest.tsv \
    --patient-col patient_id \
    --specimen-col specimen_id \
    --assay-col assay_id \
    --out-dir cohort_v3/

# 3. Review the auto-generated routing report.
cat cohort_v3/.migration_report.md

# 4. If any column landed at the wrong level, re-run with an override.
casetrack migrate \
    --flat old_manifest.tsv \
    --patient-col patient_id --specimen-col specimen_id --assay-col assay_id \
    --metadata-map 'patient:age,brca_status;specimen:tissue_site,timepoint' \
    --out-dir cohort_v3/ --force

# 5. Update your pipelines to point at the project directory.
#    Search-and-replace:
#      --manifest ${MANIFEST}  →  --project-dir ${PROJECT_DIR}

# 6. Keep the source TSV around (it's automatically copied to
#    cohort_v3/sandbox/source_manifest.tsv). Needed if you ever run
#    `casetrack recover` to rebuild the DB from provenance.jsonl.
```

---

## What `migrate` does

Given a flat TSV with `patient_id`, `specimen_id`, and `assay_id` columns
(plus any other metadata), `casetrack migrate` produces a v0.3 project
under `--out-dir`:

```
cohort_v3/
├── casetrack.toml        # declared schema (three levels, per-column types)
├── casetrack.db          # SQLite database, WAL journaling
├── provenance.jsonl      # every mutation logged (init + migrate entry)
├── .gitignore            # excludes casetrack.db* + exports/
├── .migration_report.tsv # machine-readable: column → level → reason
├── .migration_report.md  # human-readable version
└── sandbox/
    └── source_manifest.tsv   # verbatim copy of your input, never deleted
```

### Column routing — how columns are assigned to a level

For each column that isn't a level key, migrate walks up the hierarchy
from **finest to coarsest** looking for the level at which the column's
value is **constant within every group** (ignoring NaN):

| Column is constant within every... | → assigned to |
|---|---|
| patient | patient level |
| specimen (but varies across specimens) | specimen level |
| neither (varies within specimens) | assay level |

This is deterministic — there's no ambiguity by construction — but the
heuristic can land a column at the wrong level if your data happens to
be constant-within-patient by coincidence (e.g., `tissue_site = tumor`
for a cohort where every patient has only one specimen). Use
`--metadata-map` to override:

```bash
# Force `tissue_site` to specimen level even if it's currently
# constant-within-patient by coincidence:
casetrack migrate ... --metadata-map 'specimen:tissue_site,timepoint'
```

The routing decisions land in `.migration_report.md` so you can review
every assignment before re-running with overrides.

### Column types

Inferred from pandas dtypes:
- int-like → `INTEGER`
- float-like → `REAL`
- bool → `BOOLEAN`
- anything else → `TEXT`

Adjust by editing `casetrack.toml` after migration and running
`casetrack schema apply`, or at analysis time via
`casetrack append --col-type name:TYPE,…`.

---

## CLI change cheatsheet

| Task | v0.2 (flat) | v0.3 (project) |
|---|---|---|
| Create | `init --manifest M.tsv --samples s.txt` | `init --project-dir D [--from-template hgsoc]` |
| Append analysis | `append --manifest M.tsv --results R.tsv --analysis A` | `append --project-dir D [--level assay] --results R.tsv --analysis A` |
| Bulk metadata | `add-metadata --manifest M.tsv --metadata X.tsv` | `add-metadata --project-dir D --level LEVEL --metadata X.tsv [--allow-new --yes]` |
| Single-row insert | n/a | `register --project-dir D --level L --id ID [--parent P] [--meta k=v,…]` |
| Status | `status --manifest M.tsv` | `status --project-dir D [--group-by {analysis,assay,specimen,patient}]` |
| Validate | `validate --manifest M.tsv` | `validate --project-dir D` |
| Log | `log --manifest M.tsv` | `log --project-dir D [--level L] [--transaction TX]` |
| Schema | `schema --manifest M.tsv` (JSON sidecar) | `schema --project-dir D {show,dump,check,apply}` |
| SQL query | `query --manifest M.tsv "SQL"` | `query --project-dir D "SQL"` (views: patients, specimens, assays, `_`) |
| Export | `export --manifest M.tsv --output O` | `export --project-dir D --output O [--shape {tables,joined}]` |
| Dashboard | `dashboard --manifest M.tsv --output dash.html` | `dashboard --project-dir D --output dash.html` (nested HTML) |
| Rerun | `rerun --manifest M.tsv --analysis A --script S` | `rerun --project-dir D --analysis A --script S [--level L]` |
| Cross-project scan | `projects --root R` | same — now detects both v0.2 and v0.3 projects under R |
| **New** — concurrency test | — | `doctor --project-dir D [--workers N] [--writes M]` |
| **New** — rebuild DB | — | `recover --project-dir D [--from LOG.jsonl]` |

---

## Behavioral differences

### Strict FK enforcement

`casetrack register --level specimen --id S1 --parent PHANTOM` exits 2
because `PHANTOM` isn't a known patient. Opt in to inline-creating the
parent with `--allow-new-parent --yes`. Same pattern for
`add-metadata --allow-new --yes` when the TSV contains unknown IDs.

### Fill-only by default on re-run

Both `append` and `add-metadata` do `SET col = COALESCE(col, ?)` by
default, preserving existing non-null values. Pass `--overwrite` to
replace.

### Schema drift is a first-class concept

`casetrack schema check` compares `casetrack.toml` to the live DB.
`casetrack schema apply` adds any declared-but-missing columns via
`ALTER TABLE` and bumps `schema_v` in the TOML + provenance.

### Queries go through DuckDB with the SQLite scanner

`casetrack query --project-dir D "SELECT ..."` attaches `casetrack.db`
READ_ONLY and publishes these views in the default catalog:
- `patients`, `specimens`, `assays`
- `_` = `assays ⋈ specimens ⋈ patients` (inner join — one row per
  assay with all ancestor metadata inlined)

---

## Keeping flat mode working during the transition

`--manifest` still works in every command, but emits a one-shot
deprecation warning pointing at `casetrack migrate`. Silence with:

```bash
export CASETRACK_NO_DEPRECATION=1
```

Flat mode is scheduled for removal in **v1.0** (~6 months after the
v0.3.0 release).

---

## Reproducibility — DB rebuild from provenance

`provenance.jsonl` is append-only and records every mutation with its
exact SQL + source file + checksum. If `casetrack.db` is ever lost or
corrupted:

```bash
casetrack recover --project-dir cohort_v3/
```

Replays every entry in order:
- **init_project / schema_apply / register** — self-contained (no
  source files needed).
- **append / add_metadata** — re-reads the recorded TSV and verifies
  its checksum matches what was logged.
- **migrate** — replays from `sandbox/source_manifest.tsv`.

If a source TSV has moved or changed, recover exits 2 and tells you
which entry failed. Re-run with `--permit-partial` to accept a partial
rebuild and re-run the missing steps by hand.

---

## Pipeline integration — Nextflow

The `casetrack.nf` module ships with new v0.3 processes alongside the
existing v0.2 ones:

```groovy
include { casetrack_append_project } from './casetrack.nf'

workflow {
    summarize_modkit(samples_ch)       // emits (analysis, tsv)
    casetrack_append_project(summarize_modkit.out)
}
```

```
params.casetrack_project_dir = "./cohort_v3"
params.casetrack_level       = "assay"   // default; set to patient/specimen
                                         // for cross-level analyses
```

---

## Claude post-analysis hook

The hook now accepts `PROJECT_DIR` in place of `MANIFEST`:

```bash
export SAMPLE_ID="A001" ANALYSIS="modkit" \
       PROJECT_DIR="/path/to/cohort_v3" \
       RESULTS_TSV="/path/to/summary.tsv" \
       LEVEL="assay"
bash examples/claude/post_analysis_hook.sh
```

Falls back cleanly to `MANIFEST` if `PROJECT_DIR` is unset.

---

## Common errors and their fixes

| Error | Fix |
|---|---|
| `Error: project directory not found: ...` | Check path; `init --project-dir` first. |
| `Error: casetrack.toml not found in ...` | `init --project-dir` has been run and the dir contains `casetrack.db` but the TOML was deleted. Run `casetrack schema dump > casetrack.toml`. |
| `Error: {level} {id} does not exist` (exit 2) | The parent you referenced isn't registered. Either register it first or pass `--allow-new-parent --yes`. |
| `Error: {N} key(s) ... do not exist in table` (exit 2) | Append/add-metadata found IDs in the TSV that aren't in the DB. Register them first, or pass `--allow-new --yes` on add-metadata. |
| `Error: columns not declared in casetrack.toml` | add-metadata only touches declared columns. For analysis columns, use `append`, which auto-adds via ALTER TABLE. For schema columns, edit the TOML and `casetrack schema apply`. |

---

*Questions? Open an issue on https://github.com/sahuno/casetrack.*
