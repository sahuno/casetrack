# Reference artifacts (proposal 0010) — deep dive

Read this when declaring versioned references in TOML, bumping a reference version,
checking whether outputs are stale after a reference update, or wiring
`append`/`append-cohort` reference capture. SKILL.md §16 has the quick version;
this file has the schema, the staleness algorithm, worked examples, and migration.

## Table of contents
- Why reference tracking is a separate concern from 0009
- Schema (the two tables)
- TOML contract
- Capture paths: `append`, `--uses-references`, `--no-track-references`, `append-cohort`
- Three-state staleness algorithm with worked examples
- Orthogonality with 0009 (input_stale vs ref_stale)
- Where staleness surfaces (references / status / query / export / dashboard / MCP)
- Migration (`migrate-references` / `init`)
- Documented limitations

---

## Why reference tracking is a separate concern from 0009

Proposal 0009 (cohort artifacts) answers: *"is this output stale because a contributing
sample was removed from the cohort?"* It is input-centric — the lineage of assay IDs
that fed into a joint VCF or panel-of-normals.

Proposal 0010 answers: *"is this output stale because a reference input changed?"*
A variant-call VCF built against hg38_v0 is not reproducible against hg38_v1, even if
every contributing assay is still active and uncensored. Version bumps are routine:
genome patches, new dbSNP releases, refreshed repeat libraries, updated interval lists.

The two staleness signals are **orthogonal**: a cohort artifact can be `input_stale`
(0009), `ref_stale` (0010), both, or neither. They're tracked in separate tables and
surfaced as separate columns — collapsing them would hide the specific cause.

---

## Schema

Two tables, all DDL idempotent (so `init` and `migrate-references` share one code path):

```sql
CREATE TABLE reference_artifacts (
    ref_key     TEXT PRIMARY KEY,   -- e.g. "genome", "dbsnp", "intervals"
    path        TEXT NOT NULL,      -- current path on disk (from TOML)
    version     TEXT NOT NULL,      -- current version string (from TOML)
    description TEXT,               -- optional human label
    updated_at  TEXT NOT NULL       -- timestamp of last schema apply that changed this row
);

CREATE TABLE reference_usage (
    usage_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    output_id   TEXT    NOT NULL,   -- assay_id / specimen_id / patient_id / artifact_id
    ref_key     TEXT    NOT NULL REFERENCES reference_artifacts(ref_key),
    version_used TEXT   NOT NULL,   -- version string at time of append
    scope       TEXT    NOT NULL,   -- 'analysis' or 'cohort'
    analysis    TEXT,               -- analysis name (matches [analyses.<name>] key)
    recorded_at TEXT    NOT NULL,
    UNIQUE (output_id, ref_key, scope)  -- one row per (output, reference, scope)
);

-- Partial unique index: one live analysis-scope row per (output_id, ref_key)
CREATE UNIQUE INDEX IF NOT EXISTS ux_ref_usage_analysis
    ON reference_usage (output_id, ref_key)
    WHERE scope = 'analysis';

-- Partial unique index: one live cohort-scope row per (output_id, ref_key)
CREATE UNIQUE INDEX IF NOT EXISTS ux_ref_usage_cohort
    ON reference_usage (output_id, ref_key)
    WHERE scope = 'cohort';
```

### Key design points

- `reference_artifacts` holds the **canonical current set** — one row per `ref_key`,
  materialized from TOML on every `schema apply`. It is upserted (not append-only):
  bumping a version in TOML updates the existing row's `version` and `updated_at`.
  No history lives here; history is in `provenance.jsonl` (`reference_version_change`
  action, logged by `schema apply` whenever a version string changes).
- `reference_usage` is the many-to-many edge, written by `append` and `append-cohort`.
  The partial unique indexes enforce that each `(output_id, ref_key, scope)` combination
  has exactly one row — a re-run with `--overwrite` updates the `version_used` in place.
- `scope = 'analysis'` means the `output_id` is an assay/specimen/patient row.
  `scope = 'cohort'` means `output_id` is a `cohort_artifacts.artifact_id` (0009).

---

## TOML contract

References are declared at the top level of `casetrack.toml` in a `[references]` block.
Each key becomes a `ref_key`; each value must have `path` and `version`:

```toml
[references]
genome    = { path = "/data/hg38_v1/Homo_sapiens_assembly38.fasta", version = "hg38_v1" }
dbsnp     = { path = "/data/hg38_v1/dbsnp155.vcf.gz",               version = "155" }
intervals = { path = "/data/hg38_v1/wgs_calling_regions.bed",        version = "hg38_v1_wgs" }
gtf       = { path = "/data/gencode47/gencode.v47.annotation.gtf.gz", version = "gencode_v47",
              description = "GENCODE v47 primary assembly annotation" }

[analyses.variant_call]
level          = "specimen"
column_prefix  = "vc"
summary_tsv    = "variant_call_summary.tsv"
uses           = ["genome", "dbsnp"]

[analyses.rna_quant]
level          = "specimen"
column_prefix  = "rq"
summary_tsv    = "rna_quant_summary.tsv"
uses           = ["genome", "gtf"]
```

- `uses` is a list of `ref_key` strings declared in `[references]`. At `schema apply`
  time, casetrack validates that every key in `uses` exists in `[references]`. A typo
  or missing key is a hard error, not a warning.
- `description` is optional; it appears in `casetrack references` table output.
- The `version` string is arbitrary — semver, date, patch label, anything. The only
  constraint: it must change when the reference changes. casetrack never hashes files.

### Running `schema apply`

```bash
casetrack schema apply --project-dir .
```

This upserts `reference_artifacts` from the current TOML. If any `version` string
changed since the last apply, an action `reference_version_change` is logged to
`provenance.jsonl` with the old and new version strings. Existing `reference_usage`
rows are **not** modified — they retain the `version_used` at the time of the `append`
that wrote them. This is what makes old outputs appear `STALE` after a bump.

---

## Capture paths

### Auto-capture from TOML (the default)

When an analysis has a `uses` list in `[analyses.<name>]`, `casetrack append` reads the
current `reference_artifacts` versions and writes one `reference_usage` row per
`ref_key` for every output row in the summary TSV:

```bash
casetrack append --project-dir . --analysis variant_call \
  --results vc_summary.tsv --overwrite
# → writes reference_usage rows: (specimen_id, "genome", "hg38_v1", "analysis")
#                                 (specimen_id, "dbsnp",  "155",     "analysis")
#   for every specimen_id in vc_summary.tsv
```

On a rerun with `--overwrite`, the partial unique index ensures the `version_used`
is updated in place — no duplicate rows accumulate.

### `--uses-references` override

Override which references are captured for a specific call, regardless of what `uses`
declares in TOML:

```bash
casetrack append --project-dir . --analysis variant_call \
  --results vc_summary.tsv --overwrite \
  --uses-references genome,dbsnp,intervals
```

This is useful when a one-off run used an extra reference not declared in the TOML's
`uses` list, or when you want to narrow the capture to a subset of the `uses` list.
The `ref_key` values in `--uses-references` must exist in `[references]`; unknown keys
are a hard error.

### `--no-track-references` opt-out

Skip reference tracking entirely for this append call. The output appears with state
`untracked` in `casetrack references` output:

```bash
casetrack append --project-dir . --analysis variant_call \
  --results vc_summary.tsv --overwrite \
  --no-track-references
```

Use this for one-off exploratory runs where you don't want reference-staleness noise,
or when the analysis genuinely didn't use any declared references.

### `append-cohort` with reference tracking

Cohort artifacts follow the same logic. Auto-capture from `[analyses.<name>].uses`:

```bash
casetrack append-cohort --project-dir . \
  --analysis joint_genotype \
  --run-tag 20260521_hg38_jointgt \
  --path data/processed/hg38/cohort/joint.vcf.gz \
  --inputs p1_tumor,p2_tumor,p3_tumor
# → writes reference_usage rows with scope = 'cohort' for genome, dbsnp
#   (if joint_genotype has uses = ["genome", "dbsnp"] in TOML)
```

Override:
```bash
casetrack append-cohort ... --uses-references genome
```

Opt out:
```bash
casetrack append-cohort ... --no-track-references
```

---

## Three-state staleness algorithm — worked examples

Staleness is computed live on every read from `reference_usage` JOIN `reference_artifacts`.
No stored flag exists — the state tracks TOML changes automatically.

### Algorithm

```
For each row in reference_usage (grouped by output_id):
  For each ref_key used by that output:
    current_version = reference_artifacts.version  WHERE ref_key = <ref_key>
    if current_version is NULL:
      → reason: "reference removed: <ref_key>"
      → state = STALE
    elif current_version != version_used:
      → reason: "<ref_key>: <version_used> -> <current_version>"
      → state = STALE
  If any ref_key is STALE → output state = STALE (with concatenated reasons)
  If all ref_keys are current → output state = fresh
  If output_id not in reference_usage at all → state = untracked
```

### Example 1 — version bump (single reference)

TOML before: `genome = { version = "hg38_v0" }`
`append` runs → `reference_usage` row: `(s001, "genome", "hg38_v0", "analysis")`
State: `fresh`

TOML after: `genome = { version = "hg38_v1" }`
`casetrack schema apply` → `reference_artifacts.version` for "genome" = "hg38_v1"
State: **`STALE`** — reason: `genome: hg38_v0 -> hg38_v1`

Re-run analysis:
`append --overwrite` → `reference_usage` row updated: `(s001, "genome", "hg38_v1", "analysis")`
State: `fresh`

Revert TOML back to `hg38_v0` + `schema apply`:
State: **`STALE`** again — reason: `genome: hg38_v1 -> hg38_v0`

### Example 2 — multiple references, one changed

`reference_usage` rows for specimen s002:
- `(s002, "genome", "hg38_v0", "analysis")` ← old
- `(s002, "dbsnp",  "155",     "analysis")` ← still current

TOML: `genome.version = "hg38_v1"`, `dbsnp.version = "155"`

State: **`STALE`** — reason: `genome: hg38_v0 -> hg38_v1`
(dbsnp is current; it doesn't contribute to the reason string)

### Example 3 — reference removed from TOML

TOML previously had `dbsnp = { version = "154" }`, now the key is deleted.
`schema apply` → the `reference_artifacts` row for "dbsnp" is removed.
`reference_usage` rows pointing to "dbsnp" become orphans (their `ref_key` FK no
longer resolves). The orphan check in the staleness query catches this:

State: **`STALE`** — reason: `reference removed: dbsnp`

`casetrack validate` also reports orphan `reference_usage` rows.

### Example 4 — cohort artifact with both staleness types

A joint VCF was built from three assays and used genome + dbsnp. Later:
- assay p2_tumor is censored (0009 input staleness)
- `genome.version` is bumped from hg38_v0 to hg38_v1 (0010 reference staleness)

`casetrack cohort-artifacts` output for this artifact:
- `input_stale = STALE`  (p2_tumor censored)
- `ref_stale   = STALE`  (genome: hg38_v0 -> hg38_v1)

Re-running the joint genotype with p2_tumor excluded and against hg38_v1 produces a new
`run_tag` row with both flags clear — the old artifact row is retained for audit.

---

## Orthogonality with 0009

| Flag | Source | Question answered |
|---|---|---|
| `input_stale` (0009) | `cohort_artifact_inputs` JOIN `_active` view | "Was a contributing sample removed from the cohort?" |
| `ref_stale` (0010) | `reference_usage` JOIN `reference_artifacts` | "Was a reference input updated since this was built?" |

Both flags are read-time, both are derived independently, and both appear side-by-side
in the `_cohort_artifacts` DuckDB view and `casetrack cohort-artifacts` output.

Per-assay/specimen/patient outputs (scope = `'analysis'`) only have `ref_stale` — they
are individual rows in the three-level hierarchy and don't have the concept of "input
assays" that 0009 tracks.

---

## Where staleness surfaces

### `casetrack references` (primary command)

```bash
casetrack references --project-dir .              # all outputs with ref state + reason
casetrack references --project-dir . --stale-only # only STALE outputs
casetrack references --project-dir . --fmt tsv    # table (default) | tsv | json
```

Columns: `output_id`, `scope`, `analysis`, `ref_key`, `version_used`, `current_version`,
`state` (fresh | STALE | untracked), `reason`.

### Other read paths

| Surface | How |
|---|---|
| `casetrack status` | Reference-artifacts section: count of fresh / STALE / untracked outputs |
| `casetrack query` | `_reference_usage` DuckDB view with derived `state` and `reason` columns |
| `casetrack cohort-artifacts` | `ref_stale` column alongside `input_stale` |
| `_cohort_artifacts` DuckDB view | Both `input_stale` and `ref_stale` columns |
| `casetrack export --include-references` | Adds reference-usage rows to TSV/JSON export |
| HTML dashboard | Dedicated reference-artifacts section |
| MCP (Claude Code) | `mcp__casetrack__casetrack_references(project_id="<slug>", stale_only=False)` |
| `casetrack validate` | Reports orphan `reference_usage` rows (ref_key present in usage but absent from `reference_artifacts`) |

---

## Migration

### Pre-0010 projects

```bash
casetrack migrate-references --project-dir .            # create tables + indexes + materialise from TOML
casetrack migrate-references --project-dir . --dry-run  # print the plan, change nothing
```

`migrate-references` is additive and idempotent — safe to run on any project, including
one that already has the tables from a prior run. It also materializes `reference_artifacts`
from the current TOML (same as `schema apply` does), so after migration `casetrack references`
will immediately reflect the current TOML state.

### New projects

Projects created by a current `casetrack init` already have both tables — no migration needed.

### Version history

Every time `schema apply` changes a `reference_artifacts.version` string (i.e., the TOML
version is different from the stored value), it appends an action `reference_version_change`
to `provenance.jsonl`:

```json
{
  "action": "reference_version_change",
  "ref_key": "genome",
  "old_version": "hg38_v0",
  "new_version": "hg38_v1",
  "timestamp": "2026-05-21T14:23:00Z",
  "actor": "manual:ahunos"
}
```

This is the only place version history is stored. The `reference_artifacts` table itself
is always the current state, not a history table.

---

## Documented limitations

- **Staleness keys on version string only.** If the reference file at `path` changes on
  disk but the `version` string in TOML is not updated, staleness is **not detected**.
  casetrack never hashes reference files. This is an intentional design choice documented
  in proposal 0010 §6.2 — hashing large genome FASTAs on every `schema apply` would be
  prohibitively slow. The operator is responsible for bumping `version` when a reference
  changes.

- **Content drift without a version bump fires nothing** — if you patch a reference file
  in place and forget to bump `version`, all outputs appear `fresh` even though they
  were built against the old content. A future `casetrack doctor --references` command
  will compare checksums for this case, but checksum tracking is not in scope for 0010.

- **Orphan `reference_usage` rows** accumulate when a `ref_key` is removed from the
  TOML `[references]` block entirely. On the next `schema apply`, the corresponding
  `reference_artifacts` row is deleted, but `reference_usage` rows pointing to that key
  remain. `casetrack validate` reports them; a future `doctor` command will prune them.
  The staleness algorithm treats orphan usage rows as `STALE` with reason
  `"reference removed: <ref_key>"`.

- **`--uses-references` validates against current TOML at call time.** If you declare a
  `ref_key` in `--uses-references` that is not in `[references]`, the `append` fails
  with a hard error. This prevents untracked references from silently accumulating.
