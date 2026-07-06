# Proposal 0014 — Enterprise provenance platform (architecture vision)

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | **Vision / exploratory** (not a shippable spec; a target to build toward) |
| **Date** | 2026-07-06 |
| **Target release** | N/A — describes a *successor platform*, not a casetrack point release |
| **Depends on** | The whole 0001–0013 line (casetrack is the reference implementation / single-lab edition) |
| **Target deployment** | On-prem **and** multi-tenant SaaS, regulated (GxP / HIPAA / 21 CFR Part 11) |

> **Read this first.** This is not a feature spec for the casetrack CLI. It is the architecture
> for an enterprise-grade provenance *platform* for which casetrack is the prototype and reference
> model. It records a strategic direction and the reasoning behind it so a future session (or a
> different engineer) can pick up the design without re-deriving it. Nothing here is committed or
> scheduled.

## 0. Framing decisions (captured this session)

| # | Question | Answer / recommendation |
|---|---|---|
| 1 | Which product is this? | **All three** — reproducibility platform, data catalog / lineage, compliance & audit. Resolved by the §2 thesis: they are three *read models over one substrate*, not three products. |
| 2 | Evolve casetrack or greenfield? | **Undecided** → recommendation in §3: **greenfield core, concept-inherited** (strangler-fig). Keep casetrack's model, replace its substrate. |
| 3 | Deployment / compliance regime? | **On-prem + SaaS + regulated.** One codebase, two shapes (§5); regulated primitives baked in from Layer 1, certified late (§6, §7). |
| 4 | Highest-leverage strategic call? | **Build-vs-buy on the catalog layer** (§8, risk 4). The defensible wedge is *validity + reproducibility*, not catalog UX — which may reframe scope downward (feed an existing catalog rather than rebuild one). |

## 1. Summary

An enterprise provenance platform that serves reproducibility, cataloging/lineage, and
compliance/audit is **one system, not three**, if it is built as an **immutable, tamper-evident
event log (the single source of truth) projected into three specialized read models**
(event-sourcing / CQRS). Reproducibility queries the projected graph ("what produced this, is it
still valid"); catalog queries it ("what exists, what feeds what"); compliance queries the log
("who did what, prove it wasn't altered").

casetrack already embodies the right pattern in miniature — `provenance.jsonl` is an append-only
event log, and `qc_status` / the read-time staleness cascade are projections derived from it. The
**conceptual model is ahead of the market**; the **substrate (single-file SQLite, hardcoded
3-level hierarchy, Python monolith) is the wall.** The recommendation is therefore a greenfield
core that inherits every hard-won concept while replacing the storage and execution substrate, with
casetrack surviving as the reference implementation and single-lab edition.

The build is phased so that "all three + on-prem + SaaS + regulated" — which is a platform, not a
tool — is actually deliverable: reproducibility + lineage on a solid, hash-chained substrate first;
catalog + multi-tenant platform second; compliance certification (which is cheap to *enable* early,
expensive to *certify* late) third.

## 2. Motivation — three products are three views of one substrate

The three product interpretations answer questions about the *same underlying facts*:

- **Reproducibility** — asks the graph: *what produced this artifact, is it still valid, can I
  re-run it?* (This is where casetrack lives today.)
- **Catalog / lineage** — asks the graph: *what exists, what feeds what, where did it come from?*
  (Collibra / DataHub / OpenMetadata / OpenLineage territory.)
- **Compliance / audit** — asks the log: *who did what to which record, and prove the record was
  not altered?* (21 CFR Part 11 / GxP / SOC 2 territory.)

Built naively these become three teams building three schemas. Built correctly they are **one
append-only event log** projected three ways. This is the load-bearing insight of the whole
proposal: it is the only structure under which "all three" is not a scope explosion.

casetrack is evidence the pattern works: an append-only JSONL log plus materialized/read-time
projections. The platform generalizes exactly this, on a substrate that scales.

## 3. Evolve vs. greenfield — recommendation

**Greenfield core, concept-inherited — a strangler-fig, not a rewrite-in-place.**

Separate the two things casetrack is:

**The conceptual model is a genuine asset — port it wholesale.**
- Append-only provenance as the source of truth.
- Read-time staleness with **three orthogonal flags** (`stale` / `ref_stale` / `derived_stale`).
- Additive sibling-table evolution (the `qc_events` pattern — never mutate the core, add beside it).
- Reference-version cascades (0010) and transitive derivation staleness (0011).
- Many funded catalog tools have *no real validity model at all.* This is the wedge — keep every bit.

**The substrate is a hard wall for enterprise — do not grow it into the platform.**
- Single-file SQLite, single-writer → cannot do concurrent multi-writer.
- Hardcoded 3-level `LEVEL_ORDER` → the entity model must be data-driven (see the custom-key bug:
  the DuckDB `_` view hardcodes `patient_id`/`specimen_id` and breaks on projects keyed
  `condition`/`sample`).
- ~5.6K-line Python monolith, TOML-as-schema → not a multi-tenant service shape.
- Migration pain and `schema_v` drift are already felt at single-lab scale.

**Concretely:** casetrack becomes the **single-lab edition and reference implementation** — the
domain-model prototype and one API client of the new core. The enterprise core is new,
event-log-first on a real database, sharing a documented event schema with casetrack so they
interoperate. The code's *lessons* are preserved; a single-file SQLite tool is not asked to become
a regulated multi-tenant platform.

*Fallback if greenfield can't be staffed:* evolve casetrack's storage backend to Postgres first as
a stepping stone — but treat that as a compromise, not the target.

## 4. Target architecture (layered)

### Layer 0 — Entity model (generalize past the three levels)

Adopt the **W3C PROV triple** as the spine: `Entity` / `Activity` / `Agent`.
- casetrack's patient → specimen → assay, and all artifacts → `Entity`.
- analyses / pipeline runs → `Activity`.
- operators / pipelines / services → `Agent`.

Entity *types* are data-driven, not a fixed enum. This removes the hardcoded-hierarchy limitation
**and** buys standards interop for free (Layer 3). It is also the correct fix for the whole
"custom hierarchy keys break tooling" bug class.

### Layer 1 — Event log (source of truth)

- Append-only, immutable, **hash-chained per stream** (each event carries the prior event's hash →
  tamper-evident *by construction*, not by convention).
- Events **signed by the acting agent**; actor identity captured *into* the log, so "who did what"
  is native.
- Postgres with trigger-enforced immutability is an acceptable start; the hash chain is what makes
  it *provable*. casetrack's `provenance.jsonl` is append-only-by-convention — which an auditor
  rejects.
- Optionally anchor chain heads periodically (RFC 3161 timestamps / transparency-log style) for
  third-party non-repudiation.
- **This single choice is what makes compliance cheap to add later** (see §7).

### Layer 2 — Projections (the three read models, CQRS)

- **Lineage graph** — serves catalog *and* reproducibility "what feeds what." Postgres recursive
  CTEs are fine to ~10⁵–10⁶ nodes; beyond that, a graph store or materialized adjacency.
- **Catalog / search index** — full-text + faceted metadata for discovery.
- **Staleness engine** — casetrack's cascade, but **do not keep it read-time at scale.** Read-time
  visited-set DAG traversal is O(graph) per query and dies past ~10⁶ nodes. Upgrade to
  **push-based incremental invalidation**: when an event flips a node stale, propagate and *store*
  the flag on affected descendants, indexed. Same three-flag semantics, materialized instead of
  recomputed.
- **Audit views** — filtered projections of the log for compliance reporting.

### Layer 3 — Interop (do not ship a dialect)

Because Layer 0 *is* PROV, adapters are near-mechanical:
- **Export:** PROV-O, RO-Crate / Workflow-Run RO-Crate, OpenLineage.
- **Import:** connectors from Nextflow (weblog / OpenLineage), Cromwell metadata, Airflow, Dagster.
  This is how the platform *enters* an enterprise that already owns a catalog — it feeds the
  catalog rather than trying to replace it on day one.

### Layer 4 — API & access

- REST + GraphQL over the projections.
- **RBAC / ABAC + row-level security + SSO (SAML / OIDC).**
- **Multi-tenancy as a first-class dimension** (tenant on every row + isolation strategy).

### Layer 5 — Clients

- Web app: dashboard + catalog UI + audit reports (casetrack's dashboard/query work ports here as a
  UI over the API).
- **casetrack CLI as one client among several.**

## 5. Deployment — one codebase, two shapes

- **Containerized, config-driven, no hard cloud dependencies.** K8s / Helm for multi-tenant SaaS;
  single-node compose / appliance for on-prem. "No hard cloud deps" also keeps the platform
  **air-gap-ready** for free — worth doing even though air-gap was not selected, because regulated
  on-prem customers drift into it.
- **Tenancy strategy:** row-level for SaaS density; a single-tenant on-prem deployment is just "one
  tenant." Do not fork the code per deployment mode.
- Pluggable IdP: cloud IdP for SaaS, LDAP / on-prem SSO for enterprise.

## 6. Regulated-grade cross-cutting (GxP / HIPAA / Part 11)

Design in from Layer 1, certify later:
- **Tamper-evidence** → the hash chain (Layer 1). Covered if built early.
- **E-signatures (21 CFR Part 11)** → signed events capturing meaning / intent; log already holds
  actor + timestamp.
- **The system must itself be validatable (CSV / IQ / OQ / PQ)** → versioned releases, a
  requirements-traceability matrix, an automated validation test pack. *A provenance platform needs
  its own provenance* — reproducible builds, documented change control.
- **GDPR erasure vs. immutable audit** (the sharp tension) → **crypto-shredding**: store PII / PHI
  encrypted under per-subject keys, keep the event *structure* immutable, honor erasure by
  destroying the key. Audit trail intact, subject data unrecoverable. Must be designed into Layer 1;
  an expensive retrofit otherwise.
- Retention policies, legal hold, chain of custody.

## 7. Phasing (all-three-at-once is the trap)

Compliance is *cheap to enable and expensive to certify* — bake the primitives early, certify late.

- **Phase 1 — Reproducibility + lineage on solid substrate.** Event-log core (hash-chain from day 1,
  even before it is needed), lineage projection, staleness engine ported from casetrack, one interop
  export (OpenLineage *or* PROV). Ships real value; casetrack users migrate.
- **Phase 2 — Catalog + platform.** Search / discovery UI, full API, RBAC / SSO, multi-tenancy, SaaS
  deployment. Ships the catalog product and the SaaS shape.
- **Phase 3 — Compliance certification.** E-signatures, validation pack, retention / erasure,
  Part 11 controls, SOC 2 / ISO 27001 evidence. Because the hash chain exists from Phase 1, this is
  hardening + paperwork, not re-architecture.

## 8. Risks & rejected alternatives

1. **Scope reality.** "All three + on-prem + SaaS + regulated" is a *platform*, not a tool —
   multi-year, multi-person. The phasing in §7 is what makes it survivable; be honest about
   staffing before committing.
2. **Staleness at scale.** Read-time recomputation (casetrack today) will not survive past ~10⁶
   nodes. Plan the push-based rewrite (§4, Layer 2) before it is a fire.
3. **SaaS multi-tenant + regulated on-prem from one codebase** is doable only if tenancy isolation
   and no-cloud-deps are designed in from the start; retrofitting either is a rewrite.
4. **Build-vs-buy on the catalog layer.** DataHub / OpenMetadata / Collibra already do
   discovery / lineage well. The defensible wedge is the *staleness / validity* model and
   reproducibility rigor, **not catalog UX.** Strongly consider *integrating with* an existing
   catalog (feed OpenLineage into DataHub) rather than rebuilding search — it shortens Phase 2
   dramatically and reframes scope downward. **This is the highest-leverage strategic decision.**

**Rejected:** building three separate subsystems (reproducibility, catalog, audit) with independent
schemas — collapses under duplication and consistency bugs; the §2 single-log thesis supersedes it.
**Rejected:** growing the SQLite monolith into the enterprise platform — the substrate wall in §3.

## 9. casetrack → PROV mapping (the interop bridge)

The home-grown model is not wasted — it is a PROV subset:

| casetrack concept | PROV term |
|---|---|
| patient / specimen / assay, artifacts, references | `prov:Entity` |
| analysis / pipeline run | `prov:Activity` |
| operator / pipeline / service | `prov:Agent` |
| `append` (produced `{analysis}_done`) | `prov:wasGeneratedBy` |
| `uses` (reference_usage, 0010) | `prov:used` |
| `derived-from` (artifact_derivation, 0011) | `prov:wasDerivedFrom` |

This mapping is the unlock for Layer 3 exports and shows the existing subsystems (0009–0013) are a
principled subset of a standard, not a dialect to be discarded.

## 10. Open questions / next step

- **The pivotal near-term decision is risk 4 combined with the §3 evolve/greenfield call.** If the
  wedge is validity + reproducibility (it is), the catalog may not need building at all — the
  platform becomes the *provenance / validity engine that feeds* the catalog the enterprise already
  owns. That reframes scope downward, which given risk 1 is good news.
- Deepest-leverage follow-on design: **the event-log + hash-chain schema** (Layer 1) — everything
  else projects from it.
- Concrete migration path from casetrack's current SQLite schema to the event-log core (a
  strangler-fig cutover plan) is the natural companion document if greenfield is chosen.
