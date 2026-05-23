# Proposal 0006 — Assay lineage (`assay_merges`) and batch tracking (`batch_id`)

**Status**: draft (2026-04-20)
**Target release**: casetrack v0.6.0
**Breaking**: no (additive schema; all new columns nullable; new tables independent of existing FK graph)
**Author**: Samuel Ahuno

---

## Motivation

ONT sequencing routinely produces multiple flowcell runs per sample. In the
current three-level hierarchy (`patients → specimens → assays`) run-level assays
exist as tracked entities, but their relationship to the specimen-level analysis
that consumed them is invisible to casetrack.

### Concrete reference case: project_demo

```
p_demo_6 (patient)
  └── p_demo_6_tumor (specimen)    ← modkit methylation lands here (specimen level)
        ├── s_demo_6_1_1_1_1_1     ← flowcell 1 BAM, 10.9M reads, R10.4.1
        └── s_demo_6_1_2_1_1_1     ← flowcell 2 BAM,  9.9M reads, R10.4.1
```

Both run-level assays are already registered with `flowcell_id`, `chemistry`,
`total_reads`, `mapped_pct`. Methylation results from `MODKIT_MERGED_TRACKED`
landed on `p_demo_6_tumor` (specimen) — modkit pileup reads both BAMs
simultaneously; no explicit merged BAM artifact is written to disk.

Casetrack currently cannot answer:

- "Which flowcells fed p_demo_6_tumor's methylation call?"
- "If s_demo_6_1_1_1_1_1 had a basecalling issue, which specimens are affected?"
- "Were run1 and run2 from the same reagent lot / library prep batch?"
- "Across the cohort, which specimens drew from a contaminated flowcell batch?"

### Two distinct patterns in practice

| Pattern | Example | Artifact on disk? |
|---|---|---|
| **Implicit merge** | modkit pileup reads N BAMs in one call | No — specimen IS the merge unit |
| **Explicit merge** | samtools merge produces a combined BAM | Yes — a new BAM file, potentially a new assay |

Both need lineage tracking. The schema below handles both.

This proposal adds two orthogonal, non-breaking features:

1. **Assay lineage** — `assay_sources` join table recording which assays (or
   which assay→specimen pairs) fed a downstream analysis unit. Plus
   `casetrack link-sources` to register the relationship.
2. **Batch grouping** — `batch_id` nullable column on `assays` + a `batches`
   metadata table, plus `casetrack add-batch` / `casetrack censor --batch`.

These features are independent but ship together because they interact at the
QC layer (a censored batch cascades to downstream analyses that consumed its
sources) and at `casetrack rerun` (sources may need re-running after a batch
is remediated).

---

## Design decisions (locked)

### D1 — No 4th hierarchy level; lineage tracked as a join table

Adding a `runs` level between specimens and assays would be breaking (schema
migration, all CLI flags, all FK enforcement, all queries). Not justified.

Instead: a new `assay_sources` join table records lineage between existing
entities. It supports two modes:

```
Mode A — assay → assay  (explicit merged BAM case)
  merged_assay_id: "p_demo_6_merged"  →  source_assay_id: "s_demo_6_1_1_1_1_1"
  merged_assay_id: "p_demo_6_merged"  →  source_assay_id: "s_demo_6_1_2_1_1_1"

Mode B — assay → specimen  (implicit merge / modkit multi-BAM case)
  consumer_specimen_id: "p_demo_6_tumor"  →  source_assay_id: "s_demo_6_1_1_1_1_1"
  consumer_specimen_id: "p_demo_6_tumor"  →  source_assay_id: "s_demo_6_1_2_1_1_1"
```

The table has nullable `merged_assay_id` and `consumer_specimen_id` — exactly
one is non-null per row (enforced by CHECK).

### D2 — `assay_merges` is many-to-many and supports recursive merges

A merged assay can itself be a source for a further merge. `casetrack status`
shows direct sources only; `--deep` shows transitive provenance.

### D3 — `casetrack rerun` targets the merged assay, not its sources

If `p_demo_6.modkit_done` is null, `rerun --analysis modkit` emits the sbatch
for `p_demo_6`. Source run assays are independent entities. Add
`--include-sources` to also emit rerun commands for all direct source assays
(e.g. to re-basecall after a model update).

### D4 — `batch_id` lives on `assays` (nullable TEXT)

Batching in ONT (and most assay types) happens at the library-prep /
sequencing-run level, which maps to `assay`. A patient's specimens can span
batches — that's the point. `batch_id` is a FK into the new `batches` table.
Existing rows get `batch_id = NULL` — no migration needed.

**project_demo note**: the `flowcell_id` column already exists on `assays`
and carries the natural batch identifier. `batch_id` can alias `flowcell_id`
or group multiple flowcells into a named library-prep batch — whichever
granularity is relevant for QC (reagent lot contamination → prep batch;
chemistry mismatch → per-flowcell). Both are supported: `batch_id` in `batches`
is user-defined and can reference either granularity.

### D5 — `casetrack censor --batch <id>` cascades to assays and their consumers

Censoring a batch sets `qc_status = 'censored'` on every assay with that
`batch_id`. It then looks up `assay_sources` and flags:

- Mode A: any `merged_assay_id` whose sources include a censored assay.
- Mode B: any `consumer_specimen_id` whose sources include a censored assay
  (sets specimen `qc_status = 'warn'`, not `censored` — the specimen data
  may still be usable if other sources are clean; the analyst decides).

Cascade stops there — does not auto-propagate to patient unless a separate
censor is issued.

Reversible via `casetrack uncensor --batch <id>`.

---

## Schema additions

### New table: `batches`

(`batch_id` is user-defined — can be a flowcell ID, a library prep lot, a
sequencing date, or any other grouping label meaningful to the project.)

```sql
CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    prep_date  TEXT,          -- ISO date, e.g. "2026-03-15"
    reagent_lot TEXT,
    operator   TEXT,
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
```

### New column: `assays.batch_id`

```sql
ALTER TABLE assays ADD COLUMN batch_id TEXT REFERENCES batches(batch_id);
```

Added via `casetrack migrate-batch` (additive; no data loss).

### New table: `assay_sources`

Tracks lineage for both patterns — implicit merge (assay→specimen) and
explicit merge (assay→assay). Exactly one of `merged_assay_id` /
`consumer_specimen_id` is non-null per row.

```sql
CREATE TABLE IF NOT EXISTS assay_sources (
    source_assay_id      TEXT NOT NULL REFERENCES assays(assay_id),
    -- Mode A: explicit merged BAM assay
    merged_assay_id      TEXT REFERENCES assays(assay_id),
    -- Mode B: implicit merge into specimen-level analysis
    consumer_specimen_id TEXT REFERENCES specimens(specimen_id),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    PRIMARY KEY (source_assay_id, merged_assay_id, consumer_specimen_id),
    CHECK (
        (merged_assay_id IS NOT NULL) != (consumer_specimen_id IS NOT NULL)
    ),
    CHECK (merged_assay_id != source_assay_id)
);
```

**project_demo example** (Mode B — implicit merge):
```sql
INSERT INTO assay_sources (source_assay_id, consumer_specimen_id)
VALUES ('s_demo_6_1_1_1_1_1', 'p_demo_6_tumor'),
       ('s_demo_6_1_2_1_1_1', 'p_demo_6_tumor');
```

---

## New commands

### `casetrack add-batch`

```
casetrack add-batch --batch-id BATCH_B \
    --meta 'prep_date=2026-03-15,reagent_lot=RL-4422,operator=jdoe' \
    --project-dir /path/to/proj
```

Creates a row in `batches`. Idempotent — if the `batch_id` already exists,
updates the metadata fields that are provided.

### `casetrack link-sources`

Links source assays to their downstream consumer — either an explicit merged
assay (Mode A) or a specimen (Mode B, the project_demo pattern).

```bash
# Mode A — explicit merged BAM assay
casetrack link-sources \
    --sources    s_demo_6_1_1_1_1_1,s_demo_6_1_2_1_1_1 \
    --merged-id  p_demo_6_merged \
    --project-dir /path/to/proj

# Mode B — implicit merge into specimen (no merged BAM on disk)
casetrack link-sources \
    --sources    s_demo_6_1_1_1_1_1,s_demo_6_1_2_1_1_1 \
    --specimen   p_demo_6_tumor \
    --project-dir /path/to/proj
```

Inserts rows into `assay_sources`. All IDs must exist. Idempotent.
Provenance: writes a `link_sources` action to `provenance.jsonl`.

**Bulk registration from a TSV** (for project_demo's 12 assays → 6 specimens):
```bash
casetrack link-sources --from-tsv links.tsv --project-dir /path/to/proj
# links.tsv columns: source_assay_id, consumer_specimen_id (or merged_assay_id)
```

### `casetrack censor --batch <id>`

```
casetrack censor --batch BATCH_B \
    --reason "reagent lot RL-4422 confirmed contaminated" \
    --project-dir /path/to/proj
```

1. Censors every assay with `batch_id = BATCH_B` (same `qc_events` row as
   `casetrack censor --level assay --id <...>`).
2. Identifies all merged assays whose *direct* sources include at least one
   censored-batch assay, and censors those too with
   `reason = "source assay <id> in censored batch BATCH_B"`.
3. Returns a summary: N assays censored + M derived merged assays flagged.

### `casetrack uncensor --batch <id>`

Reverses step 1 and step 2 in a single `uncensor` pass. Uses the same
append-only `qc_events` semantics as the existing `uncensor` command.

### `casetrack status` / `casetrack query` additions

```
casetrack status --project-dir proj --show-lineage
```

Shows source assays indented under merged assays in the status table.

```
casetrack query --project-dir proj \
    "SELECT a.assay_id, m.source_assay_id FROM assays a
     JOIN assay_merges m ON a.assay_id = m.merged_assay_id
     WHERE a.specimen_id = 'p_demo_6_primary'"
```

DuckDB query mode already works against any table — `assay_merges` and
`batches` are immediately queryable with no changes.

---

## `casetrack migrate-lineage` command

One-shot migration for projects that predate this proposal:

```
casetrack migrate-lineage --project-dir /path/to/proj
```

1. Adds `batch_id` column to `assays` (ALTER TABLE; safe if column exists).
2. Creates `batches` and `assay_sources` tables (safe if they exist).
3. Writes a `migrate_lineage` provenance entry.
4. Prints "Migration complete — no data changed, all new columns nullable."

Idempotent. Does NOT modify `schema_version` — additive migration only.

**project_demo quick-start after migration**:
```bash
casetrack migrate-lineage --project-dir /path/to/project_demo

# Register all 6 specimen→assay links in one pass:
cat > links.tsv <<TSV
source_assay_id,consumer_specimen_id
s_demo_1_1_1_1_1_1,p_demo_1_tumor
s_demo_1_1_2_1_1_1,p_demo_1_tumor
s_demo_2_1_1_1_1_1,p_demo_2_tumor
s_demo_2_1_2_1_1_1,p_demo_2_tumor
s_demo_3_1_1_1_1_1,p_demo_3_tumor
s_demo_3_1_2_1_1_1,p_demo_3_tumor
s_demo_4_1_1_1_1_1,p_demo_4_tumor
s_demo_4_1_2_1_1_1,p_demo_4_tumor
s_demo_5_1_1_1_1_1,p_demo_5_tumor
s_demo_5_1_2_1_1_1,p_demo_5_tumor
s_demo_6_1_1_1_1_1,p_demo_6_tumor
s_demo_6_1_2_1_1_1,p_demo_6_tumor
TSV
casetrack link-sources --from-tsv links.tsv --project-dir /path/to/project_demo
```

---

## `casetrack.toml` additions (optional)

```toml
[batches]
# Pre-declare known batch IDs for validation on append.
# If omitted, any string is accepted as batch_id.
known_ids = ["BATCH_A", "BATCH_B", "BATCH_C"]
```

---

## Interaction with existing features

| Feature | Impact |
|---|---|
| `casetrack append` | No change. `batch_id` can be included in a summary TSV column and will be appended like any other column (it's already a real column once migration runs). |
| `casetrack rerun` | `--include-sources` flag added. Without it: unchanged behavior. |
| `casetrack export` | `assay_merges` and `batches` exported as extra sheets in XLSX / extra TSVs in directory export. |
| `casetrack validate` | New invariant: every `assay_merges.source_assay_id` and `.merged_assay_id` must exist in `assays`. Every `assays.batch_id` must exist in `batches` (if `batches` is non-empty). |
| `casetrack doctor` | Reports: orphaned `assay_merges` rows, assays with non-existent `batch_id`. |
| QC autoflag (v0.4) | If a summary TSV includes a `batch_id` column, `append` writes it to `assays.batch_id`. No autoflag from batch assignment itself. |
| MCP `casetrack_query` | Immediately usable — DuckDB attaches SQLite and can join `assay_merges`. |

---

## Nextflow integration

`DORADO_BASECALLER_TRACKED` produces a per-run BAM. A downstream merge step
(e.g. `SAMTOOLS_MERGE_TRACKED`, a future wrapper) would:

1. Accept `ch_bams` — a grouped channel of source BAMs per specimen.
2. Run `samtools merge` via the stock nf-core module.
3. Call `casetrack merge-assay --merged-id <id> --sources <run1,run2>` in
   `CASETRACK_REGISTER`.

The `merge-assay` CLI call is a natural fit for the `CASETRACK_REGISTER`
process — it's the same "local executor, sqlite write" pattern.

---

## Open questions

| # | Question | Recommendation |
|---|---|---|
| O1 | Should `casetrack rerun` refuse to rerun a merged assay if any source is censored? | Warn but don't block — the user may be rerunning *because* a source was fixed. |
| O2 | Should `assays.batch_id` be required (non-null) for new assays once `batches` is populated? | No — keep nullable. Different assay types (derived analyses) may not map to a wet-lab batch. |
| O3 | Is `batches` project-scoped or global? | Project-scoped (lives in the same `casetrack.db`). Cross-project batch comparisons go through `casetrack query` + DuckDB `ATTACH`. |
| O4 | Should `casetrack add-batch` accept a TSV to bulk-load many batches at once? | Yes — `--from-tsv batches.tsv` with columns `batch_id, prep_date, reagent_lot, operator, notes`. Similarly `link-sources --from-tsv`. |
| O5 | For project_demo: should `flowcell_id` be auto-populated as `batch_id` during migration? | Opt-in: `--map-flowcell-to-batch` flag on `migrate-lineage` copies `flowcell_id` → `batch_id` for every assay that has a `flowcell_id` and no `batch_id`. Non-destructive. |

---

## Implementation order

1. `casetrack migrate-lineage` + schema DDL (`batches` + `assay_sources`) — tests first.
2. `casetrack add-batch` + `casetrack link-sources` commands (both modes A and B; `--from-tsv`).
3. `casetrack censor --batch` + cascade logic + `uncensor --batch`.
4. `casetrack rerun --include-sources`.
5. `casetrack status --show-lineage` + `validate` / `doctor` invariants.
6. Export additions (XLSX / TSV).
7. Nextflow: `SAMTOOLS_MERGE_TRACKED` wrapper (separate PR).

Each step is independently testable and mergeable. Target: steps 1–3 in v0.6.0,
steps 4–7 in v0.6.1.

---

## References

- Proposal 0002 §Q9 — `batch_id` deferred from v0.4 ("free-text-reason kind for now")
- Proposal 0004 — Nextflow integration (DORADO_BASECALLER_TRACKED, SAMTOOLS_SORT_TRACKED)
- ADR-001 — level-aware L1 wrappers (assay as the base unit)
- `VisionPhilosophy.md` — "cohorts to atoms"; batch tracking is the provenance
  chain from raw flowcell runs (atoms) to cohort-level merged analysis units
