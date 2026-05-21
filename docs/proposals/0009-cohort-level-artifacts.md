# Proposal 0009 — Cohort-level artifacts (joint VCFs, PoNs, cohort matrices)

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | **Draft** |
| **Date** | 2026-05-20 |
| **Target release** | TBD (numbering unsettled across docs — confirm before implementation) |
| **Depends on** | Proposal 0001 (SQLite backend), Proposal 0002 (QC events + cascade), Proposal 0004 (Nextflow integration) |
| **Target HPC** | IRIS @ MSKCC (WekaFS, SLURM, Apptainer) |

## 0. Accepted decisions

| # | Question | Accepted answer |
|---|---|---|
| 1 | Is a cohort-level result a new hierarchy level? | **No.** Reject the 4th-level design (Option B, §7). A cohort artifact is derived, many-to-many, and dynamically-membered; a hierarchy level is biological, single-parent, and static. Wrong abstraction first, expensive refactor second. |
| 2 | Where do cohort-level results live? | **Additive sibling table** `cohort_artifacts` keyed by `(analysis, run_tag)`, plus a `cohort_artifact_inputs` join table to participating `assay_id`s. Mirrors the `qc_events` additive-sibling pattern (Proposal 0002). |
| 3 | Does the three-level core change? | **No.** `LEVEL_ORDER = ("patient", "specimen", "assay")` is untouched. Per-sample analyses keep the existing append path. |
| 4 | How does the QC cascade reach a cohort artifact? | Via `cohort_artifact_inputs`. If any input assay is censored / consent-revoked, the artifact is flagged **stale** at read time. Append-only; the artifact row is never deleted. |
| 5 | Interim modeling before this ships | `level = "patient"` denormalization (the `cohort_dmr` template, §3). Documented as a stopgap, not the target. |

## 1. Summary

Add a first-class home for **analysis outputs that span many samples** — a joint-genotyped
multi-sample VCF, a panel-of-normals, a cohort PCA / relatedness matrix, a cohort-wide DMR table.
These are *derived artifacts* with *many-to-many* lineage to the assays that produced them. They do
not fit casetrack's three-level biological hierarchy and are currently modeled by denormalizing
onto patient rows, which loses single-artifact identity and input provenance.

The design is an **additive sibling table** (`cohort_artifacts` + `cohort_artifact_inputs`), not a
new hierarchy level. §7 records why the 4th-level alternative was rejected — this is the load-bearing
part of the proposal and the reason it exists before any code is written.

## 2. Motivation

casetrack hardcodes three levels — patient → specimen → assay (`casetrack.py:80`) — and every
analysis must attach to one of them (`casetrack.py:517`). This is correct for per-sample results
(one bedMethyl per assay, one gVCF per assay). It has no answer for **one artifact derived from N
samples**.

Concrete driving case — **GATK germline joint genotyping**:

- **Phase 1** — `HaplotypeCaller -ERC GVCF`: one gVCF per assay. Fits `level = "assay"` cleanly.
- **Phase 2** — `GenomicsDBImport` → `GenotypeGVCFs`: **one** multi-sample pVCF for the whole cohort.
  No natural home.

Today Phase 2 is modeled by attaching at `level = "patient"` with a summary TSV of N patient rows,
each carrying the **same** `joint_vcf_path` repeated plus that sample's extracted slice
(`bcftools stats -s SAMPLE`). This works but leaves three gaps:

1. **The artifact is recorded N times, not once.** A v2 joint call (one more sample) forces re-pointing
   every patient row with `--overwrite`. No single row represents "cohort147 joint call v2".
2. **No input→output lineage.** The denormalized columns don't capture which assays fed the call, so
   the QC cascade (Proposal 0002) cannot mark the joint VCF stale when an input assay is censored.
3. **Non-per-sample outputs don't fit at all.** A PoN, a PCA, a relatedness matrix have no per-patient
   slice — the denormalization degenerates to pure repetition.

## 3. Interim pattern (what to do until this ships)

Use the `cohort_dmr` template shape (`casetrack.py:288`) — attach the cohort analysis at the highest
existing level and denormalize:

```toml
[analyses.joint_genotype]
level         = "patient"
column_prefix = "joint"
summary_tsv   = "joint_genotype_summary.tsv"   # N patient rows; joint_vcf_path repeated
```

This is a **documented stopgap**, not the target design. It is acceptable while the cohort is static
and small; it does not survive re-genotyping or input censoring cleanly.

## 4. Goals

- A `cohort_artifacts` table: one row per cohort-level output, identified by `(analysis, run_tag)`.
- A `cohort_artifact_inputs` join table: many-to-many lineage from artifact → contributing `assay_id`.
- `casetrack append-cohort` (or an extension of `append`) to register an artifact + its inputs in one
  transaction, logging to `provenance.jsonl`.
- Read-time **staleness flagging**: an artifact whose inputs include a censored / consent-revoked assay
  is surfaced as `stale` in `status`, `query`, `export`, `cohort`.
- A `CASETRACK_COHORT_ARTIFACT` Nextflow subworkflow mirroring the three-phase pattern.

## 5. Non-goals (first cut)

- A 4th hierarchy level (rejected — §7).
- Auto-recomputation of stale artifacts. Staleness is **flagged, not fixed** — re-running is the
  operator's call.
- Storing the artifact contents in-DB. Only path + checksum + summary stats; the file lives on WekaFS.
- Sub-cohort algebra (set operations over artifact membership). Deferred.

## 6. Design (Option A — additive sibling tables)

### 6.1 Schema

```sql
-- One row per cohort-level output.
CREATE TABLE IF NOT EXISTS cohort_artifacts (
    artifact_id   INTEGER PRIMARY KEY,
    analysis      TEXT NOT NULL,           -- e.g. "joint_genotype"
    run_tag       TEXT NOT NULL,           -- e.g. "20260520_hg38_cohort147"
    path          TEXT NOT NULL,           -- shared artifact path on WekaFS
    checksum      TEXT,                    -- sha256 of the artifact
    n_inputs      INTEGER NOT NULL,        -- number of contributing assays
    stats_json    TEXT,                    -- cohort-level summary (ti_tv, n_variants, ...)
    created_at    TEXT NOT NULL,
    UNIQUE (analysis, run_tag)
);

-- Many-to-many lineage: which assays fed which artifact.
CREATE TABLE IF NOT EXISTS cohort_artifact_inputs (
    artifact_id   INTEGER NOT NULL REFERENCES cohort_artifacts(artifact_id),
    assay_id      TEXT    NOT NULL REFERENCES assays(assay_id),
    PRIMARY KEY (artifact_id, assay_id)
);
```

`(analysis, run_tag)` is the natural key — a re-genotyping run gets a new `run_tag`, hence a new
artifact row, hence v1 and v2 coexist with distinct identity (fixes gap 1). The join table records
exactly which assays went in (fixes gap 2). Non-per-sample outputs fit with no per-patient repetition
(fixes gap 3).

### 6.2 Staleness (cascade interaction)

```sql
-- An artifact is stale if any contributing assay is currently censored or consent-revoked.
SELECT a.artifact_id, a.analysis, a.run_tag,
       COUNT(*) FILTER (WHERE q.kind IS NOT NULL) AS n_censored_inputs
FROM cohort_artifacts a
JOIN cohort_artifact_inputs ci USING (artifact_id)
LEFT JOIN <active_qc_view> q ON q.assay_id = ci.assay_id
GROUP BY a.artifact_id;
```

Staleness is **derived at read time** from the existing QC view (Proposal 0002 §4.4), so it tracks
censor / uncensor automatically and needs no separate cache. Append-only: censoring an input never
mutates or deletes the artifact row — it just changes how the artifact reads.

### 6.3 CLI sketch

```bash
casetrack append-cohort \
  --analysis joint_genotype \
  --run-tag 20260520_hg38_cohort147 \
  --path results/joint/cohort147.joint.vcf.gz \
  --inputs-from gvcf_manifest.tsv \      # one assay_id per line → cohort_artifact_inputs
  --stats joint_stats.json
```

## 7. Design alternatives considered

### 7.1 Option B — add a 4th hierarchy level (`cohort` / `project`). **Rejected.**

The intuitive move is to make "cohort" a level above patient, so a joint VCF attaches to it the way a
bedMethyl attaches to an assay. We reject this for **conceptual** reasons first, refactor cost second.

**What a "level" is in casetrack.** The three levels are biological provenance entities in a strict tree:

- **single-parent** — an assay belongs to exactly one specimen, a specimen to exactly one patient
  (`casetrack.py:438`: `expected_parents = {"patient": None, "specimen": "patient", "assay": "specimen"}`);
- **one-to-many downward** — 1 patient → N specimens → N assays;
- **static** — a row's parent never changes; it is a fact about *what the sample is*, fixed at registration.

A cohort artifact violates every one of these:

1. **Direction and cardinality are inverted.** A cohort artifact is not *below* assay — it sits *above*
   many assays, and the relationship is **many-to-many**: one joint VCF is built from N assays, and one
   assay feeds N artifacts (full-cohort call, tumor-only subcohort, PoN, a re-run with one more sample).
   A tree level is single-parent by definition and structurally cannot represent "this assay contributed
   to these four cohort outputs."
2. **Membership is dynamic and run-defined.** The set of assays in "cohort147 joint call v1" is decided
   at analysis time and changes in v2. The biological hierarchy is permanent. Encoding a run-scoped
   grouping as a structural parent conflates *what a sample is* with *which analysis batch it landed in*.
3. **Membership overlaps.** A patient simultaneously belongs to the full cohort, a sex-stratified subset,
   a sequencing batch, a tumor-only group. A parent level forces exactly one parent — you would have to
   pick one canonical cohort (wrong) or break the single-parent invariant (which *is* the level model).

**The decisive point: a 4th level buys nothing here.** Even with a `cohort` level, the thing the QC
cascade actually needs — *which assays fed this joint VCF, so it can be flagged stale when one is
censored* — is a many-to-many edge a tree level cannot store. You would **still** add a
`cohort_artifact_inputs` join table. So Option B = (wider refactor) + (Option A's join table anyway).
Option A is Option B without the refactor and without the conceptual conflation.

**The existing schema already made this call.** Cohort membership is modeled today as a *patient column*,
not a structure — e.g. the SU2C `casetrack.toml`:

```toml
[levels.patient.columns]
cohort    = { type = "TEXT" }
trio_role = { type = "TEXT", enum = ["proband", "father", "mother", "sibling", "unrelated"] }
```

The model already treats cohort/family grouping as an *attribute you filter and group by*, not a level
you nest under. A `cohort` level would directly contradict a decision the schema already encodes.

**Refactor cost (the secondary objection).** `LEVEL_ORDER` is threaded through the FK chain
(`casetrack.py:438`), path templates, the `_giab`/default TOML templates, the MCP tools, the dashboard,
and ~522 tests that assume exactly three levels. Real, but it is not the reason — a wrong abstraction is
the reason; the cost just makes the wrong design also painful.

### 7.2 When a 4th level *would* be correct (for the record)

A new level is the right tool when the new entity is *also* static, single-parent, and nests cleanly:

- **A sublevel below assay** — `lane` / `readset` (1 assay → N lanes) or `cell` for single-cell.
  Single-parent, static, downward — fits the tree. A legitimate future
  `LEVEL_ORDER = (..., "assay", "lane")`.
- **A static single-parent grouping above patient** — e.g. `family` for strict trio-only studies,
  *only if* every patient belongs to exactly one permanent group forever. Cohorts fail this because
  membership overlaps and changes.

A cohort *result* fails the test on every axis — derived, many-to-many, dynamic. Derived data with input
lineage is a sibling table (the `qc_events` pattern), not a hierarchy level.

### 7.3 Option C — keep denormalizing onto `patient`. **Rejected as the target** (kept as interim).

§3 documents this as a stopgap. It does not give single-artifact identity, carries no input lineage, and
degenerates for non-per-sample outputs. Fine until re-genotyping or input censoring occurs; not the
durable design.

## 8. Open questions / risks

1. **Artifact-level vs. per-sample-slice stats.** Some consumers want the cohort-level number
   (cohort ti/tv); some want each sample's slice. Proposal: `cohort_artifacts.stats_json` holds the
   cohort-level summary; per-sample slices, if wanted, stay an ordinary `level = "assay"` analysis that
   references the same artifact path. Avoids re-denormalizing.
2. **`run_tag` discipline.** Identity hinges on `(analysis, run_tag)` uniqueness. Need a lint that warns
   when an `append-cohort` reuses a `run_tag` with a different input set.
3. **Export shape.** How does a cohort artifact appear in `casetrack export` (a per-sample TSV today)?
   Likely a separate `--cohort-artifacts` export, not crammed into the sample sheet.
4. **Dashboard / MCP surfacing.** Cohort artifacts and their staleness need a panel; out of scope for the
   first cut but noted.

## 9. Next actions

1. ~~Land §6.1 schema as an additive migration (`migrate-cohort`), no three-level change.~~ **Done** (`casetrack_qc/cohort_artifacts.py`; `init` + `migrate-cohort`).
2. ~~Implement `append-cohort` + provenance logging.~~ **Done** (`casetrack_qc/cohort_artifacts_cli.py`).
3. Add read-time staleness to `status` / `query` / `export` / `cohort`. **Partial** — `cohort-artifacts` surfaces staleness today (`artifact_staleness`); folding it into the other read paths is still open.
4. ~~`CASETRACK_COHORT_ARTIFACT` subworkflow + a joint-genotyping worked example in `examples/`.~~ **Done** — `casetrack_append_cohort` process in `examples/nextflow/casetrack.nf` + `examples/giab_chr21/run_cohort_demo.sh` (mock + bcftools engines).

### Still open
- Surface cohort-artifact staleness inside `status` / `export` / `query` (item 3 above), not just the dedicated `cohort-artifacts` command.
- Dashboard / MCP panel for cohort artifacts (§8.4).
- `--stats`-less `append-cohort` ergonomics (currently a `{}` file is the idiom).
