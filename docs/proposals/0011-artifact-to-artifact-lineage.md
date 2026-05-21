# Proposal 0011 — Artifact-to-artifact lineage (derived artifacts and multi-hop staleness)

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | accepted (design) — 2026-05-21 |
| **Depends on** | 0001 (hierarchy), 0002 (QC cascade), 0009 (cohort artifacts), 0010 (reference artifacts) |
| **Companion** | 0010 (reference artifacts) — the single-hop predecessor 0010 §5 carved this out of |
| **Target HPC** | IRIS @ MSKCC (WekaFS, SLURM, Apptainer) |

## 0. Accepted decisions

Settled during design; not re-litigated below:

1. **0011 is the recursive edge.** 0009 (assay → cohort artifact) and 0010 (output → reference) each give a *single hop*. 0011 adds a generic **`derived-from`** edge between any two lineage nodes, making the lineage a multi-hop DAG and making staleness **transitive**. This is the whole point — neither existing table can express "an artifact derived from another artifact."
2. **Both endpoints fully polymorphic over three node types** — `cohort:<analysis>@<run_tag>`, `reference:<ref_key>`, `analysis:<entity_level>/<entity_id>/<analysis>`. The three driving cases (cohort→cohort, reference→cohort, sample-output→cohort) plus the deferred-no-more reference→reference and sample-output→sample-output all fall out of one symmetric edge.
3. **One additive sibling table** (`artifact_derivation`), mirroring the 0009/0010 ethos. The three-level core, the 0009 tables, and the 0010 tables are untouched. (§7 records the rejected alternatives.)
4. **Canonical node-ref strings, not wide polymorphic columns.** Each endpoint is one TEXT column holding a canonical id; a shared `LineageNode` helper is the single home for parse/format/resolve. (§7.3 records why not wide columns.)
5. **`derived_stale` is a third orthogonal flag**, alongside 0009's `stale` and 0010's `ref_stale` — consistent with 0010's "never collapse into one flag" decision. A node can be any combination of the three. Its reason names the upstream culprit *and* that culprit's root cause.
6. **The walk traverses the 0010 `reference_usage` edge too.** A reference that is *derived* from a stale cohort artifact (a PoN whose input was censored) must propagate to everything that `uses` it — even with no TOML version bump. This is the connective tissue that closes the loop between 0009, 0010 and 0011.
7. **Read-time derived, no stored flag** — same as 0009/0010. Censor/uncensor and version bumps flow through automatically.
8. **DAG invariant enforced.** Cycle prevention at `derived-from` time (refuse an edge whose upstream is reachable from the downstream); a defensive cycle-guard in the read-time walk; `validate` asserts acyclicity.
9. **History in `provenance.jsonl`, not the DB.** Edge adds log `artifact_derivation_link`; the table holds only current edges.

## 1. Summary

casetrack models three output shapes today: single-owner (the hierarchy), many-producers-to-one-output (0009 cohort artifacts), and outputs-consume-versioned-inputs (0010 reference artifacts). All three derive staleness over a **single hop**. This proposal adds the **recursive** relation: a generic `derived-from` edge between any two lineage nodes, so staleness propagates through a chain.

One additive sibling table — `artifact_derivation(down_node, up_node, …)` — where each endpoint is a canonical node-ref over the three node types. Staleness is derived at read time: a node is **`derived_stale`** when any upstream node it derives from is itself stale by *any* cause (input-stale, ref-stale, or derived-stale — recursively). The walk spans all three edge tables, with memoization and a cycle guard. `derived_stale` is a third flag orthogonal to 0009's `stale` and 0010's `ref_stale`.

## 2. Motivation

The single-hop tables break the chain at every arrow. Three concrete cases, all real on the cohort:

- **Cohort → cohort (#1).** GATK joint genotyping produces a joint VCF (a cohort artifact, 0009). It is then VQSR-filtered, then annotated — each a *new* cohort artifact derived from the prior. Censor a contributing assay and 0009 flags the joint VCF stale, but the filtered and annotated VCFs read fresh: nothing records that they descend from it.
- **Reference ← cohort (#2, "PoN-as-ref").** A panel-of-normals is built from N normal assays (a cohort artifact, 0009), then declared as a reference (`[references.pon]`) that downstream callers `uses=["pon"]` (0010). Censor a normal that fed the PoN: 0009 flags the PoN cohort-artifact, but the `pon` *reference* — and therefore every VCF that consumed it — reads fresh, because 0010 only fires on a TOML *version* string change, not on the PoN's underlying inputs going stale.
- **Sample-output ← cohort (#3).** Per-sample genotyping against a cohort joint-sites VCF: the per-sample result descends from a cohort artifact. If the cohort sites VCF goes stale, the per-sample call should too.

Each case is a derivation edge the schema cannot currently express, and each needs *transitive* propagation that a single-hop cascade cannot provide. This proposal closes all three with one edge and one recursive read-time walk.

## 3. Interim pattern (what to do until this ships)

Encode the upstream artifact's identity into a result column on the downstream output (e.g. a `{prefix}_derived_from` column carrying `cohort:joint_genotype@cohort147_v1`) and audit by hand. This is the manual analog of an `artifact_derivation` row: it records the edge but does not cascade and must be re-checked per analysis. It is forward-compatible — those columns can seed `artifact_derivation` on migration.

## 4. Goals

- A generic `artifact_derivation` edge table linking any two lineage nodes over the three node types.
- A `LineageNode` helper: the single, tested home for canonical node-ref parse/format and resolution to existing staleness params.
- Read-time **transitive `derived_stale`** for cohort artifacts, references, and sample-level outputs, with a root-cause-naming reason, derived by a memoized cycle-guarded walk over all three edge tables.
- Capture: a generic `derived-from` command, `--derived-from` convenience on `append`/`append-cohort`, and a declarative TOML `derived_from` on references.
- Surfacing in every existing read path (`status`, `query`, `export`, `dashboard`, MCP, `validate`) plus a new `lineage` command.
- Cycle prevention at write time and a `validate` acyclicity invariant.
- Zero change to the three-level core, the 0009 tables, and the 0010 tables; additive migration for existing projects.

## 5. Non-goals (first cut)

- **Auto-recomputation** of stale outputs — flagged, never auto-fixed (same posture as 0009/0010).
- **Checksum-drift detection** on derived artifacts — staleness keys on identity + upstream state, not content hashing. A future `doctor` check (same deferral as 0010 §5).
- **Storing artifact contents in-DB** — only the edge; the files live on WekaFS (same as 0009).
- **Cross-project lineage** — edges reference nodes within one `casetrack.db`.
- **Edge attributes / typed roles** (e.g. "trained-on" vs "filtered-from"). An edge is an untyped `derived-from`. A `role` column is a clean future addition if a use case appears.

## 6. Design (Approach A — one additive sibling table)

### 6.1 Node addressing — `LineageNode`

A node is identified by a **canonical node-ref string**, parsed/formatted by a single helper so addressing logic lives in one place:

| Node type | Canonical form | Resolves to |
|---|---|---|
| cohort artifact | `cohort:<analysis>@<run_tag>` | `cohort_artifacts` row via the `(analysis, run_tag)` natural key |
| reference artifact | `reference:<ref_key>` | `reference_artifacts.ref_key` |
| sample-level output | `analysis:<entity_level>/<entity_id>/<analysis>` | the `(entity_level, entity_id, analysis)` triple `reference_usage` already uses |

Cohort artifacts are addressed by the **natural key** `(analysis, run_tag)`, never the autoincrement `artifact_id` — the natural key is stable across re-import and human-writable in TOML/CLI. `LineageNode.resolve(conn)` maps a node-ref to the parameters the existing `output_staleness()` accepts (and, for cohort, to its `artifact_id`).

```python
@dataclass
class LineageNode:
    scope: str            # 'cohort' | 'reference' | 'analysis'
    # cohort:    analysis, run_tag
    # reference: ref_key
    # analysis:  entity_level, entity_id, analysis
    ...
    @classmethod
    def parse(cls, s: str) -> "LineageNode": ...
    def canonical(self) -> str: ...
    def resolve(self, conn) -> dict | None:  # None => dangling (validate flags it)
        ...
```

### 6.2 Schema — one additive table

```sql
CREATE TABLE artifact_derivation (
    derivation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    down_node      TEXT NOT NULL,   -- canonical node-ref: the derived/downstream output
    up_node        TEXT NOT NULL,   -- canonical node-ref: the source/upstream artifact
    recorded_at    TEXT NOT NULL,
    transaction_id TEXT
);
CREATE UNIQUE INDEX idx_deriv_edge ON artifact_derivation(down_node, up_node);
CREATE INDEX idx_deriv_up   ON artifact_derivation(up_node);
CREATE INDEX idx_deriv_down ON artifact_derivation(down_node);
```

Deliberate choices:
- **No SQL FK on either endpoint.** Endpoints are polymorphic canonical strings; integrity is app-level, exactly as `reference_usage`'s entity side already is. `validate` flags dangling edges (§6.5). (Note: this trades away the `ON DELETE CASCADE` `reference_usage` keeps on `artifact_id` — acceptable, since a cohort artifact addressed by natural key is not normally deleted, and a dangling edge is *meaningful*, not corrupt.)
- **`(down_node, up_node)` unique** — re-recording the same edge is idempotent.
- **`artifact_derivation` is the only new object.** No view materialization in SQLite; derived state is computed by the read-time walk and surfaced through the existing DuckDB views.

### 6.3 Staleness — the recursive walk (`derived_stale`)

**Read-time derived, no stored flag.** Define, over the union of all three edge tables:

```
upstream(node) = { up_node : artifact_derivation row with down_node = node }      # 0011 edges
              ∪ { reference:<ref_key> : reference_usage row with consumer = node } # 0010 edge, traversed as derivation

direct_stale(node) =
      direct_input_stale(node)     # 0009: a contributing assay is censored / consent-revoked  (cohort artifacts only)
   OR direct_ref_stale(node)       # 0010: a uses= reference's version_used != current          (analysis / cohort consumers)

is_stale(node)      = direct_stale(node) OR ANY( is_stale(u) for u in upstream(node) )    # memoized, cycle-guarded
derived_stale(node) = ANY( is_stale(u) for u in upstream(node) )                          # the new third flag
```

`derived_stale` is `is_stale` minus the node's *own* direct causes — it answers "is any source I descend from stale." This keeps it orthogonal to `stale` (0009) and `ref_stale` (0010).

**Traversing the 0010 edge is the load-bearing subtlety.** A reference node (`reference:pon`) has no intrinsic input- or ref-staleness, but it *can* be derived-stale when its own `artifact_derivation` edge points at a stale cohort artifact. By treating each `reference_usage` row as an upstream derivation link to its `reference:<ref_key>` node, the walk lets that derived-staleness reach the reference's consumers — even when the TOML version string never changed. This is exactly the gap 0010 §5 left for 0011.

**Three flags, never collapsed** (extends 0010's orthogonality):

| Flag | Cause | Proposal |
|---|---|---|
| `stale` | a contributing assay censored / consent-revoked | 0009 |
| `ref_stale` | a `uses=` reference version-bumped | 0010 |
| `derived_stale` | an upstream artifact it derives from is stale (any cause, transitively) | **0011** |

**Reasons name the chain.** `derived_stale` reasons carry the upstream culprit *and* its root cause:
`STALE(derived): upstream cohort:make_pon@cohort147_v1 is STALE(inputs): assay A2 censored`.

**`derived_stale` is a boolean rollup, not three-state.** Absence of derivation edges means `False` — a leaf cohort artifact tracked only via `cohort_artifact_inputs` is *fully tracked*, not "untracked." (The 0010 `untracked` state stays specific to ref-tracking, where "never declared `uses`" is genuinely distinct from "verified fresh.")

**Cycle guard + memoization.** The walk memoizes `is_stale` per node within one evaluation and tracks the in-progress path; a back-edge is treated as a no-contribution terminal (and is independently prevented at write time, §6.4 / flagged by `validate`, §6.5).

**History in provenance.** Each edge add writes `action='artifact_derivation_link'`, `{down_node, up_node}`. The table holds only current edges.

### 6.4 CLI

- **`casetrack derived-from --downstream <node> --upstream <node> [--upstream …] --project-dir .`** *(new)* — record one or more edges between **existing** nodes in one transaction; **cycle-checked** (refuse if the downstream node is reachable from any proposed upstream node, with a clear error naming the offending path). Logs provenance.
- **`casetrack append --derived-from <up-node>,…`** *(extended)* — convenience: downstream is the sample-level output being appended.
- **`casetrack append-cohort --derived-from <up-node>,…`** *(extended)* — convenience: downstream is the cohort artifact just created.
- **TOML `[references.<key>] derived_from = ["cohort:make_pon@cohort147_v1", …]`** *(extended)* — declarative; materialized into `artifact_derivation` (downstream `reference:<key>`) on `schema apply`, mirroring `uses`.
- **`casetrack migrate-derivation --project-dir . [--dry-run]`** *(new)* — additive table creation (mirrors `migrate-references`); `init` creates the table by default going forward.
- **`casetrack lineage --project-dir . [--node <ref>] [--stale-only] [--fmt table|tsv|json]`** *(new)* — lists edges with per-node `derived_stale`; `--node X` shows X's immediate up/downstream neighbours and the full root-cause chain; `--stale-only` drills into derived-stale outputs.

### 6.5 Read-path integration

- **`casetrack status`** — a "Lineage / derived" section (edge count + derived-stale tally); `derived_stale` flagged inline on artifacts alongside `stale` / `ref_stale`.
- **`casetrack query`** — a `_artifact_derivation` DuckDB view (edges + resolved endpoint kinds + per-down-node `derived_stale`); a `derived_stale` column added to `_cohort_artifacts` (beside `stale` and `ref_stale`) and to `_reference_usage`.
- **`casetrack export --include-derivation`** — emits `artifact_derivation` with derived-staleness; auto-enabled for XLSX.
- **`casetrack dashboard`** — a "Lineage" section + `derived-stale` badges on outputs.
- **MCP** — a `casetrack_lineage` tool (companion to the CLI): edges + derived-stale outputs with their root-cause chains.
- **`casetrack validate`** — new invariants: (a) every `artifact_derivation` endpoint resolves to a live node, else flag a **dangling edge**; (b) the derivation graph (union with the traversed `reference_usage` edges) is **acyclic**, else flag the cycle.

### 6.6 Nextflow

- **`CASETRACK_REGISTER`** — gains an optional `--derived-from` passthrough param, so a tracked sample-level subworkflow can declare the cohort artifact it descended from.
- **`casetrack_append_cohort`** — gains an optional `derived_from` input following the same `[]`-means-none pattern as the existing `stats` / `uses_references` slots (no placeholder file; flag dropped when empty). Used for the joint→VQSR→annotated chain.

## 7. Design alternatives considered

### 7.1 Approach A — one additive sibling table, canonical node-refs. **Chosen** (§6).

### 7.2 Approach B — extend `reference_usage` with a nullable `upstream_artifact_id`. **Rejected.**
Let a `reference_usage` row point at either a `ref_key` or an upstream artifact, avoiding a new table. Rejected because it mutates 0010's clean "output consumes an *external* versioned input" semantics, forces nullable/either-or columns and forked partial indexes, and still cannot express the symmetric cases (reference→reference, cohort→cohort where neither end is a `ref_key`). One purpose-built additive table is clearer and leaves 0010 untouched.

### 7.3 Approach C — wide polymorphic columns (down_scope/down_entity_level/…/up_scope/…). **Rejected.**
Mirror `reference_usage`'s column-per-field convention, duplicated for both endpoints (~15 columns, mostly NULL). It keeps per-field SQL queryability and an `artifact_id` FK, but two-ended polymorphism makes it genuinely unwieldy and NULL-heavy, and the staleness walk has to re-assemble a node identity from six columns at every hop anyway. Canonical node-refs + a single `LineageNode` helper give the same resolution with a far smaller, honestly-graph-shaped table; the lost `artifact_id` FK is already precedented away on `reference_usage`'s entity side.

### 7.4 Approach D — a fourth hierarchy level / a generic node table subsuming all artifacts. **Rejected** (for the record).
A unified `nodes` + `edges` graph that absorbs `cohort_artifacts`, `reference_artifacts`, and entity outputs into one table is the "real" graph database move. Rejected for the same reason 0009 §7 rejected the 4th level: it is a wide refactor of three shipped subsystems to gain nothing the additive edge does not already provide. 0011 composes *with* 0009/0010, it does not subsume them.

## 8. Open questions / risks

- **Dangling edges.** Endpoints have no FK, so deleting a cohort artifact or removing a reference can orphan an edge — same risk profile as `qc_events` / `reference_usage`; `validate` flags it. Entities/artifacts are not normally deleted; a dangling edge reads as derived-stale-of-an-unresolvable-source, which is honest.
- **Walk cost.** The read-time walk is O(nodes + edges) per evaluation with memoization; cohort graphs are small (tens–hundreds of artifacts). If a project ever grows pathological, a materialized closure cache is a clean future optimization — not needed now.
- **User-asserted derivation.** As with 0010's version strings, the edge is operator-declared; a derivation that is never recorded yields `derived_stale = False` (honest, not wrong). The interim `{prefix}_derived_from` column (§3) is the bridge.
- **Run-tag discipline carries over.** A cohort node-ref hinges on `(analysis, run_tag)` uniqueness (already a 0009 concern). A re-genotyping run with a new `run_tag` is a *new* upstream node; downstream edges must be re-pointed deliberately — which is the correct, auditable behaviour.

## 9. Next actions

1. Schema module `casetrack_qc/artifact_derivation.py`: DDL + idempotent `ensure_derivation_schema` (mirrors `reference_artifacts.py`); `LineageNode` parse/format/resolve; `record_edge` + write-time cycle check.
2. Read-time `derived_staleness` walk (memoized, cycle-guarded, traversing all three edge tables) + `all_derived_stale`.
3. Capture: `derived-from` command; `append` / `append-cohort` `--derived-from`; TOML `[references].derived_from` materialized on `schema apply`; `artifact_derivation_link` provenance.
4. Read paths: `_artifact_derivation` view; `derived_stale` column on `_cohort_artifacts` and `_reference_usage`; `status` section; `export --include-derivation`; `dashboard`; `lineage` CLI; `validate` (dangling + acyclic).
5. MCP `casetrack_lineage` tool.
6. Nextflow: `CASETRACK_REGISTER` `--derived-from` passthrough; `casetrack_append_cohort` `derived_from` input.
7. Migration `migrate-derivation`; `init` creates the table going forward.
8. Tests (§10); docs (README, CLAUDE.md, casetrack skill, CHANGELOG); version bump.

## 10. Testing

- DDL idempotency; `ensure_derivation_schema` on a fresh and an existing DB.
- `LineageNode` round-trip parse/format for all three node types; resolve to live + dangling; rejection of malformed node-refs.
- Capture: `derived-from` single + multi-upstream; `append --derived-from`; `append-cohort --derived-from`; TOML `derived_from` materialized on `schema apply`; idempotent re-record; provenance entry shape.
- **Cycle prevention**: `derived-from` refuses a direct cycle (A→A), a 2-hop cycle (A→B, B→A), and a longer chain; error names the path.
- **Staleness — the core matrix**:
  - cohort→cohort chain: censor an assay feeding the root, assert the annotated leaf reads `derived_stale` with the chained root-cause reason.
  - reference←cohort (PoN-as-ref): censor a PoN input, assert the `pon` reference *and* a downstream VCF that `uses=["pon"]` both read `derived_stale` **with no TOML version bump** (the §6.3 traversal of the 0010 edge).
  - sample-output←cohort: cohort sites VCF stale ⇒ per-sample output `derived_stale`.
  - **Orthogonality**: a node that is `derived_stale` but not `stale`/`ref_stale`, and every other independent combination, including a node fresh on all three.
  - `derived_stale = False` for a leaf cohort artifact with no derivation edges (not "untracked").
  - Cycle-guard: the read walk terminates and returns a sensible result even if a back-edge slips into the table.
- Read paths: `lineage` CLI (table/tsv/json + `--node` + `--stale-only`); `status` section; `_artifact_derivation` view + `derived_stale` on `_cohort_artifacts` / `_reference_usage`; `export --include-derivation`; `validate` dangling + acyclic invariants; MCP tool.
- Nextflow: `casetrack_append_cohort derived_from []` drops the flag; `CASETRACK_REGISTER --derived-from` passthrough.
- Migration: `migrate-derivation` additive on a 0010-era DB; `--dry-run`.
