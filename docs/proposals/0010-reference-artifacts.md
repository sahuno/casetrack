# Proposal 0010 — Reference artifacts (genomes, annotations, known-variant sets) and downstream staleness

| | |
|---|---|
| **Status** | accepted (design) — 2026-05-21 |
| **Depends on** | 0001 (hierarchy), 0002 (QC cascade), 0009 (cohort artifacts — pattern reuse) |
| **Companion** | 0011 (artifact-to-artifact lineage) — separate proposal |

## 0. Accepted decisions

These were settled during design and are not re-litigated below:

1. **Primary job is invalidation, not just provenance.** The feature must answer "the GTF / dbSNP / genome bumped — which of my outputs are now stale?". Provenance (recording what each output used) is the *mechanism* that makes invalidation possible, not the end goal.
2. **Per-file granularity.** Each reference file (`genome`, `gencode`, `dbsnp`, `cpg_islands`, …) is its own artifact with its own version. A GTF bump flags annotation outputs without touching alignments.
3. **TOML `[references]` is the canonical source.** The current/canonical version of each reference is declared in `casetrack.toml`. A version change is detected when an output's recorded `version_used` differs from the current declared `version`. Consistent with "TOML is the contract; the DB is a cached materialization."
4. **Declarative capture with auto-snapshot.** Each `[analyses.<tool>]` declares the references it consumes (`uses = [...]`); `append` auto-records the current declared version of each at production time. A per-append override exists for ad-hoc runs.
5. **Both sample-level results and cohort artifacts carry ref-staleness.** Same link mechanism for both, keyed by `(entity_level, entity_id, analysis)` or by `artifact_id`.
6. **Additive sibling tables (Approach A).** Two new tables; the three-level core (`LEVEL_ORDER`) and the 0009 tables are untouched. (§7 records the rejected alternatives.)
7. **Read-time staleness, no stored flag** — derived live, exactly like 0009. Flip a version in TOML + `schema apply` → downstream outputs read STALE; revert → fresh again.
8. **History lives in `provenance.jsonl`, not the DB.** `reference_artifacts` holds only *current*; the audit of when a reference moved is reconstructable from provenance.

## 1. Summary

casetrack tracks two output shapes today: single-owner (the hierarchy) and many-producers-to-one-output (0009 cohort artifacts). Both derive staleness by cascading *up* from censored samples. This proposal adds the mirror image: **upstream reference artifacts** (genome, annotation, known-variant sets, repeats, intervals) whose version changes cascade *down* to invalidate the outputs that consumed them.

Two additive sibling tables — `reference_artifacts` (the canonical set, mirrored from a new TOML `[references]` block) and `reference_usage` (the edge: which output used which reference at which version) — plus an extension to `[analyses.<tool>]` (`uses = [...]`). Staleness is derived at read time: an output is **ref-stale** when any reference it recorded no longer matches the current declared version.

## 2. Motivation

In practice the most common reproducibility/invalidation event is an *upstream* change, not a sample being censored:

- A genome build is reprocessed (`hg38_v0 → hg38_v1`), or you switch a cohort from `GRCh37` to `hg38`.
- `databases_config.yaml` swaps `gencode.v47 → v48`, or dbSNP/gnomAD is bumped.
- A panel-of-normals borrowed from another cohort is regenerated.

Today the genome build is a free-text column on entities. casetrack cannot answer "dbSNP bumped — which of my 240 VCFs are now stale?" because (a) there is no first-class, versioned reference object and (b) nothing records which reference version each output consumed. This proposal closes both gaps with the same cascade machinery 0009 already proved out, pointed in the opposite direction.

## 3. Interim pattern (what to do until this ships)

Encode the reference version into the analysis `column_prefix` or a result column (e.g. a `{prefix}_genome_version` column emitted by the summarizer), and grep/query for mismatches by hand. This is the manual analog of `reference_usage`; it does not cascade and must be audited per-analysis, but it is forward-compatible — those columns can seed `reference_usage` on migration.

## 4. Goals

- A versioned, per-file reference registry declared in TOML and materialized to the DB.
- Automatic capture of which reference version each output consumed, declared once per analysis.
- Read-time ref-staleness for both sample-level analysis results and cohort artifacts, with a named reason.
- Surfacing in every existing read path (`status`, `query`, `export`, `dashboard`, MCP, `validate`).
- Zero change to the three-level core and the 0009 tables; additive migration for existing projects.

## 5. Non-goals (first cut)

- **Checksum-drift detection.** Staleness keys on the `version` string only. Bumping a file's content without bumping its `version` fires no staleness. A future `doctor --references` can compare the stored `checksum` against the file. Documented limitation.
- **Full version-history table.** Rejected (Approach B, §7.2). History lives in `provenance.jsonl`.
- **Auto-regeneration** of stale outputs — flagged, never auto-fixed (same posture as 0009).
- **Artifact-to-artifact lineage** — a reference derived from other artifacts, or multi-hop derivation DAGs. That is proposal 0011.
- **Path-existence / version-match validation** at registration — a future `doctor` check.
- **Sub-assay (per-region) reference granularity.**

## 6. Design (Approach A — additive sibling tables)

### 6.1 Schema

**TOML — new `[references]` block** (the canonical/current set, the source of truth):

```toml
[references.genome]
path     = "/data1/greenbab/database/hg38/v0/Homo_sapiens_assembly38.fasta"
version  = "hg38_v0"          # version identity; changing it signals "ref changed"
kind     = "genome"           # genome | annotation | known_variants | repeats | intervals | other
checksum = "sha256:…"         # optional

[references.gencode]
path = "/data1/greenbab/database/gencode_annotations/hg38/gencode.v47.annotation.gtf.gz"
version = "gencode_v47"
kind = "annotation"

[references.dbsnp]
path = ".../dbsnp_b156.vcf.gz"
version = "dbsnp_b156"
kind = "known_variants"
```

**`[analyses.<tool>]` gains a `uses` key** naming the `ref_key`s it consumes:

```toml
[analyses.modkit_pileup]
level = "specimen"
column_prefix = "modkit"
uses = ["genome", "cpg_islands"]

[analyses.clair3]
level = "specimen"
column_prefix = "clair3"
uses = ["genome", "dbsnp"]
```

**Two additive tables, materialized from TOML on `schema apply`:**

```sql
-- canonical "current" set, synced from [references] on `schema apply`
CREATE TABLE reference_artifacts (
    ref_key    TEXT PRIMARY KEY,    -- "genome", "gencode", "dbsnp"
    path       TEXT NOT NULL,
    version    TEXT NOT NULL,       -- current canonical version
    kind       TEXT,                -- genome|annotation|known_variants|repeats|intervals|other
    checksum   TEXT,                -- optional
    updated_at TEXT NOT NULL
);

-- the edge: which output consumed which ref, at which version (snapshotted at append)
CREATE TABLE reference_usage (
    usage_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    scope          TEXT NOT NULL,   -- 'analysis' | 'cohort'
    entity_level   TEXT,            -- patient|specimen|assay   (scope='analysis')
    entity_id      TEXT,            --                          (scope='analysis')
    analysis       TEXT,            -- analysis name            (scope='analysis')
    artifact_id    INTEGER,         -- cohort_artifacts.artifact_id (scope='cohort')
    ref_key        TEXT NOT NULL,   -- → reference_artifacts.ref_key (logical)
    version_used   TEXT NOT NULL,   -- snapshot of canonical version at production time
    recorded_at    TEXT NOT NULL,
    transaction_id TEXT,
    FOREIGN KEY (artifact_id) REFERENCES cohort_artifacts(artifact_id) ON DELETE CASCADE
);

-- idempotency (partial unique indexes):
CREATE UNIQUE INDEX idx_refusage_analysis
    ON reference_usage(entity_level, entity_id, analysis, ref_key) WHERE scope = 'analysis';
CREATE UNIQUE INDEX idx_refusage_cohort
    ON reference_usage(artifact_id, ref_key) WHERE scope = 'cohort';
CREATE INDEX idx_refusage_refkey ON reference_usage(ref_key);
```

Deliberate choices:
- **No polymorphic FK on `(entity_level, entity_id)`** — same as `qc_events`, which keys on `(level, entity_id)` without an FK. App-level integrity; `validate` catches orphans.
- **`reference_artifacts` is mirrored from TOML, never hand-edited.** TOML stays the contract; the table is the cached materialization, exactly like the column schema.
- **No FK on `ref_key`** — a usage row can outlive the removal of a reference from TOML; that case is *meaningful* (it makes the output STALE-removed, §6.2), so we do not cascade-delete it.

### 6.2 Staleness (cascade interaction)

**Read-time derived, no stored flag.** The rule: an output is **ref-stale** when any of its `reference_usage` rows has `version_used` ≠ the current `reference_artifacts.version` for that `ref_key`.

**Three states — `fresh` / `STALE` / `untracked`:**

| State | Meaning |
|---|---|
| `fresh` | has usage rows, all `version_used` == current |
| `STALE` | ≥1 usage row where `version_used` ≠ current, **or** the `ref_key` was removed from TOML |
| `untracked` | no usage rows at all — **no claim is made** |

The `untracked` state is deliberate and distinct from `fresh`: an analysis that never declared `uses` (or predates this feature) must not masquerade as "verified against current references."

**Staleness carries a reason.** A removed `ref_key` → `STALE (reference removed: dbsnp)`; a version mismatch → `STALE (genome: hg38_v0 → hg38_v1)`.

**Orthogonal to 0009 — reported separately.** A cohort artifact now has two independent staleness causes that never collapse into one flag:
- **input-stale** (0009): a contributing assay is censored / consent-revoked.
- **ref-stale** (0010): a reference version changed.

An artifact can be `STALE(inputs)`, `STALE(refs)`, both, or neither. Sample-level analysis results get only the ref-stale check (they have no 0009 inputs).

**History in provenance.** When `schema apply` changes a reference's `version` or `path`, it writes a provenance entry — `action='reference_version_change'`, `{ref_key, old_version, new_version, old_path?, new_path?}`. `reference_artifacts` holds only current state.

### 6.3 CLI

- **`casetrack schema apply`** *(extended)* — syncs `reference_artifacts` from `[references]`; emits `reference_version_change` provenance on a version/path move. The single action that flips outputs stale.
- **`casetrack migrate-references --project-dir . [--dry-run] [--bootstrap-from FILE]`** *(new)* — additive table creation (mirrors `migrate-cohort`); `init` creates the tables by default going forward. `--bootstrap-from databases_config.yaml` seeds a starter `[references]` block (user reviews/commits the TOML); off the hard path.
- **`casetrack append`** *(extended)* — auto-snapshots `reference_usage` rows from `[analyses.<tool>].uses`. Overrides: `--uses-references genome,dbsnp` (explicit), `--no-track-references` (opt out).
- **`casetrack append-cohort`** *(extended)* — `--uses-references genome,dbsnp` (primary path, since cohort analyses aren't always in `[analyses]`).
- **`casetrack references --project-dir . [--fmt table|tsv|json] [--stale-only]`** *(new)* — lists the canonical set with per-ref `fresh`/`STALE`/`untracked` tallies; `--stale-only` drills into stale outputs (which output, which ref, `used → current`).

### 6.4 Read-path integration

- **`casetrack status`** — a "References" section (canonical set + tallies); ref-stale outputs flagged inline.
- **`casetrack query`** — a `_reference_usage` DuckDB view with derived `current_version` / `is_stale`; the `_cohort_artifacts` view gains a `ref_stale` column **alongside** its existing input-`stale` column.
- **`casetrack export --include-references`** — emits `reference_artifacts` + `reference_usage` (with derived staleness); auto-enabled for XLSX.
- **`casetrack dashboard`** — a "References" section + ref-stale badges on outputs.
- **MCP** — a `casetrack_references` tool (companion to the CLI), surfacing canonical set + stale outputs.
- **`casetrack validate`** — new invariants: every `reference_usage.ref_key` resolves to a `reference_artifacts` row (else flag orphan); `scope`-shape consistency.

### 6.5 Nextflow

- **`CASETRACK_REGISTER`** — the `append` it runs auto-captures usage from `[analyses.<tool>].uses`, so tracked sample-level subworkflows gain ref-staleness for free once the TOML declares `uses`. Optional `--uses-references` passthrough param for ad-hoc cases.
- **`casetrack_append_cohort`** — gains an optional `uses_references` input following the same `[]`-means-none pattern as the stats slot (no placeholder file; flag dropped when empty).

## 7. Design alternatives considered

### 7.1 Approach A — additive sibling tables, TOML is the contract. **Chosen** (§6).

### 7.2 Approach B — full version history in the DB. **Rejected.**
A `reference_versions(version_id, ref_key, version, checksum, is_current)` table tracking every version ever seen, with `reference_usage` FK to a `version_id`. Gives a complete audit of reference evolution and "everything that ever used dbSNP b155." Rejected because it duplicates TOML as a second source of truth (needs sync logic + an `is_current` flag), and invalidation only needs current-vs-used — full history is YAGNI for the stated goal. The audit it would provide is already in `provenance.jsonl` via `reference_version_change`.

### 7.3 Approach C — denormalize onto existing rows. **Rejected.**
A `{analysis}_ref_versions` JSON blob on entity rows + a column on `cohort_artifacts`, no new tables. Cheapest to build, but the lineage is unqueryable ("what consumes the GTF?" impossible), and it breaks the normalization the project deliberately maintains — re-creating the denormalization 0009 explicitly rejected.

## 8. Open questions / risks

- **Orphan usage rows.** `entity_id` has no FK, so deleting an entity can orphan `reference_usage` rows — same risk profile as `qc_events`; `validate` flags it. Entities are not normally deleted.
- **User-asserted version.** Bumping a file but not its `version` string fires no staleness (the §5 checksum non-goal). Documented limitation until `doctor --references` lands.
- **Per-analysis, not per-output-instance, `uses`.** All outputs of analysis X are assumed to use X's declared refs. A run with a non-default reference needs the `--uses-references` override; the declared default covers the common path.
- **Ad-hoc appends.** An analysis not in `[analyses]` (bare `append`) relies on `--uses-references`; if forgotten, the output is `untracked` (honest, not wrong).

## 9. Next actions

1. Schema module: DDL + idempotent `ensure_reference_schema` (mirrors `casetrack_qc/cohort_artifacts.py`); TOML `[references]` + `[analyses].uses` parse/validation.
2. `schema apply` sync + `reference_version_change` provenance.
3. `append` / `append-cohort` capture (auto + override + opt-out).
4. Read-time staleness derivation (three-state + reason) and the `_reference_usage` view; `_cohort_artifacts.ref_stale` column.
5. CLI: `references`, `migrate-references`; read-path hooks (`status`, `export`, `dashboard`, `validate`); MCP `casetrack_references`.
6. Nextflow: `CASETRACK_REGISTER` passthrough; `casetrack_append_cohort` `uses_references`.
7. Tests (§10); docs (README, CLAUDE.md, casetrack skill).

## 10. Testing

- DDL idempotency; `[references]` parse + validation (`version` required, `kind` enum, `ref_key` identifier).
- `schema apply` sync: new ref; version change emits provenance; path change.
- Capture: auto from `uses`; `--uses-references` override; `--no-track-references`; `append-cohort uses_references`.
- Staleness: three-state `fresh`/`STALE`/`untracked`; removed `ref_key`; **orthogonality with 0009** (cohort artifact `STALE(refs)` while inputs fresh, and the converse).
- Read paths: `references` CLI (table/tsv/json + `--stale-only`); `status` section; `_reference_usage` view + `_cohort_artifacts.ref_stale`; `export --include-references`; `validate` invariants; MCP tool.
- Nextflow: `append-cohort uses_references []` drops the flag; `CASETRACK_REGISTER` passthrough.
