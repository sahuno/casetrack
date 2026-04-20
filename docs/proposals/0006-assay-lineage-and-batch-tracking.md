# Proposal 0006 — Assay lineage (`assay_merges`) and batch tracking (`batch_id`)

**Status**: draft (2026-04-20)
**Target release**: casetrack v0.6.0
**Breaking**: no (additive schema; all new columns nullable; new tables independent of existing FK graph)
**Author**: Samuel Ahuno

---

## Motivation

ONT sequencing routinely produces multiple flowcell runs per sample. In the
current three-level hierarchy (`patients → specimens → assays`) both raw runs
*and* the merged BAM produced from them are represented as assays — but the
relationship between them is invisible to casetrack. Concretely:

```
p17424_6_run1  →  assay (flowcell 1 BAM)  ─┐
                                             ├─ samtools merge → p17424_6 → modkit pileup
p17424_6_run2  →  assay (flowcell 2 BAM)  ─┘
```

Today `casetrack` can track `p17424_6` and both run assays independently,
but cannot answer:

- "Which flowcells contributed to p17424_6?"
- "Did run1 and run2 come from the same sequencing batch / reagent lot?"
- "If batch B is contaminated, which merged assays are affected?"

This proposal adds two orthogonal, non-breaking features that address this:

1. **Assay lineage** — `assay_merges` join table recording `source → merged`
   relationships, plus a `casetrack merge-assay` command to register merges.
2. **Batch grouping** — `batch_id` nullable column on `assays` + a `batches`
   metadata table, plus `casetrack add-batch` / `casetrack censor --batch`.

These features are independent but ship together because they interact at the
QC layer (a censored batch cascades to merged assays that consumed its sources)
and at `casetrack rerun` (merges may need to be re-done after a batch is
remediated).

---

## Design decisions (locked)

### D1 — Merged assay stays in the `assays` table; no 4th hierarchy level

A merged BAM is still a specimen's assay. Adding a 4th level (`runs`) would
be breaking (schema migration, all CLI flags, all queries) and is not justified
biologically — the merge is a *processing* step, not a new entity type.

The `assay_merges(merged_assay_id, source_assay_id)` join table records the
lineage additively. Both the merged assay and its source run assays live in
`assays` under the same `specimen_id`.

### D2 — `assay_merges` is many-to-many and supports recursive merges

A merged assay can itself be a source for a further merge. `casetrack status`
shows direct sources only; `--deep` shows transitive provenance.

### D3 — `casetrack rerun` targets the merged assay, not its sources

If `p17424_6.modkit_done` is null, `rerun --analysis modkit` emits the sbatch
for `p17424_6`. Source run assays are independent entities. Add
`--include-sources` to also emit rerun commands for all direct source assays
(e.g. to re-basecall after a model update).

### D4 — `batch_id` lives on `assays` (nullable TEXT)

Batching in ONT (and most assay types) happens at the library-prep /
sequencing-run level, which maps to `assay`. A patient's specimens can span
batches — that's the point. `batch_id` is a FK into the new `batches` table.
Existing rows get `batch_id = NULL` — no migration needed.

### D5 — `casetrack censor --batch <id>` cascades to assays

Censoring a batch sets `qc_status = 'censored'` on every assay in that batch
(same cascade semantics as `casetrack censor --level assay`). It also flags
any *merged* assay whose direct sources include a censored-batch assay
(cascade stops at merged assays — does not propagate further up to specimen/
patient unless a separate censor is issued).

Reversible via `casetrack uncensor --batch <id>`.

---

## Schema additions

### New table: `batches`

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

### New table: `assay_merges`

```sql
CREATE TABLE IF NOT EXISTS assay_merges (
    merged_assay_id TEXT NOT NULL REFERENCES assays(assay_id),
    source_assay_id TEXT NOT NULL REFERENCES assays(assay_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    PRIMARY KEY (merged_assay_id, source_assay_id),
    CHECK (merged_assay_id != source_assay_id)
);
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

### `casetrack merge-assay`

```
casetrack merge-assay \
    --merged-id  p17424_6 \
    --sources    p17424_6_run1,p17424_6_run2 \
    --project-dir /path/to/proj
```

Inserts rows into `assay_merges`. All IDs must already exist in `assays`.
Idempotent — re-registering the same pair is a no-op.

Provenance: writes a `merge_assay` action to `provenance.jsonl`.

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
     WHERE a.specimen_id = 'p17424_6_primary'"
```

DuckDB query mode already works against any table — `assay_merges` and
`batches` are immediately queryable with no changes.

---

## `casetrack migrate-batch` command

One-shot migration for projects that predate this proposal:

```
casetrack migrate-batch --project-dir /path/to/proj
```

1. Adds `batch_id` column to `assays` (ALTER TABLE; safe if column exists).
2. Creates `batches` and `assay_merges` tables (safe if they exist).
3. Writes a `migrate_batch` provenance entry.
4. Prints "Migration complete — no data changed, all new columns nullable."

Idempotent. Does NOT modify `schema_version` — this is an additive migration,
not a schema contract change.

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
| O4 | Should `casetrack add-batch` accept a TSV to bulk-load many batches at once? | Yes — `--from-tsv batches.tsv` with columns `batch_id, prep_date, reagent_lot, operator, notes`. |

---

## Implementation order

1. `casetrack migrate-batch` + schema DDL (tests first).
2. `casetrack add-batch` + `casetrack merge-assay` commands.
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
