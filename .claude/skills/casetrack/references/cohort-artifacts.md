# Cohort-level artifacts (proposal 0009) — deep dive

Read this when registering, listing, or troubleshooting outputs that span **many**
assays at once (joint-genotyped VCFs, panels-of-normals, cohort matrices), or
when wiring the Nextflow side. SKILL.md §15 has the quick version; this file has
the schema, the staleness cascade, the full CLI surface, and the NF subworkflow.

## Table of contents
- Why a sibling table and not a 4th level
- Schema (the two tables)
- CLI: `migrate-cohort`, `append-cohort`, `cohort-artifacts`
- Read-time staleness — how it's derived
- Where staleness surfaces (status / query / export / dashboard / MCP)
- Nextflow: `casetrack_append_cohort` + `COHORT_ARTIFACT_TRACKED`
- Common mistakes

## Why a sibling table and not a 4th level

The three-level hierarchy (`patients → specimens → assays`) is **biological,
single-parent, and static**: every row has exactly one owning parent, fixed at
registration. A joint VCF built from 12 assays has none of those properties — its
membership is *derived*, *many-to-many*, and *dynamically composed per run*.
Forcing it into a 4th level would break the single-parent FK invariant the whole
core relies on.

So 0009 mirrors the `qc_events` pattern: **additive sibling tables** that the
three-level core never references. `LEVEL_ORDER` is untouched; existing projects
keep working; the feature is opt-in via `migrate-cohort`. Proposal 0009 §7 records
the full Option-A (4th level, rejected) vs Option-B (sibling tables, chosen)
reasoning — cite it if a user proposes "just add a cohort level".

## Schema

Two tables, all DDL idempotent (so `init` and `migrate-cohort` share one code path):

```sql
CREATE TABLE cohort_artifacts (
    artifact_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis       TEXT NOT NULL,        -- e.g. joint_genotype, panel_of_normals
    run_tag        TEXT NOT NULL,        -- {YYYYMMDD}_{genome}_{description}
    path           TEXT NOT NULL,        -- artifact location on disk
    checksum       TEXT,                 -- optional, e.g. sha256:...
    n_inputs       INTEGER,              -- cached count of contributing assays
    stats_json     TEXT,                 -- optional cohort-level summary stats (JSON)
    created_at     TEXT NOT NULL,
    created_by     TEXT,                 -- actor; default manual:$USER
    transaction_id TEXT,                 -- ties to provenance.jsonl
    UNIQUE (analysis, run_tag)           -- ← the identity key
);

CREATE TABLE cohort_artifact_inputs (
    artifact_id INTEGER REFERENCES cohort_artifacts(artifact_id) ON DELETE CASCADE,
    assay_id    TEXT NOT NULL,
    PRIMARY KEY (artifact_id, assay_id)  -- many-to-many lineage
);
```

`(analysis, run_tag)` is the unique key. A re-genotyping run uses a **new**
`run_tag` and produces a **new** row — v1 and v2 coexist in the audit trail.
Re-using a `run_tag` for the same `analysis` is a uniqueness violation; the CLI
surfaces a clean error rather than a raw `IntegrityError`.

## CLI

### `migrate-cohort` — make the tables exist
Additive, idempotent, safe to run on any pre-0009 project. Projects created by a
current `casetrack init` already have the tables (no migrate needed).

```bash
casetrack migrate-cohort --project-dir .            # create the two tables + indexes
casetrack migrate-cohort --project-dir . --dry-run  # print the plan, change nothing
```

### `append-cohort` — register one artifact + its lineage
One transaction: inserts the `cohort_artifacts` row, the `cohort_artifact_inputs`
rows, and an `action='append_cohort'` provenance entry.

| Flag | Required | Meaning |
|---|---|---|
| `--project-dir` | yes | project root |
| `--analysis` | yes | analysis name (e.g. `joint_genotype`) |
| `--run-tag` | yes | run identifier; `(analysis, run_tag)` is the unique key |
| `--path` | yes | artifact path on disk |
| `--inputs` | one of | comma-separated contributing `assay_id`s |
| `--inputs-from` | one of | file of `assay_id`s, one per line (an `assay_id` header + extra TSV columns tolerated) |
| `--stats` | no | path to a JSON file of cohort-level summary stats |
| `--checksum` | no | artifact checksum |
| `--created-by` | no | override the recorded actor (default `manual:$USER`) |

You must pass exactly one of `--inputs` / `--inputs-from`; with neither it errors
("no contributing assays").

```bash
casetrack append-cohort --project-dir . \
  --analysis joint_genotype \
  --run-tag 20260521_hg38_jointgt \
  --path data/processed/hg38/cohort/joint.vcf.gz \
  --inputs p1_tumor_ont,p2_tumor_ont,p3_tumor_ont \
  --stats cohort_stats.json
```

### `cohort-artifacts` — list with staleness
```bash
casetrack cohort-artifacts --project-dir .              # all artifacts + fresh/STALE flag
casetrack cohort-artifacts --project-dir . --stale-only # only those with a censored input
casetrack cohort-artifacts --project-dir . --fmt json   # table (default) | tsv | json
```
Columns: `artifact_id, analysis, run_tag, n_inputs, …` plus the derived staleness
flag. The flag column is computed live (see below), never read from a stored field.

## Read-time staleness — how it's derived

An artifact is **`STALE`** when *any* of its contributing assays is currently
**censored or consent-revoked**, evaluated through the same `_active` cascade used
everywhere else (§10; proposal 0002 §4.4). Key consequences:

- **No stored flag.** Staleness is recomputed on every read, so `censor` / `uncensor`
  on a contributing assay (or a `consent_revoked` on its patient, which cascades
  down) flips an artifact STALE / fresh automatically — you never re-run
  `append-cohort` just to refresh a flag.
- **Flagged, not auto-fixed.** casetrack will not silently regenerate the joint VCF.
  Re-running the analysis (with a new `run_tag`) is the operator's decision.
- A consent revocation at the patient level cascades to all that patient's assays,
  so it can mark an artifact STALE even if you only censored at the patient level.

## Where staleness surfaces

You rarely need a bespoke SQL query — the derived flag is exposed in every read path:

| Surface | How |
|---|---|
| `casetrack status` | appends a cohort-artifact section: count + per-artifact fresh/STALE |
| `casetrack query` | `_cohort_artifacts` DuckDB view, with the derived staleness column |
| `casetrack export` | `--include-cohort-artifacts` adds them to the TSV/JSON export |
| HTML dashboard | dedicated cohort-artifacts section |
| MCP (Claude Code) | `mcp__casetrack__casetrack_cohort_artifacts(project_id="<slug>", stale_only=False)` |

For ad-hoc SQL, prefer the `_cohort_artifacts` view over the raw tables — the view
carries the derived staleness; the raw `cohort_artifacts` table does not.

## Nextflow

The cohort analogue of `CASETRACK_REGISTER` is the `casetrack_append_cohort`
process (`examples/nextflow/casetrack.nf`), wrapped by the `COHORT_ARTIFACT_TRACKED`
subworkflow (`examples/nextflow/subworkflows/local/cohort_artifact_tracked.nf`).

```groovy
include { COHORT_ARTIFACT_TRACKED } from './subworkflows/local/cohort_artifact_tracked.nf'

workflow {
    // ch_assay_ids      : queue channel, one assay_id per contributing assay
    // ch_artifact_stats : value channel, ONE tuple (artifact_path, stats-or-[])
    COHORT_ARTIFACT_TRACKED(ch_assay_ids, ch_artifact_stats)
}
```

Mechanics worth knowing:
- The subworkflow `collectFile`s the assay-ids into a single `${run_tag}.inputs.txt`
  manifest, then `combine`s it with the `(artifact, stats)` tuple into the one
  fan-in call the process expects.
- **Stats are optional.** Pass `[]` (not a `{}` placeholder file) for the stats slot
  and the process drops `--stats` entirely. This is decision §5 of 0009's accepted
  list — no empty-JSON sentinel files.
- `params.cohort_analysis` (default `joint_genotype`) and `params.run_tag` (default
  `run`) drive the `(analysis, run_tag)` key. Set `run_tag` per run so v1/v2 coexist.
- Same write-serialization discipline as `CASETRACK_REGISTER`: run on the `local`
  executor with `maxForks = 1` so the SQLite writes and provenance log stay clean.

`examples/nextflow/README.md` (§"Cohort-level artifacts") has the full process body.

## Common mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Tracking a joint VCF / PoN / matrix at a level | no single owning entity | use `append-cohort`, not `append` |
| `append-cohort` on a pre-0009 project | `no such table: cohort_artifacts` | `casetrack migrate-cohort` once first |
| Reused `run_tag` for a re-run | uniqueness error on `(analysis, run_tag)` | give each run a distinct `--run-tag`; both coexist |
| Passed neither `--inputs` nor `--inputs-from` | "no contributing assays" error | pass exactly one |
| Expected staleness to auto-regenerate the artifact | STALE flag set, file unchanged | staleness is flagged, not fixed — re-run with a new `run_tag` |
| Queried raw `cohort_artifacts` for staleness | no staleness column | query the `_cohort_artifacts` view instead |
| Handed a `{}` file to the NF stats slot | needless placeholder file | pass `[]` — the process drops `--stats` |
