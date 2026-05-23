# casetrack — Claude Code Skill Patterns
*Pattern reference for AI agents and human collaborators. Basis for the Claude Code skill.*
*Distilled from real project sessions. Expand with /skill-creator:skill-creator.*

---

## 1. Mental Model

**What casetrack IS:**
- A manifest-centric **tracker** — it records what has been done, what is pending, and what is blocked
- A SQLite database (WAL mode) with a TOML-declared schema, not a pipeline scheduler
- An audit trail: every mutation goes to `provenance.jsonl`; QC events are append-only

**What casetrack IS NOT:**
- A job scheduler (Nextflow/Snakemake does that)
- A file manager (it stores paths, not files)
- A workflow engine (it tracks analyses, not runs them)

**The core loop:**
```
register entities → run tool externally → summarize results to TSV → casetrack append → query status
```

---

## 2. The 3-Level Hierarchy

Every casetrack project has exactly three levels:

```
patients           ← clinical/demographic metadata
  └── specimens    ← biological sample (tumor, normal, cell line…); analysis tracking lives here
        └── assays ← individual sequencing run; per-run QC and basecalling lives here
```

**Key column conventions:**
- Level key columns: `patient_id`, `specimen_id`, `assay_id`
- Parent FK: each level's row must include its parent's key (`specimen` has `patient_id`, `assay` has `specimen_id`)
- Analysis tracking (`*_done` timestamps, stats columns) lives at the level defined in `[analyses.<tool>]`
- Basecalling (per-run) → `assay` level; sorting, methylation, SV → `specimen` level

---

## 3. Project Mode vs Flat Mode

Always use **project mode** for new work. Flat mode (`--manifest`) is legacy.

| Feature | Project mode | Flat mode |
|---|---|---|
| Storage | SQLite | TSV |
| Schema | TOML-declared | Inferred |
| Levels | 3 (patient/specimen/assay) | 1 (flat) |
| QC events | Full system | None |
| Command flag | `--project-dir .` | `--manifest manifest.tsv` |

---

## 4. Project Initialization

```bash
casetrack init \
  --project-dir /path/to/my_project \
  --project-name my_project
```

Creates:
```
my_project/
├── casetrack.toml    ← schema definition (edit this first)
├── casetrack.db      ← SQLite database (gitignored)
├── provenance.jsonl  ← append-only audit log
├── data/             ← raw/processed inputs
├── results/          ← analysis outputs
└── logs/
```

Edit `casetrack.toml` before registering any data. Schema changes after the fact
require `casetrack schema apply`.

---

## 5. TOML Schema Design

The TOML is the schema contract. Declare all columns upfront; add later via `schema apply`.

```toml
[project]
project_id = "my-project"
name       = "my_project"
schema_v   = 1

[levels.patient.columns]
patient_id   = { type = "TEXT", required = true, unique = true }
sex          = { type = "TEXT", enum = ["F", "M", "unknown"] }
cohort       = { type = "TEXT" }
sample_id    = { type = "TEXT" }   # real clinical/external ID
internal_id  = { type = "TEXT" }   # lab-internal label

[levels.specimen.columns]
specimen_id   = { type = "TEXT", required = true, unique = true }
patient_id    = { type = "TEXT", required = true }
specimen_type = { type = "TEXT" }
source        = { type = "TEXT" }  # e.g. "tumor", "germline"

[levels.assay.columns]
assay_id   = { type = "TEXT", required = true, unique = true }
specimen_id = { type = "TEXT", required = true }
assay_type  = { type = "TEXT", required = true }
condition   = { type = "TEXT" }   # "tumor" or "normal"
chemistry   = { type = "TEXT" }
pod5_path   = { type = "TEXT" }   # critical: enables DB-driven samplesheet generation
bam_path    = { type = "TEXT" }
qc_pass     = { type = "BOOLEAN" }

[analyses.dorado_basecaller]
level         = "assay"
column_prefix = "dorado"
summary_tsv   = "dorado_basecaller_summary.tsv"

[analyses.samtools_sort]
level         = "specimen"
column_prefix = "sort"
summary_tsv   = "samtools_sort_summary.tsv"

[analyses.sniffles2]
level         = "specimen"
column_prefix = "sv"
summary_tsv   = "sniffles2_summary.tsv"
```

**Column prefix rule:** every column from the summary TSV gets prefixed with `{prefix}_` in the DB.
A `dorado` prefix + `n_reads` column → `dorado_n_reads` in DB. The key column and `{analysis}_done`
timestamp are never prefixed.

---

## 6. Schema Migration (adding columns after init)

```bash
# 1. Edit casetrack.toml — add new columns to [levels.*.columns]
# 2. Apply to the DB:
casetrack schema apply --project-dir .
# Emits: "Applied N schema change(s); schema_v X → Y"
# Runs ALTER TABLE for each new column — safe, non-destructive
```

**Never manually ALTER the SQLite DB.** Always go through TOML → `schema apply`.

---

## 7. Registering Entities

Use `casetrack add-metadata` to register new or update existing patients/specimens/assays.
This is NOT for analysis results — it's for entity-level metadata.

```bash
# Register patients
casetrack add-metadata \
  --project-dir . \
  --level patient \
  --metadata patients.tsv \
  --allow-new --yes        # required for new rows

# Register specimens (patients must exist first)
casetrack add-metadata \
  --project-dir . \
  --level specimen \
  --metadata specimens.tsv \
  --allow-new --yes

# Register assays (specimens must exist first)
casetrack add-metadata \
  --project-dir . \
  --level assay \
  --metadata assays.tsv \
  --allow-new --yes

# Update existing rows (overwrite NULLs and existing values)
casetrack add-metadata \
  --project-dir . \
  --level patient \
  --metadata patients_update.tsv \
  --overwrite              # no --allow-new needed if IDs already exist
```

**TSV must include the level's key column** (`patient_id`, `specimen_id`, or `assay_id`).
Parent FK column is required for new rows at specimen and assay levels.

**Registration order matters:** patient → specimen → assay (FK enforcement).

---

## 8. `add-metadata` vs `append` — Critical Distinction

| Command | Use for | Requires `--analysis` | Creates new rows |
|---|---|---|---|
| `add-metadata` | Entity metadata, schema columns | No | Yes (with `--allow-new --yes`) |
| `append` | Analysis results from a summary TSV | Yes | No (IDs must pre-exist) |

Use `add-metadata` to register a cohort. Use `append` (or Nextflow's CASETRACK_REGISTER) to record that an analysis ran.

---

## 9. Analysis Tracking — The 3-Phase Pattern

Every analysis follows this pattern:

```
Phase 1: Run the tool (Nextflow / SLURM / local)
Phase 2: Produce a per-entity summary TSV with a key column + result columns
Phase 3: casetrack append --project-dir . --analysis <name> --results summary.tsv --overwrite
```

**Phase 3 in detail:**
```bash
casetrack append \
  --project-dir /path/to/project \
  --analysis samtools_sort \           # matches [analyses.samtools_sort] in TOML
  --results samtools_sort_summary.tsv \ # key col (e.g. specimen_id) + result cols
  --overwrite                           # REQUIRED for reruns; default is fill-only
```

**What happens:** casetrack reads the TOML to find the analysis level + column_prefix,
prefixes each result column, upserts the DB, and writes `{analysis}_done = now()`.

**fill-only default (GOTCHA):** Without `--overwrite`, existing non-NULL values are never
updated. Always pass `--overwrite` for analysis results — a rerun that doesn't update the
DB is silent data staleness.

---

## 10. Nextflow Integration Pattern

The canonical Nextflow integration uses `CASETRACK_REGISTER` (in `casetrack-nf-subworkflows`):

```groovy
// subworkflow pattern
include { CASETRACK_REGISTER } from '../modules/local/casetrack_register'

TOOL_PROCESS(inputs)          // Phase 1
SUMMARIZE_TOOL(tool_outputs)  // Phase 2 — produces summary TSV
CASETRACK_REGISTER(          // Phase 3
    SUMMARIZE_TOOL.out.summary.map { meta, tsv ->
        tuple(meta, tool_name, summary_filename, tsv)
    }
)
```

`CASETRACK_REGISTER` runs `casetrack append --infer-from-path --overwrite`.

**`--infer-from-path` contract:** The summary TSV must be placed at the path template
defined in `[layout.path_templates.<level>]` before calling append. The tool name,
run_tag, patient, specimen, and assay_id are recovered from the directory path — no
explicit flags needed.

**Path template (assay level):**
```
results/{tool}/{run_tag}/{patient_id}/{specimen_id}/{assay_id}/summary.tsv
```

**Critical params required by every NF run:**
```
--casetrack_project_dir   absolute path to casetrack project
--run_tag                 {YYYYMMDD}_{genome}_{description}
--casetrack_level         assay | specimen | patient
--casetrack_bin           casetrack  (or path to binary)
```

---

## 11. Batch/Incremental Pattern

casetrack enables incremental analysis: run only what's pending, safely resubmit.

```bash
# 1. Query pending work from DB
casetrack query \
  --project-dir . \
  --sql "SELECT a.assay_id, a.pod5_path, p.patient_id, s.specimen_id
         FROM assays a
         JOIN specimens s ON a.specimen_id = s.specimen_id
         JOIN patients p ON s.patient_id = p.patient_id
         WHERE a.condition = 'normal'
           AND a.qc_status = 'pass'
           AND a.dorado_basecaller_done IS NULL"

# 2. Write result to CSV → use as NF --input samplesheet

# 3. Submit Nextflow
nextflow run main.nf -profile slurm,apptainer \
  --input pending_normals.csv \
  --casetrack_project_dir /path/to/project \
  --run_tag 20260421_hg38_normal_basecalling \
  --tool dorado_basecaller

# 4. Resubmit safely with -resume
# Nextflow skips completed tasks; casetrack --overwrite prevents double-counting
```

**The pending query pattern:**
```sql
-- What still needs to run?
WHERE {analysis}_done IS NULL AND qc_status = 'pass'

-- What ran and needs reprocessing?
WHERE {analysis}_done < '2026-04-01'   -- older than a date

-- What's the overall progress?
SELECT condition,
       COUNT(*) total,
       SUM({analysis}_done IS NOT NULL) done,
       SUM({analysis}_done IS NULL AND qc_status='pass') pending,
       SUM(qc_status='warn') flagged
FROM assays GROUP BY condition
```

---

## 12. QC Event System

QC events are append-only. `censor` adds an event; `uncensor` resolves it (never deletes).
The `_active` view excludes censored/failed/consent-revoked entities automatically.

**Censor an entity:**
```bash
casetrack censor \
  --project-dir . \
  --level assay \
  --id s_demo_C_6_1_1_1_1_1 \
  --kind qc_warn \
  --reason "pod5 rsync incomplete as of 2026-04-21"
```

**Lift a censor when resolved:**
```bash
casetrack uncensor \
  --project-dir . \
  --level assay \
  --id s_demo_C_6_1_1_1_1_1 \
  --reason "pod5 rsync confirmed complete 2026-04-22"
```

**QC kinds (from TOML `[qc.kinds]`):**
`qc_fail`, `qc_warn`, `consent_revoked`, `sequencing_run_failed`, `library_prep_failed`,
`basecall_accuracy_low`, `contamination`, `protocol_deviation`, `batch_effect_flagged`,
`superseded`, `other`

**qc_status values:** `pass` | `warn` | `fail` | `censored`

**Rule:** Use `qc_warn` for temporary holds (pending data transfer, pending QC review).
Use `qc_fail` or `censored` for permanent exclusion. `consent_revoked` applies at patient
level only and cascades to all specimens and assays.

**`_active` view:** Excludes any entity where `qc_status IN ('fail','censored')` OR
where a parent is censored. Use `_active` for all analysis queries; use raw tables only
for auditing.

---

## 13. Querying

```bash
# Interactive SQL
casetrack query --project-dir . --sql "SELECT ..."

# Full status table
casetrack status --project-dir .

# Schema dump
casetrack schema show --project-dir .

# Via MCP (in Claude Code)
mcp__casetrack__casetrack_query(project_id="my-project", sql="SELECT ...")
mcp__casetrack__casetrack_list_projects()
```

**Useful query patterns:**
```sql
-- Full progress matrix
SELECT p.patient_id, p.internal_id, a.condition,
       COUNT(a.assay_id) n_assays,
       SUM(a.dorado_basecaller_done IS NOT NULL) dorado_done,
       s.samtools_sort_done,
       s.sniffles2_done
FROM patients p
JOIN specimens s ON p.patient_id = s.patient_id
JOIN assays a ON s.specimen_id = a.specimen_id
GROUP BY p.patient_id, a.condition
ORDER BY p.patient_id, a.condition;

-- Generate NF samplesheet for pending normals
SELECT a.assay_id, p.patient_id, s.specimen_id, a.pod5_path, 'hg38' as genome
FROM assays a
JOIN specimens s ON a.specimen_id = s.specimen_id
JOIN patients p ON s.patient_id = p.patient_id
WHERE a.condition = 'normal'
  AND a.qc_status = 'pass'
  AND a.dorado_basecaller_done IS NULL;
```

---

## 14. Results Layout Convention

Outputs from tracked tools are stored at:
```
{casetrack_project_dir}/results/{tool}/{run_tag}/{patient_id}/{specimen_id}/{assay_id}/
```

Primary biological outputs (BAMs, VCFs) are published to `data/processed/` with
genome-tagged filenames:
```
{casetrack_project_dir}/data/processed/{genome}/{patient_id}/{assay_id}/
  {assay_id}.{genome}.sorted.bam
  {assay_id}.{genome}.basecalled.bam
  {assay_id}.{genome}.sniffles.vcf.gz
```

**Why two locations:**
- `results/{tool}/...` — summary TSVs, trace files, per-run reports (ephemeral OK)
- `data/processed/` — primary biological files indexed in DB (persistent, never ephemeral)

The DB stores the `data/processed/` path so any downstream tool can find the file
without scanning the filesystem.

---

## 15. Common Pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| Missing `--overwrite` on append | Reruns don't update DB values | Always pass `--overwrite` to append/CASETRACK_REGISTER |
| Wrong `analysis` name in append | "no such analysis" error | Name must match key in `[analyses.<name>]` in TOML |
| Schema column not in TOML | `no such column` on add-metadata | Add to TOML → `casetrack schema apply` |
| Registration order violated | FK constraint error | Register patient → specimen → assay in order |
| `pod5_path` missing from assays | Can't generate NF samplesheet from DB | Add `pod5_path` to `[levels.assay.columns]` in TOML |
| `_active` not used in query | Returns censored samples | Use `_active` view; raw tables only for auditing |
| `add-metadata` not `append` for new rows | "ID not found" on append | New entities → `add-metadata --allow-new --yes`; analysis results → `append` |
| `column_prefix = ""` rejected | TOML validation error | column_prefix must be a valid non-empty identifier |
| Querying before `schema apply` | Column doesn't exist in DB | Always run `schema apply` after TOML edits |
| Empty string enum value | TOML validation error | Use `null` or a sentinel like `"unknown"` |

---

## 16. Run Tag Convention

```
{YYYYMMDD}_{genome}_{description}
e.g.  20260421_hg38_normal_basecalling
      20260501_hg38_tumor_sort_sv
      20260510_hg38_all_modkit_pileup
```

The run_tag appears in: results paths, DB `{analysis}_run_tag` columns, NF trace files.
It is the primary way to trace which pipeline run produced a given DB row.

---

## 17. Key Commands Cheatsheet

```bash
# Project setup
casetrack init --project-dir . --project-name my_project
casetrack schema apply --project-dir .        # after TOML edits
casetrack schema show --project-dir .         # inspect current schema

# Entity registration
casetrack add-metadata --project-dir . --level patient  --metadata patients.tsv  --allow-new --yes
casetrack add-metadata --project-dir . --level specimen --metadata specimens.tsv --allow-new --yes
casetrack add-metadata --project-dir . --level assay    --metadata assays.tsv    --allow-new --yes

# Analysis tracking
casetrack append --project-dir . --analysis <name> --results summary.tsv --overwrite

# QC events
casetrack censor   --project-dir . --level assay --id <id> --kind qc_warn --reason "..."
casetrack uncensor --project-dir . --level assay --id <id> --reason "..."
casetrack qc-history --project-dir . --id <id>

# Status and queries
casetrack status --project-dir .
casetrack query  --project-dir . --sql "SELECT ..."

# Validation
casetrack validate --project-dir .
casetrack doctor   --project-dir .
```
