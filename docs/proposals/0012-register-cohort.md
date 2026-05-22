# Proposal 0012 — `register-cohort`: one-shot cohort registration from a sample sheet

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | accepted (implemented) — 2026-05-22 |
| **Depends on** | 0001 (hierarchy + SQLite backend), 0003 (init scaffold) |
| **Relates to** | `migrate` (flat→project) — `register-cohort` is its successor for *new* projects once flat mode is removed at v1.0 |

## 0. Accepted decisions

Settled during design; not re-litigated below:

1. **One new focused command, `register-cohort`.** Not an overload of `add-metadata` (which is strictly single-level) and not a rework of `migrate` (which carries v0.2 baggage and is slated for v1.0 removal). Mirrors the 0009/0010/0011 habit of adding one command with a single clear job.
2. **Schema-native wide sample sheet.** Column *names* match the schema's declared level columns; the command routes each column to its level by reading `casetrack.toml`. No mapping flags (unlike `migrate`'s `--patient-col …`). Self-describing and stays correct as the schema evolves.
3. **Full chain per row.** Every row must populate `patient_id` + `specimen_id` + `assay_id` (one row per assay, with its lineage). Partial/sparse rows (a banked patient with no assay yet) are a non-goal — those keep using `register` / `add-metadata`.
4. **Create-by-default; `--dry-run` is the guard.** Registering new entities *is* the job, so it is not gated behind `--allow-new --yes` (that double-opt-in convention is for commands where new IDs are the exception). Existing rows fill-only-update; `--overwrite` replaces non-null attrs; `--dry-run` previews counts and writes nothing.
5. **Shared insert engine.** Extract `add-metadata`'s per-level "validate + upsert a dataframe" core into one internal helper that both `add-metadata` (one call) and `register-cohort` (three calls, FK order) use. One tested upsert path, no duplication.
6. **All-or-nothing.** The three level-loads run in a single `begin_immediate` transaction; any error rolls back the whole load. Validation is entirely pre-write.

## 1. Summary

casetrack has no single command to populate the `patients → specimens → assays` hierarchy from one file. The bulk path today is three `add-metadata --allow-new --yes` calls (one TSV per level, in FK order); the `giab_chr21` example sidesteps even that with a hand-written `bootstrap.py` that loops `register`. Both are workarounds for a missing primitive.

`register-cohort` is that primitive: it takes one **schema-native wide sample sheet** (one row per assay, full lineage), explodes it into the three normalized tables — deduping patients and specimens across rows — and loads them in FK order in a single transaction. Column→level routing is read from the schema; new entities are created by default; `--dry-run` previews.

## 2. Motivation

- The lab already maintains wide sample sheets (the global convention `patient, sample, condition, assay, path, genome`; the giab demo's `sample_sheet.tsv`). The normalized backend forces them to be hand-split into three files or loaded by glue code.
- `bootstrap.py` in `examples/giab_chr21/` is direct evidence of the gap: it reads one `sample_sheet.tsv`, dedups patients, derives specimens, and loops `casetrack register` row by row, treating UNIQUE violations as idempotent no-ops. That logic belongs in the tool, tested, not re-implemented per project.
- Once flat mode is removed at v1.0, `migrate` (the flat-TSV importer) goes with it. `register-cohort` is what remains for "load a cohort from a sheet" — built for the normalized world from the start, with no `--manifest`/classification baggage.

## 3. Interim pattern (what to do until this ships)

The three-call bulk load, in FK order:

```bash
casetrack add-metadata --project-dir . --level patient  --metadata patients.tsv  --allow-new --yes
casetrack add-metadata --project-dir . --level specimen --metadata specimens.tsv --allow-new --yes
casetrack add-metadata --project-dir . --level assay    --metadata assays.tsv    --allow-new --yes
```

or a per-project `register` loop (the `bootstrap.py` shape). Both are documented stopgaps, not the target.

## 4. Goals

- A `register-cohort` command that loads patients + specimens + assays from one wide sheet in a single transaction.
- Schema-driven column→level routing (no mapping flags).
- Cross-row dedup of patient/specimen rows; FK-ordered insert.
- Create-by-default with fill-only updates, `--overwrite`, and `--dry-run`.
- Comprehensive pre-write validation (required/undeclared columns, full-chain, intra-sheet integrity).
- A shared upsert engine factored out of `add-metadata`, leaving its behavior unchanged.
- Provenance + a human summary.

## 5. Non-goals (first cut)

- **Partial/sparse rows** — full chain per row only (§0.3). Banked-but-unsequenced patients/specimens stay with `register`/`add-metadata`.
- **Cohort *artifacts*** (joint VCFs / PoNs) — that is 0009 `append-cohort`. The `cohort` *grouping attribute* is just an ordinary patient column here.
- **Special handling of `path` / `genome`** — they are ordinary declared columns if the schema has them; `register-cohort` assigns no semantics.
- **Replacing `migrate` now** — `migrate` (flat→project) coexists until v1.0 removes flat mode; `register-cohort` is its successor for new projects, not a drop-in for the v0.2 path.
- **Auto-creating schema columns** — every sheet column must already be declared (`schema apply` first), same as `add-metadata`.

## 6. Design

### 6.1 CLI

```bash
casetrack register-cohort --project-dir . --samplesheet cohort.tsv [--overwrite] [--dry-run]
#   --project <id>     accepted as an alternative to --project-dir (registry lookup), like other commands
#   --overwrite        replace existing non-null attribute cells (default: fill-only)
#   --dry-run          print the per-level new/existing plan; write nothing
```

No `--allow-new`/`--yes` (create-by-default, §0.4). No `--level` (the command spans all three).

### 6.2 Column → level routing

On invocation, read `[levels.<lvl>.columns]` from `casetrack.toml`. For each column in the sheet header:
- It must be declared at exactly one level → route there. The key columns (`patient_id`, `specimen_id`, `assay_id`) and the FK columns (`patient_id` on specimen, `specimen_id` on assay) are known from each level's `key` / `parent_key`; they are projected into the relevant frames rather than treated as routable attributes.
- A column declared at no level → **error** (undeclared, matches `add-metadata`).
- A non-key attribute somehow declared at two levels → **error** (ambiguous).

### 6.3 Explode

From the wide frame, build three per-level frames:
- **patient**: distinct `patient_id` + patient attribute columns present in the sheet.
- **specimen**: distinct `specimen_id` + `patient_id` (FK) + specimen attrs.
- **assay**: distinct `assay_id` + `specimen_id` (FK) + assay attrs.

"Distinct" is by the level's key; identical duplicate rows collapse silently (§6.4 handles *conflicting* duplicates as errors).

### 6.4 Validation (pre-write; any failure aborts the whole load)

1. **Header**: every level's required columns present; no undeclared columns (§6.2).
2. **Full chain**: every row has non-empty `patient_id`, `specimen_id`, `assay_id` and every required attribute for all three levels.
3. **Intra-sheet integrity**:
   - a `specimen_id` associated with more than one `patient_id` → error (a specimen has one parent);
   - a duplicate `assay_id` (two non-identical rows sharing an `assay_id`) → error;
   - an entity key (`patient_id` / `specimen_id` / `assay_id`) carrying **conflicting attribute values** across rows → error (e.g. `P01.cohort` differs between rows). Identical repeats are fine (they dedup).

Validation runs on the parsed frame before any DB write, so a bad sheet never half-loads.

### 6.5 Load

```
with begin_immediate(conn):
    _upsert_level(conn, "patient",  patient_frame,  allow_new=True, overwrite=args.overwrite, txn_id=…)
    _upsert_level(conn, "specimen", specimen_frame, allow_new=True, overwrite=args.overwrite, txn_id=…)
    _upsert_level(conn, "assay",    assay_frame,    allow_new=True, overwrite=args.overwrite, txn_id=…)
```

`_upsert_level` is the engine extracted from `cmd_add_metadata_project` (§6.6). Strict FK is satisfied by ordering. On any exception the transaction rolls back — nothing is partially loaded. `--dry-run` runs §6.2–6.4 + a per-level new/existing count (existing = key already in the table) and returns before the `with` block.

### 6.6 Shared upsert engine (refactor)

`cmd_add_metadata_project` today inlines: validate columns against the schema, split keys into existing-vs-new, `UPDATE` existing (fill-only or `--overwrite`), `INSERT` new (when `allow_new`), route missing-parent errors. Extract that into:

```python
def _upsert_level(conn, *, level, frame, schema, allow_new, overwrite, transaction_id) -> dict:
    """Validate `frame` against the level's schema and upsert it. Returns
    {'inserted': n, 'updated': m, 'skipped': k}. Caller owns the transaction."""
```

`cmd_add_metadata_project` becomes a thin caller (`allow_new=args.allow_new`); `register-cohort` calls it three times with `allow_new=True`. The extraction must be behavior-preserving — the existing `add-metadata` test suite is the regression guard.

### 6.7 Provenance + output

- Provenance: one entry `action="register_cohort"`, `{samplesheet, checksum, counts: {patient, specimen, assay: {inserted, updated}}, transaction_id}`.
- stdout: `register-cohort: patients +2 (~0), specimens +3 (~0), assays +3 (~0)` (`+` = inserted, `~` = updated). `--dry-run` prefixes `[dry-run]` and shows new-vs-existing without writing.

## 7. Design alternatives considered

### 7.1 New `register-cohort` command, schema-native sheet. **Chosen** (§6).

### 7.2 Overload `add-metadata --sample-sheet`. **Rejected.** `add-metadata` is contractually single-level (`--level` required); a multi-level mode forks its validation and muddies its argument surface. A separate command keeps each one's contract clean.

### 7.3 Generalize `migrate` into the importer. **Rejected.** `migrate` is the flat→project path and carries v0.2 classification flags + the deprecation surface; it is removed/reworked at v1.0. Entangling a forward-looking command with deprecated code is backwards. `register-cohort` is the clean successor.

### 7.4 Explicit `--patient-col/--specimen-col/--assay-col` mapping (migrate-style). **Rejected for the first cut.** Schema-native names make the sheet self-describing and the command flagless; the mapping approach is verbose and error-prone. If a future need arises for arbitrary column names, a `--map` option is an additive extension.

## 8. Open questions / risks

- **Refactor blast radius.** Extracting `_upsert_level` touches `cmd_add_metadata_project`, a shipped path. Mitigation: behavior-preserving extraction gated by the existing `add-metadata` test suite; no signature change to the command.
- **Conflicting-attribute strictness.** Erroring on the same key with different attribute values (rather than last-wins) is the safe default; if real sheets legitimately carry per-row variation at a parent level, a future `--last-wins` could relax it. Documented as strict for now.
- **Key column naming.** Assumes the sheet uses each level's declared `key` column name (`patient_id`, etc.). A sheet using the lab's `patient`/`sample` names needs a header rename first; documented, and the §7.4 `--map` extension is the escape hatch if it becomes common.
- **Large sheets.** Pandas-based explode + per-level upsert is fine for cohort scale (hundreds–thousands of rows). No streaming needed.

## 9. Next actions

1. Extract `_upsert_level` from `cmd_add_metadata_project` (behavior-preserving; add-metadata tests stay green).
2. `register-cohort` parse + schema-driven column routing (§6.2) + explode (§6.3).
3. Validation (§6.4) — pre-write, abort-all.
4. Load (§6.5) in one transaction; `--dry-run`; provenance + summary (§6.7).
5. Argparse wiring + dispatch.
6. Tests (§10); docs (README, CLAUDE.md, casetrack skill, CHANGELOG); version bump.

## 10. Testing

- `_upsert_level` extraction: existing `add-metadata` suite unchanged (regression guard); plus direct unit tests for inserted/updated/skipped counts.
- Column routing: each column routed to its declaring level; undeclared column → error; ambiguous (two-level) attribute → error.
- Explode + dedup: 2 patients / 3 specimens / 3 assays from a 3-row sheet; cross-row patient/specimen dedup.
- Happy-path load → correct counts + a join across the three tables; provenance entry shape.
- Re-run idempotency: same sheet again → 0 inserted (fill-only); `--overwrite` updates changed attrs.
- `--dry-run`: prints the new/existing plan, writes nothing (table counts unchanged).
- Validation errors (each aborts with no partial write): missing required column, undeclared column, broken chain (blank key / missing required attr), `specimen_id`→2 `patient_id`s, duplicate `assay_id`, conflicting attribute across rows.
- Transaction: a mid-load failure (e.g. forced FK/IntegrityError on the assay frame) rolls back patients + specimens too.
- CLI: `--project-dir` and `--project <id>` both resolve; clean exit codes on validation errors (exit 2), not tracebacks.
