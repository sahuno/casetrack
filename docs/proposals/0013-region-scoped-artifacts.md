# Proposal 0013 — Region-scoped artifacts + contrast roles

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | **Draft** |
| **Date** | 2026-05-22 |
| **Target release** | TBD (post-v0.10; confirm numbering before implementation) |
| **Depends on** | Proposal 0009 (cohort-level artifacts), Proposal 0010 (reference artifacts) |
| **Target HPC** | IRIS @ MSKCC (WekaFS, SLURM, Apptainer) |

## 0. Accepted decisions

| # | Question | Accepted answer |
|---|---|---|
| 1 | Are genes / loci / variants first-class tracked entities? | **No.** Casetrack tracks *provenance and usability of outputs over the sample tree*. Genes/loci live on an orthogonal **feature axis**; individual findings (variant-as-finding, per-DMR, per-DMC) are a *findings store* — deferred (option **C**, §7.3). This proposal handles only **region-scoped output artifacts** (option **B**). |
| 2 | How is region scope represented? | **A named label** on the artifact (`region_scope`), e.g. `genome-wide`, `promoters_EPDnew`, `LTR_subfamilies`, `chr17:7565097-7590856`. Casetrack does **no interval math** (option **A** behavior). |
| 3 | What happens when a scope label matches a registered reference key? | **It resolves** (option **C**, the "reference door"). A reference-backed scope auto-captures a `reference_usage` row (0010, `scope='cohort'`), so the artifact inherits 0010's `ref_stale` semantics for free. A label-only scope stays an opaque, groupable string. |
| 4 | Where does scope attach? | **Cohort artifacts only** (`cohort_artifacts`, 0009) — option **A1**. Every headline case (DMR contrast, PoN, cohort matrix) is a cohort artifact. The column shape is kept **compatible with A3** (scope on any lineage node) as a documented future migration. |
| 5 | Does a contrast record its design (tumor vs normal)? | **Yes.** Add a nullable `role` column on `cohort_artifact_inputs`. Descriptive metadata only — **not** staleness-bearing. Folded into 0013 because roles are tightly coupled to region-scoped contrasts. |
| 6 | Does the three-level core or 0009/0010 table set change? | **No.** Two nullable columns on existing 0009 tables. No new tables. Existing artifacts (scope `NULL`, roles `NULL`) remain valid and unscoped. |
| 7 | Interval / overlap queries ("which outputs touch chr17:7.58M")? | **Out of scope.** Not built. The *door* is left open: a reference-backed scope points at a BED `path` (0010), so a future command could read its intervals without a schema change. |

## 1. Summary

Add the ability to tag a cohort-level artifact (0009) with the **genomic territory it covers** —
a differential-methylation region (DMR) call between a tumor group and a normal group, a
panel-restricted re-genotyping run, a cohort methylation matrix over a repeat-element class, a
per-gene panel-of-normals. These outputs already fit 0009's `(analysis, run_tag)` model; what's
missing is (a) a first-class **region scope**, so artifacts can be grouped, filtered, and made
stale by the region set they used, and (b) a **role** on each contributing input, so a contrast
records its *design* and not merely its membership.

The design is **two nullable columns** — `cohort_artifacts.region_scope` and
`cohort_artifact_inputs.role` — plus one derivation rule: **a `region_scope` that matches a
registered `ref_key` (0010) auto-captures a `reference_usage` edge**, so scope-driven staleness is
entirely 0010's existing machinery. No new tables, no new staleness code, no interval math.

This is option **B** (region-scoped *artifacts*), with the per-region *findings* store (option
**C**) explicitly deferred. §7 records the rejected alternatives, including why genes/loci are not
a new hierarchy level and not a new axis in this cut.

## 2. Motivation

casetrack's entire model lives on one axis: **provenance of outputs over the sample tree**
(`patient → specimen → assay`, plus cohort artifacts fanning up, reference artifacts cascading
down, derivation tying it together). Every node answers the same question — *is this output still
valid given its inputs and the QC state of its samples?*

Genes and loci are not on that axis. A locus is a coordinate in *reference* space, not a sample and
not an output. The driving cases are **region-scoped analyses** whose output is a file bounded by,
or producing, genomic territory:

- **DMR call, tumor vs normal** (DSS / methylKit / `modkit dmr`) — one BED of regions, derived from
  two *groups* of assays. This is a cohort artifact (0009) whose contributing assays have *roles*
  (tumor / normal) and whose scope is either `genome-wide` or a panel (promoters, CpG islands).
- **Repeat / LTR methylation aggregation** — methylation summarized over a feature *class* (LTR
  subfamilies). The run is a cohort artifact; its scope is the repeat panel.
- **Hotspot / gene-panel re-genotyping**, **per-gene PoN**, **targeted coverage over an interval
  list** — each is an artifact whose identity is incomplete without "what territory did it cover?"

Two concrete gaps in 0009 today:

1. **No region scope.** An artifact can declare it `uses` a reference panel (0010), but it cannot
   record "my scope is `chr17:7565097-7590856`" (ad-hoc) or even `genome-wide` as a first-class,
   groupable attribute. You cannot ask *"which outputs cover scope X?"* unless the scope happens to
   be a named reference and you happen to have listed it in `uses`.
2. **No role on inputs.** `cohort_artifact_inputs` is a flat many-to-many list. A DMR or DE contrast
   has a *design* — these assays were tumor, those were normal — which the membership list loses.

## 3. Goals

- `cohort_artifacts.region_scope TEXT NULL` — a scope label on each cohort artifact.
- `cohort_artifact_inputs.role TEXT NULL` — a contrast role on each contributing input.
- **Reference-resolve**: when `region_scope` matches a registered `ref_key`, auto-capture a
  `reference_usage` row (0010, `scope='cohort'`) so scope changes cascade staleness with no new code.
- `casetrack append-cohort` gains `--region-scope` and per-input roles.
- Read-time surfacing of scope + roles + (reference-backed) ref-staleness in every existing read
  path: `cohort-artifacts`, `status`, `query` (`_cohort_artifacts`), `export`, dashboard, MCP.
- `casetrack migrate-region-scope` to add the two nullable columns to pre-0013 projects.

## 4. Non-goals (first cut)

- **Per-region findings.** Each DMR / DMC / variant as a queryable row is the *findings store*
  (option C, §7.3) — deferred to a later proposal.
- **Interval math / overlap queries.** Casetrack does not parse or compare coordinates. Only the
  door is left open (a reference-backed scope knows its BED `path`).
- **Coordinate validation** of ad-hoc scope strings (`chr17:…` is stored verbatim, unchecked).
- **Scope on sample-level analyses (A2)** and **scope-on-any-node (A3)** — deferred. `region_scope`
  is a plain column now; A3 promotes it to a node-keyed sibling table later (§7.2).
- **Multiple scopes per artifact** — one label per artifact; multi-scope waits for A3.

## 5. Design (additive columns on 0009 tables)

### 5.1 Schema

```sql
-- 0009 table, two existing columns elided; ADD one nullable column:
ALTER TABLE cohort_artifacts      ADD COLUMN region_scope TEXT;   -- NULL = unscoped
-- 0009 join table; ADD one nullable column:
ALTER TABLE cohort_artifact_inputs ADD COLUMN role        TEXT;   -- NULL = unroled

-- optional grouping index (scope is a frequent filter/group key):
CREATE INDEX IF NOT EXISTS idx_cohort_artifacts_scope ON cohort_artifacts(region_scope);
```

Deliberate choices:

- **Nullable, backward-compatible.** Every pre-0013 artifact reads as `region_scope = NULL`
  (unscoped) and every input as `role = NULL` (unroled). No backfill, no breakage.
- **`region_scope` is a free-text label, not an FK.** It *may* coincide with a `ref_key`; the link
  is derived at read time (§5.2), not enforced by a constraint — mirroring 0010's deliberate
  no-FK-on-`ref_key` choice (a label can name a panel that isn't registered, and that's fine).
- **`role` is descriptive only.** It does not participate in any staleness computation. It answers
  "what was the contrast design" and powers queries like "DMR artifacts where assayX was tumor."
- **No new tables.** The whole proposal is two `ALTER`s + one read-time rule.

### 5.2 Reference-resolve and staleness (this is option C)

The one new rule. At `append-cohort --region-scope <label>` time:

> If `<label>` equals a `ref_key` in `reference_artifacts`, casetrack inserts a `reference_usage`
> row with `scope='cohort'`, `artifact_id = <new artifact>`, `ref_key = <label>`, and
> `version_used = <current reference_artifacts.version>`.

That is the entire mechanism. Everything downstream is **0010 unchanged**:

- **Reference-backed scope** → the auto-captured usage row means the artifact's `ref_stale` flag
  (0010 §6.2, three-state `fresh` / `STALE` / `untracked`) tracks the panel's version. Bump
  `promoters_EPDnew` `2026-04-14 → 2026-05-01`, run `schema apply`, and the artifact is `STALE`
  with reason `promoters_EPDnew: 2026-04-14 -> 2026-05-01`. **No new staleness code in 0013.**
- **Label-only scope** (`genome-wide`, `chr17:…`, an unregistered class name) → no usage row, no
  staleness implication. A groupable, filterable opaque string — exactly option A.

Idempotency is inherited: 0010's `idx_refusage_cohort ON reference_usage(artifact_id, ref_key)
WHERE scope='cohort'` already prevents a duplicate edge if the same artifact also lists the panel in
its analysis `uses`. The scope-derived edge and a `uses`-derived edge for the same panel collapse to
one row.

**Orthogonality (carries 0009 + 0010 forward).** A cohort artifact can now carry up to three
independent read-time flags, none interacting:

| Flag | Source | Meaning |
|---|---|---|
| `input_stale` (0009) | `cohort_artifact_inputs` + QC cascade | a contributing assay is censored / consent-revoked |
| `ref_stale` (0010) | `reference_usage` version mismatch | a reference (incl. a reference-backed scope) changed version |
| `derived_stale` (0011) | `artifact_derivation` traversal | an upstream node it derives from is stale by any cause |

`region_scope` does not add a fourth flag — a reference-backed scope folds into `ref_stale`, and a
label-only scope is not staleness-bearing at all.

**Forward door (the C promise, not built here).** Because a reference-backed scope resolves to a
`reference_artifacts` row carrying the BED `path`, a future `casetrack` command could read those
intervals and answer overlap queries ("which scoped outputs touch `chr17:7.58M`?") without any
schema change. 0013 deliberately stops at the door.

### 5.3 CLI sketch

```bash
# DMR contrast: genome-wide scope, tumor/normal roles on inputs
casetrack append-cohort --project-dir . \
  --analysis dss_dmr \
  --run-tag 20260522_hg38_tumor_vs_normal \
  --path data/processed/hg38/cohort/dmr.bed.gz \
  --region-scope genome-wide \
  --inputs assayT1:tumor,assayT2:tumor,assayN1:normal,assayN2:normal

# Panel-restricted DMR: scope == a registered reference key → auto ref_stale tracking
casetrack append-cohort --project-dir . \
  --analysis dss_dmr \
  --run-tag 20260522_hg38_promoters \
  --path data/processed/hg38/cohort/dmr_promoters.bed.gz \
  --region-scope promoters_EPDnew \
  --inputs-from contrast.tsv     # TSV may carry an optional `role` column

# List + filter by scope; reference-backed scopes show their ref_stale state (via 0010)
casetrack cohort-artifacts --project-dir . --scope promoters_EPDnew
casetrack cohort-artifacts --project-dir . --fmt json

# Add the two columns to a pre-0013 project
casetrack migrate-region-scope --project-dir . --dry-run
casetrack migrate-region-scope --project-dir .
```

`--inputs` accepts `assay_id[:role]` per item; `--inputs-from FILE` tolerates an optional `role`
column alongside `assay_id` (consistent with 0009's existing header tolerance).

### 5.4 Read-path surfacing

Scope and roles ride along as extra columns in paths that already iterate cohort artifacts — no new
surfaces:

| Surface | Change |
|---|---|
| `casetrack cohort-artifacts` | adds a `region_scope` column + `--scope <label>` filter; ref-backed scopes already surface `ref_stale` via 0010 |
| `casetrack status` | the cohort-artifact section gains scope per artifact |
| `casetrack query` | `_cohort_artifacts` view gains `region_scope` + derived `scope_ref_key` (NULL when label-only); inputs view gains `role` |
| `casetrack export --include-cohort-artifacts` | scope + roles included in TSV/JSON |
| HTML dashboard | scope shown in the cohort-artifact section |
| MCP `casetrack_cohort_artifacts` | scope + roles in the returned rows |
| `casetrack validate` | no new invariant (a label that doesn't resolve is legal); 0010's orphan-`reference_usage` check already covers auto-captured edges |

## 6. Worked example — DMR tumor vs normal

```
1. Declare the panel as a reference (0010), if scope is panel-restricted:
     [references.promoters_EPDnew]
     path = "/data1/greenbab/database/EPDpromoters/.../Hs_EPDnew.hg38.bed.gz"
     version = "2026-04-14"
     kind = "intervals"
   casetrack schema apply

2. Register the DMR run as a scoped cohort artifact with roles:
     casetrack append-cohort --analysis dss_dmr --run-tag 20260522_hg38_promoters \
       --path .../dmr_promoters.bed.gz --region-scope promoters_EPDnew \
       --inputs assayT1:tumor,assayN1:normal
   → because `promoters_EPDnew` is a ref_key, a reference_usage(scope='cohort') row is captured.

3. Later, the panel is re-released:
     promoters_EPDnew.version = "2026-04-14" → "2026-05-01" ; casetrack schema apply
   casetrack cohort-artifacts --stale-only
   → the DMR artifact is STALE (ref_stale): "promoters_EPDnew: 2026-04-14 -> 2026-05-01"

4. A contributing tumor assay is later censored:
     casetrack censor --level assay --id assayT1 --kind contamination --reason "..."
   → the same artifact is also input_stale (0009) — both flags shown, independently.
```

## 7. Design alternatives considered

### 7.1 Genes/loci/variants as a new hierarchy level or a new sample-tree axis. **Rejected.**

A hierarchy level is biological, single-parent, and static (0009 §7.1). A locus is none of these:
it is a reference coordinate, many-to-many with samples, and dynamically membered. Wedging a feature
axis into the sample tree is the same category error 0009 rejected for cohort artifacts. The feature
axis, *if* it is ever built, belongs in the findings store (§7.3), not the hierarchy.

### 7.2 Scope on any lineage node now (A3). **Deferred, not rejected.**

A fully general `scope` attaching to any node (`cohort:`, `analysis:`, `reference:`, à la 0011)
would also cover per-sample region-scoped outputs (one specimen's LTR methylation) and multi-scope
artifacts. It is the right *eventual* shape, but it is a larger surface than the headline cases need.
0013 keeps `region_scope` a plain column on `cohort_artifacts`; the A3 migration is a clean promotion
to a `node_scope(node_ref, region_scope, ...)` sibling table keyed by canonical node-ref strings,
reusing 0011's node-ref vocabulary. Building A1 first does not foreclose A3.

### 7.3 Per-region findings store now (option C). **Deferred.**

Storing each DMR / DMC / variant as a queryable row (recurrence across the cohort, "who carries
locus X", pathogenicity assertions) is a genuinely useful but much larger system — effectively a
mutation/methylation matrix store (cBioPortal-shaped). It is the *fine-grained* layer that would
hang off the region-scoped artifacts 0013 introduces. Deferring it keeps casetrack a tracker. When
built, each finding references the scoped artifact (0013) it was extracted from, so 0013 is the
natural substrate, not a throwaway.

### 7.4 Coordinate-aware scope with overlap queries (stored intervals). **Rejected for this cut.**

Storing parsed coordinates and answering interval-containment queries makes casetrack own genomic
interval logic and edges it toward a feature DB / genome browser — a real scope expansion away from
"tracker." The reference-resolve door (§5.2) provides a *path* to this later (the BED is reachable
via the reference's `path`) without committing to interval math now.

### 7.5 Two facts instead of one: scope label *and* a separate `uses` entry. **Rejected.**

Option A (pure label) would leave a panel-scoped artifact maintaining its scope and its `uses` list
as two independent facts that must be kept in sync, with only the latter driving staleness. The
reference-resolve rule (§5.2) makes "the scope" and "the panel input" the *same* fact when the label
is a registered reference — removing the sync burden for almost no extra cost (a single read-time
`ref_key` lookup).

## 8. Open questions / risks

- **Scope label hygiene.** Free-text labels can drift (`genome-wide` vs `genomewide` vs `WG`). No
  enforcement is planned; a future `casetrack doctor --scopes` could report near-duplicate labels.
  Convention (not constraint): lowercase, snake/colon-namespaced (`panel:cancer_hotspots`).
- **Role vocabulary.** `role` is free-text; common values (`tumor`/`normal`/`case`/`control`/
  `treated`/`untreated`) are convention, not an enum, to avoid premature closure. Revisit if a
  downstream consumer needs a controlled set.
- **A3 migration cost.** Promoting `region_scope` (column) → `node_scope` (table) is a data move;
  documented as a future migration, but the column-first choice means it is deferred work, not free.
- **Numbering.** Confirm 0013 is the right proposal number and target release before implementation
  (the same numbering caveat carried by 0009).

## 9. Next actions

1. Confirm proposal number / target release.
2. Implement schema (`migrate-region-scope`; `cohort_artifacts.region_scope`,
   `cohort_artifact_inputs.role`) in `casetrack_qc/cohort_artifacts.py`.
3. Reference-resolve capture in the `append-cohort` path (`casetrack_qc/cohort_artifacts_cli.py`),
   reusing 0010's `reference_usage` insert with `scope='cohort'`.
4. `--region-scope` + per-input role parsing on `append-cohort`; `--scope` filter on
   `cohort-artifacts`.
5. Read paths: `_cohort_artifacts` view (`region_scope`, derived `scope_ref_key`), inputs `role`;
   `status` / `export` / dashboard / MCP columns.
6. Nextflow: optional `region_scope` + roles inputs on `casetrack_append_cohort` /
   `COHORT_ARTIFACT_TRACKED`.
7. Tests: scope round-trip, reference-resolve auto-capture + idempotency vs `uses`, label-only
   no-staleness, role round-trip, migrate-region-scope, all read paths, backward-compat (NULL).

### Still open

Nothing blocking. The two-column shape, the reference-resolve rule, A1-now/A3-later, and roles-in
are accepted (§0). Implementation can proceed once numbering is confirmed.
