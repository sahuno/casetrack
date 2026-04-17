# Proposal 0002 — QC events, censoring, and consent tracking

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | **Draft** (iteration 3, open for review) |
| **Date** | 2026-04-16 |
| **Target release** | v0.4.0 |
| **Depends on** | Proposal 0001 (SQLite-backed normalized hierarchy, shipped in v0.3.0) |
| **Target HPC** | IRIS @ MSKCC (WekaFS, SLURM, Apptainer) |

## 0. Accepted decisions (from iteration 3)

| # | Question | Accepted answer |
|---|---|---|
| 1 | Storage shape | **Hybrid**: `qc_events` audit table + materialized `qc_status` columns on each level (fast filters). |
| 2 | Granularity | **Three-level only** (patient / specimen / assay). Per-analysis censoring (e.g. "fine for modkit, bad for xtea on the same assay") is **deferred to a future proposal**. See §14 for rationale. |
| 3 | Consent revocation | **Distinct from QC**. Patient-level only, cascades at read, requires `--ethics-override --yes` to reverse. Default read paths exclude; `--include-censored` does *not* include consent-revoked — needs separate `--include-consent-revoked`. |
| 4 | SLURM auto-flag | **Via summary-TSV convention**. If a summarize script emits `qc_pass` (BOOLEAN) and/or `qc_fail_reason` (TEXT), `casetrack append` auto-writes a whole-assay `qc_events` row. No new pipeline commands required. |
| 5 | Reversal model | **Append-only**. `uncensor` writes `resolved_at` / `resolved_by` / `resolved_reason` on the existing row — never deletes. Reversal is the minority path (most flags are hard-fail-at-intake). |
| 6 | Patient consent attributes | **Both**: `patients.consent_status`, `patients.consent_date`, `patients.withdrawal_date` as typed columns for fast queries; `qc_events` keeps the immutable audit trail. |
| 7 | Legacy `qc_pass BOOLEAN` | **Deprecated**. Replaced by `qc_status`. A compatibility view or read-only computed column may be offered in v0.4.x. |
| 8 | Whole-cohort readiness | **Option A + Option C** (see §8): `casetrack status --usable` for pipeline driving + new `casetrack cohort` command for the grant-report view, including `--pair-by` to surface matched-pair completeness (e.g. tumor/normal pairs for a given assay type). |

## 1. Summary

Add a formal QC / censoring / consent subsystem to casetrack without violating its append-only, provenance-mandatory ethos.

Three layers:

1. **`qc_events` table** — append-only immutable audit log of every flag and its reversal. Stores who, when, why, from where (manual vs. SLURM vs. import), and the transaction ID linking to `provenance.jsonl`.
2. **Materialized `qc_status` columns** — a fast-filter summary on each level (`patients.qc_status`, `specimens.qc_status`, `assays.qc_status`). Derivable from `qc_events`; `casetrack recover` rebuilds them deterministically.
3. **Consent subsystem** — patient-level `consent_status` + `consent_date` + `withdrawal_date` columns with constrained enum; special cascade rules; ethics-override gate on reversal.

Scope note: this proposal intentionally flags exclusions at the **whole-entity level** (the whole assay, the whole specimen, the whole patient). Per-analysis exclusion — e.g. "this ONT-RNA-Seq is fine for expression quantification but its polyA integrity is bad for isoform calling" — is a real concern but deferred to a future proposal once a concrete motivating case appears (§14).

Integration points:
- Every read path (`status`, `rerun`, `dashboard`, `query`, `export`) learns QC-aware defaults.
- `casetrack append` learns to auto-flag from summary-TSV convention columns.
- New commands: `censor`, `uncensor`, `qc-history`, `cohort`.

## 2. Motivation

Casetrack today answers "*is this sample complete for analysis X?*" It does not answer "*is this sample **usable** for analysis X?*" — which is almost always the question PIs, biostatisticians, and pipeline rerunners actually care about.

Specific gaps in v0.3.1:

1. **No consent provenance.** A patient withdrawing consent has no canonical representation. Downstream tools that consume manifests will happily publish consent-revoked data unless every script hand-filters. Legal/IRB risk.
2. **No way to flag a failed assay.** Real scenario: patient HGSOC002 has paired tumor/normal specimens, each with ONT DNA-Seq, ONT-RNA-Seq, ATAC-Seq. The normal ONT-RNA-Seq failed library prep. Today the only way to express this is an ad-hoc column convention with no reason, no history, and no cross-pipeline enforcement.
3. **No audit trail for flags.** A `qc_pass BOOLEAN` column was proposed in §4 of proposal 0001, but it's a single mutable boolean — no reason, no timestamp, no history of "we flagged it, then re-sequenced, and it passed."
4. **`rerun` doesn't know about QC.** Today `casetrack rerun --analysis modkit` will resubmit SLURM jobs for a sample that we already know is never going to pass. Wasted cluster hours.
5. **No whole-cohort readiness view.** "*How many patients have a complete matched tumor/normal ONT-RNA-Seq pair where both halves pass QC?*" requires writing SQL by hand. The same failed normal from the HGSOC002 example silently taints the paired-design analysis unless someone remembers to exclude both halves.

## 3. Goals / non-goals

### Goals
1. Record *why* a sample/assay is excluded, with structured kinds (QC fail, warn, consent, protocol deviation, superseded, other).
2. Preserve the append-only / provenance-mandatory ethos — no destructive edits, every mutation logged twice (`qc_events` + `provenance.jsonl`), recoverable from provenance alone.
3. Support whole-entity exclusion at every hierarchy level (patient, specimen, assay).
4. Treat consent revocation with ethics-appropriate defaults: irreversible without opt-in, cascades across levels, visibly distinct in all UIs.
5. Make SLURM auto-flag zero-effort for pipelines that follow the summary-TSV convention.
6. Provide both pipeline-driving (`status --usable`) and cohort-readiness (`cohort`) views for the two distinct user personas, including pair-aware queries (matched tumor/normal readiness).

### Non-goals
1. Not a generic "data quality" framework. Continuous QC metrics (coverage, mapping rate, duplication) stay as regular analysis columns on `assays`. This proposal is only about the binary-ish usability decision derived from those metrics.
2. **Not per-analysis censoring in v0.4.** A future proposal will add this once a motivating case appears. Until then, flag the whole assay or don't flag at all.
3. Not building a review workflow / sign-off / approval chain. A lab manager who wants a two-person sign-off on consent revocation builds that above casetrack, not inside it.
4. Not changing what constitutes a `casetrack.toml` schema-wise beyond adding a `[qc]` block.
5. Not altering the Q5 concurrency tier from proposal 0001 — `qc_events` writes sit inside the same `BEGIN IMMEDIATE` envelope as every other mutation.
6. Not modelling paired designs as first-class hierarchy entities. `cohort --pair-by` works off existing metadata (e.g. `specimens.tissue_site`); it does not introduce a `pairs` table.

## 4. Data model

### 4.1 `qc_events` table

```sql
CREATE TABLE qc_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  level           TEXT NOT NULL CHECK(level IN ('patient','specimen','assay')),
  entity_id       TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK(kind IN (
                    'qc_fail',
                    'qc_warn',
                    'consent_revoked',       -- patient-level only (enforced at CLI, not DDL)
                    'protocol_deviation',
                    'superseded',
                    'other')),
  reason          TEXT NOT NULL,
  source          TEXT NOT NULL CHECK(source IN ('manual','slurm','import')),
  created_at      TEXT NOT NULL,
  created_by      TEXT NOT NULL,
  resolved_at     TEXT,                      -- NULL = still active
  resolved_by     TEXT,
  resolved_reason TEXT,
  transaction_id  TEXT NOT NULL              -- matches provenance.jsonl
);

CREATE INDEX idx_qc_events_entity
    ON qc_events(level, entity_id);

CREATE INDEX idx_qc_events_active
    ON qc_events(level, entity_id)
    WHERE resolved_at IS NULL;

CREATE INDEX idx_qc_events_kind
    ON qc_events(kind);
```

Immutable append-only. The only mutation on an existing row is the transition from `resolved_at IS NULL` → non-NULL, which is itself a logged `uncensor` transaction.

Each `(level, entity_id)` pair can have at most one active (unresolved) event per kind. Subsequent attempts to censor an already-censored entity for the same kind must either reference the existing event (no-op with warning) or fail until the prior one is resolved — enforced at the CLI.

### 4.2 Materialized fast-filter columns

Added to the three entity tables by `casetrack init` (or by a migration script for existing v0.3.x projects):

```sql
ALTER TABLE patients  ADD COLUMN qc_status TEXT
    CHECK (qc_status IN ('pass','warn','fail','censored','consent_revoked'))
    DEFAULT 'pass';

ALTER TABLE specimens ADD COLUMN qc_status TEXT
    CHECK (qc_status IN ('pass','warn','fail','censored'))  -- no consent_revoked here
    DEFAULT 'pass';

ALTER TABLE assays    ADD COLUMN qc_status TEXT
    CHECK (qc_status IN ('pass','warn','fail','censored'))
    DEFAULT 'pass';
```

No per-analysis `{analysis}_qc_status` columns in v0.4 — deferred (see §14).

These columns are a cache. `casetrack recover` rebuilds them by replaying `qc_events` in insertion order — so `provenance.jsonl` + `qc_events` (the latter is also reconstructible from provenance, since every row is logged there too) are the only source of truth.

### 4.3 Consent attributes on `patients`

```toml
[levels.patient.columns]
consent_status   = { type = "TEXT",
                     enum = ["consented","pending","revoked","withdrawn","deceased_pre_consent"],
                     default = "consented" }
consent_date     = { type = "DATE" }
withdrawal_date  = { type = "DATE" }
```

**Invariant** (enforced at CLI, not DDL, since DDL can't express cross-column constraints cleanly):
- If `consent_status = 'revoked'`, there MUST be a corresponding `qc_events` row with `kind = 'consent_revoked'`.
- `withdrawal_date` is non-NULL iff `consent_status IN ('revoked','withdrawn')`.

The two storage layers (columns + events) are kept in sync by writing both inside the same `BEGIN IMMEDIATE` transaction. `casetrack validate` checks the invariant and reports drift.

### 4.4 Cascade semantics (at read, not write)

An assay is **usable** iff all of the following are true:

| Level | Condition |
|---|---|
| Patient | `consent_status = 'consented'` AND `qc_status NOT IN ('fail','censored','consent_revoked')` |
| Specimen | `qc_status NOT IN ('fail','censored')` |
| Assay | `qc_status NOT IN ('fail','censored')` |

Status `'warn'` is treated as usable-with-caveat (propagates, doesn't exclude).

The read model is a single SQL view `_active` (§8.4). There is no per-analysis variant in v0.4 — if you need "usable for analysis X", it equals "usable" for all X.

### 4.5 Worked example — HGSOC002 failed normal ONT-RNA-Seq

Concrete scenario that drives this design:

```
patients:
  HGSOC002  consent_status=consented  qc_status=pass
  HGSOC006  consent_status=consented  qc_status=pass

specimens:
  HGSOC002 / tumor   qc_status=pass
  HGSOC002 / normal  qc_status=pass
  HGSOC006 / tumor   qc_status=pass
  HGSOC006 / normal  qc_status=pass

assays:
  HGSOC002 / tumor  / ONT-DNA    qc_status=pass
  HGSOC002 / tumor  / ONT-RNA    qc_status=pass
  HGSOC002 / tumor  / ATAC       qc_status=pass
  HGSOC002 / normal / ONT-DNA    qc_status=pass
  HGSOC002 / normal / ONT-RNA    qc_status=fail   ← qc_events: kind=qc_fail, reason="library prep failed"
  HGSOC002 / normal / ATAC       qc_status=pass
  HGSOC006 / * / *  (all 6)      qc_status=pass
```

After `casetrack censor --level assay --id "HGSOC002-normal-ONT-RNA" --kind qc_fail --reason "library prep failed"`:

- The single bad assay is excluded from every default read path (`status --usable`, `rerun`, `export`, `dashboard`, `query _active`).
- The sibling assays on the same `HGSOC002/normal` specimen (ONT-DNA, ATAC) remain usable.
- The matched `HGSOC002/tumor/ONT-RNA` is **still marked usable** on its own. Whether to drop it from a *paired* analysis is a cohort-level decision surfaced by `casetrack cohort --pair-by` (§8.3), not a QC flag.

## 5. CLI changes

### 5.1 New commands

```bash
# Manual censor — assay level (the common case)
casetrack censor --project-dir D --level assay \
    --id HGSOC002-normal-ONT-RNA \
    --kind qc_fail --reason "library prep failed, cDNA yield below threshold"

# Specimen-level censor (e.g. whole normal specimen contaminated)
casetrack censor --project-dir D --level specimen --id HGSOC002-normal \
    --kind protocol_deviation --reason "wrong fixation protocol"

# Consent revocation — patient-level only, enforced at CLI
casetrack censor --project-dir D --level patient --id HGSOC002 \
    --kind consent_revoked --reason "withdrew 2026-03-15" \
    --withdrawal-date 2026-03-15

# Bulk import from clinical handoff
casetrack censor --project-dir D --from intake_qc.tsv
# expected columns: level, entity_id, kind, reason

# Uncensor — resolves an active event
casetrack uncensor --project-dir D --event-id 42 \
    --reason "re-sequenced on 2026-04-10 batch, passes"

# Uncensor consent — requires extra gate
casetrack uncensor --project-dir D --event-id 17 \
    --ethics-override --yes \
    --reason "re-consent signed 2026-05-01, IRB ref 2026-042"

# History for an entity (all events, resolved or not)
casetrack qc-history --project-dir D --level assay --id HGSOC002-normal-ONT-RNA
casetrack qc-history --project-dir D --level patient --id HGSOC002 --include-cascaded

# Cohort readiness
casetrack cohort --project-dir D
casetrack cohort --project-dir D --assay-type ONT-RNA-Seq --pair-by tissue_site
casetrack cohort --project-dir D --fmt json
```

### 5.2 Changed commands

| Command | Change |
|---|---|
| `init` | Creates `qc_events` table + adds `qc_status` columns; writes default `[qc]` block in TOML |
| `append` | Auto-reads `qc_pass` / `qc_fail_reason` from summary TSV. If `qc_pass=False`, emits an assay-level `qc_events` row inside the same transaction and sets `assays.qc_status='fail'`. |
| `status` | New flags: `--usable`, `--include-censored`, `--include-consent-revoked`. Default shows only usable rows. |
| `rerun` | Default skips rows whose patient/specimen/assay QC status is `fail` or `censored`. `--force-censored` overrides (yells in stderr). |
| `dashboard` | New sections: "Censored" (all kinds except consent), "Consent-revoked" (separate block, red). Heatmap cells grey-out for non-usable assays with hover explanation. |
| `query` | New view exposed: `_active` (whole-entity QC + consent filter applied). Raw `_` stays unfiltered. |
| `export` | Default excludes QC-failed and consent-revoked. `--include-censored` includes QC-failed only. `--include-consent-revoked` is a separate flag. A one-line notice is printed to stderr summarizing what was filtered. |
| `validate` | New checks: `consent_status`/`qc_events` invariant (§4.3), active events whose target entity no longer exists, `qc_status` ↔ `qc_events` consistency |
| `schema` | `schema dump` includes `[qc]` and consent columns; `schema apply` can add new QC `kind` values to the TOML |
| `recover` | Replays `qc_events` writes in provenance order, rebuilds the materialized `qc_status` columns |

### 5.3 `casetrack.toml` additions

```toml
[qc]
kinds = [
  "qc_fail",
  "qc_warn",
  "consent_revoked",
  "protocol_deviation",
  "superseded",
  "other",
]
default_source   = "manual"
default_exclude  = ["fail", "censored", "consent_revoked"]  # in read paths
```

Teams can extend `kinds` to add domain-specific kinds (e.g. `batch_contaminated`). The SQLite `CHECK` on `qc_events.kind` is regenerated by `schema apply`.

## 6. SLURM auto-flag convention

The three-phase SLURM pattern stays unchanged:

1. Run tool (modkit, tldr, …)
2. Summarize to TSV
3. `casetrack append --results summary.tsv --analysis modkit`

Phase 3 is enriched: if `summary.tsv` contains any of

- `qc_pass` (BOOLEAN) — False triggers an assay-level `kind='qc_fail'` event
- `qc_fail_reason` (TEXT) — non-NULL used as the reason (default: `"slurm: qc_pass=False"`)
- `qc_warn` (BOOLEAN) — True triggers an assay-level `kind='qc_warn'` event

then `casetrack append` atomically writes:
- data columns for the analysis (as today)
- `{analysis}_done` column (as today)
- `assays.qc_status` bumped to `'fail'` or `'warn'`
- `qc_events` row with `source='slurm'`, `created_by='slurm:$SLURM_JOB_ID'`

All inside one `BEGIN IMMEDIATE`. If the append fails for any reason, neither data nor QC events land.

Note that the convention uses **bare** `qc_pass` / `qc_fail_reason` / `qc_warn` — not `{analysis}_qc_pass`. The flag is the summarize script's judgement: "this assay is bad, regardless of which analysis asked me." If the script decides the assay is usable (even if *this* analysis's numbers are unimpressive), it omits the columns or leaves `qc_pass=True`, and the assay stays unflagged.

Summarize scripts that don't emit these columns behave identically to today — no QC event is written, nothing is flagged.

Example summarize TSV:
```
assay_id                    modkit_mean_meth   qc_pass   qc_fail_reason
HGSOC002-tumor-ONT-RNA      0.72               True
HGSOC002-normal-ONT-RNA                        False     library prep failed (cDNA yield 8 ng, need >100)
HGSOC006-tumor-ONT-RNA      0.65               True
HGSOC006-normal-ONT-RNA     0.68               True
```

## 7. Consent revocation — special handling

### 7.1 Why it's special

Consent has legal/IRB weight that QC does not. Casetrack cannot delete (append-only), so the goal is: **make it impossible to accidentally publish consent-revoked data** using any default command.

### 7.2 Rules

1. **Level scope**: `kind = 'consent_revoked'` is allowed only at `level = 'patient'`. CLI enforces; DB allows (to keep the `CHECK` clean).
2. **Cascade at read**: a consent-revoked patient excludes all its specimens and all their assays from every default read path.
3. **Opt-in visibility**: `--include-censored` does NOT include consent-revoked data. Two separate flags. This is deliberate — researchers adding `--include-censored` to see "borderline QC" samples should never accidentally scoop in withdrawn patients.
4. **Reversal gate**: `casetrack uncensor` refuses to resolve a `consent_revoked` event unless `--ethics-override --yes` AND a `--reason` that mentions an IRB reference or re-consent date. Case-insensitive regex check.
5. **Dashboard**: own section, red accent, no data shown — just counts and patient IDs. No "click to drill in" affordance.
6. **Export**: refuses to include consent-revoked in any output format unless `--include-consent-revoked`. Prints an audit line to stderr naming every consent-revoked ID whose data was included.
7. **Provenance**: every consent-revoke / ethics-override entry is tagged with `"ethics": true` so a grep of `provenance.jsonl` trivially surfaces every consent transaction for audit.

### 7.3 Pre-enrollment censoring

A patient may be flagged before any data arrives (intake QC rejects the sample, consent withdrawn during screening). This is supported: call `casetrack register --level patient --id P001 --meta consent_status=revoked` followed by `casetrack censor ...`. A stub patient row exists with no specimens or assays attached.

## 8. Whole-cohort readiness & pipeline driving

### 8.1 `casetrack status --usable` (pipeline driving)

```
$ casetrack status --project-dir D --usable
  Usable assays:   383 / 387
    Complete:      312
    Pending:        71
  Excluded:          4
    QC-failed:       1   (HGSOC002-normal-ONT-RNA)
    Consent-rev:     3   (all assays of HGSOC099)
```

`--analysis X` filters by the specific analysis's completion status (existing behaviour), but the usability filter is the same for every X because QC lives at the assay level in v0.4.

Used daily by `rerun`, by pipeline drivers, by "what's left to run?" queries.

### 8.2 `casetrack cohort` (PI-facing readiness summary)

```
$ casetrack cohort --project-dir D
Cohort: msk_hgsoc_2026

  Patients:  50 total
    47 consented + active
     2 consent-revoked
     1 withdrawn

  Specimens: 142 total
    138 active
      4 protocol-deviation

  Assays:   387 total / 383 usable / 4 excluded

  Completion by analysis (usable assays only):

    ANALYSIS       COMPLETE   PENDING   TOTAL-USABLE
    modkit_meth       312        71         383
    tldr_L1           298        85         383
    xtea              310        73         383
```

Formats: `--fmt {table, tsv, json, md}`.

### 8.3 `casetrack cohort --pair-by` (paired-design readiness)

The HGSOC002 case from §4.5 — one half of a tumor/normal pair failed — is surfaced explicitly:

```
$ casetrack cohort --project-dir D --assay-type ONT-RNA-Seq --pair-by tissue_site
Assay type: ONT-RNA-Seq   (pair dimension: tissue_site = {tumor, normal})

  PATIENT     TUMOR    NORMAL   PAIR STATUS
  HGSOC002    pass     FAIL     broken  (drop for paired analysis)
  HGSOC006    pass     pass     complete
  ...

Summary:
  Complete matched pairs:  48
  Broken pairs:             1
  Singletons:               1  (only one tissue_site present)
```

`--pair-by` works off any column on `specimens` that partitions the specimens of a patient into distinct groups (here `tissue_site`). Extensible to other designs (`timepoint` for longitudinal, `region` for multi-region). A failed half surfaces as a broken pair without automatically censoring the passing half — the user decides whether to drop the patient from a paired analysis.

Additional flags:
- `--complete-only` → patients list whose pairs are complete (good for piping to analysis tooling)
- `--broken-only` → just the broken ones (to review)
- `--fmt {table, tsv, json}`

### 8.4 `query` views (power users)

```sql
-- Usable rows — patient consent OK, no QC fail anywhere up the hierarchy
SELECT * FROM _active;

-- Raw unfiltered — today's `_` view semantics
SELECT * FROM _;
```

Custom pair-aware queries stay in user hands via `query`; `cohort --pair-by` is sugar for the common case.

## 9. Provenance integration

Every mutation produces two records atomically:

1. A row in `qc_events`.
2. A JSONL line in `provenance.jsonl` with `action IN ('censor', 'uncensor', 'ethics_override')`.

Provenance entry example:

```json
{
  "action": "censor",
  "level": "assay",
  "entity_id": "HGSOC002-normal-ONT-RNA",
  "kind": "qc_fail",
  "reason": "library prep failed (cDNA yield 8 ng, need >100)",
  "source": "slurm",
  "transaction_id": "txn_20260416T174235_a1b2c3",
  "qc_event_id": 42,
  "sql": [
    "INSERT INTO qc_events(level, entity_id, kind, reason, source, ...) VALUES (...)",
    "UPDATE assays SET qc_status = 'fail' WHERE assay_id = 'HGSOC002-normal-ONT-RNA'"
  ],
  "schema_v_before": 7,
  "schema_v_after": 7,
  "timestamp": "2026-04-16T17:42:35",
  "user": "slurm:12345",
  "slurm_job_id": "12345",
  "git": {"commit": "...", "branch": "main", "dirty": false}
}
```

`casetrack recover` replays `action IN ('censor', 'uncensor', 'ethics_override')` alongside the data actions to reconstruct both `qc_events` and the materialized `qc_status` columns.

## 10. Migration from v0.3.x

A one-shot migration command ports existing v0.3.x projects to the QC model:

```bash
casetrack migrate-qc --project-dir D [--qc-pass-column qc_pass] [--dry-run]
```

Steps (one transaction):

1. Add `qc_events` table.
2. Add `qc_status` columns to `patients`, `specimens`, `assays`.
3. Add consent columns to `patients` (all default to `consented`).
4. If a legacy `qc_pass` column exists on `assays`, migrate values:
   - `qc_pass = TRUE` → `qc_status = 'pass'`
   - `qc_pass = FALSE` → `qc_status = 'fail'` + insert `qc_events` row with `kind='qc_fail'`, `reason='migrated from legacy qc_pass'`, `source='import'`
   - `qc_pass IS NULL` → `qc_status = 'pass'` (default; no event)
5. Drop `qc_pass` column (SQLite requires `ALTER TABLE ... DROP COLUMN` on 3.35+; otherwise table rewrite).
6. Bump `schema_v`.
7. Log one `action='migrate-qc'` provenance entry.

Backwards compatibility for the v0.3.x–v0.4.x window: an optional `qc_pass` **read-only view** over `qc_status = 'pass'` is provided for external scripts that predate this proposal.

## 11. Dashboard UX

Three visible additions:

1. **Header chips** next to the cohort counts: "2 consent-revoked" (red), "5 QC-failed" (amber), "3 QC-warn" (yellow).
2. **Per-analysis heatmap cells** get three visual states beyond "complete / pending":
   - `usable + complete`: full green
   - `usable + pending`: pale green
   - `QC-failed`: grey with red corner flag
   - `consent-revoked`: solid red, no hover
   - `warn`: yellow outline
3. **Dedicated "Excluded" section** near the provenance timeline, listing every active `qc_events` row with kind, reason, source, date. Filterable by level. Collapsed by default on cohorts > 50 patients.

A floating legend explains the visual encoding. Self-contained HTML, zero JS as today.

## 12. Open questions

| # | Question | Lean |
|---|---|---|
| Q1 | Should `warn` status propagate to downstream views or be purely informational? | Informational for now. Revisit if a team asks. |
| Q2 | How does `casetrack append` behave if the summary TSV has `qc_pass=False` for an assay already censored? | No-op with a stderr note. If the existing event was `resolved`, a new one is appended. |
| Q3 | Should `append` refuse to write data columns when `qc_pass=False`? | No. Still write the data (NaN or raw values), still set `{analysis}_done`, but also flag the assay. The data may be useful to understand the failure. |
| Q4 | Should `superseded` events be auto-emitted when a replicate is added? | No, too magic. Explicit `casetrack censor --kind superseded --reason "rep2 is canonical"` only. |
| Q5 | Time-based consent (e.g. "consent valid through 2028-12-31")? | Out of scope. Model as a `consent_expiry` column if a team needs it. Re-open if common. |
| Q6 | `cohort --pair-by` across >2 partition values (e.g. 3 timepoints)? | Full matrix: report patients with a complete set across all partitions. `--require 2 of 3` to allow partial sets. Defer until a real use case. |
| Q7 | Audit output: should `casetrack censor --from file.tsv` write one provenance entry or one per event? | One per event, same `transaction_id` for the batch. Keeps replay simple. |
| Q8 | Field `created_by`: format for humans vs. SLURM vs. imports? | Humans: `$USER`. SLURM: `slurm:$SLURM_JOB_ID`. Import: `import:$(basename FROM_FILE)`. Captured automatically by the CLI. |

## 13. Implementation plan (phased)

Same phased-rollout shape as proposal 0001.

### Phase α — v0.4.0-alpha (foundations)

- `qc_events` table DDL + `qc_status` column migrations
- `casetrack.toml` `[qc]` block parser
- `casetrack censor` / `uncensor` / `qc-history` commands
- `migrate-qc` one-shot
- Provenance entries + `recover` replay
- Tests: `test_qc_events.py`, `test_censor_cli.py`, `test_qc_recover.py`

### Phase β — v0.4.0-beta (read-path integration)

- `append` auto-flag from summary TSV
- `rerun`, `status --usable`, `export` defaults
- `validate` new invariants
- Dashboard visual changes
- Tests: `test_append_autoflag.py`, `test_rerun_skips_censored.py`, `test_export_qc_defaults.py`, `test_dashboard_qc_sections.py`

### Phase release — v0.4.0

- `casetrack cohort` command (single-analysis summary + `--pair-by` paired-design view)
- `query` view (`_active`)
- Consent semantics + ethics override
- Migration guide `docs/MIGRATION_v0.3_to_v0.4.md`
- README / CASETRACK_SYNOPSIS updates
- Tests: `test_cohort_command.py`, `test_cohort_pair_by.py`, `test_consent_revocation.py`, `test_ethics_override.py`, `test_compat_qc_pass.py`

### Success criteria

- All v0.3.x tests continue to pass (no regressions on flat or project modes).
- At least 50 new tests covering QC paths.
- `casetrack recover` round-trips a project with a non-trivial QC history byte-identical to the original DB.
- A real HGSOC cohort migrates via `migrate-qc` with zero data loss and a clean audit report.
- `cohort --pair-by tissue_site` correctly identifies the HGSOC002 broken-pair scenario from §4.5.
- CI green on Python 3.10–3.13.
- Dashboard renders correctly for cohorts with 0, 1, and ≥2 consent-revoked patients.

## 14. Alternatives considered

- **Single `qc_status` column, no events table.** Rejected — loses the "why" and reversal history, which is the entire reason this proposal exists.
- **Store QC flags only in `provenance.jsonl`, no `qc_events` table.** Rejected — read paths would need to scan JSONL on every query. Events table is an indexed materialized view of the relevant subset of provenance.
- **Make consent just another `kind` of QC event with no special handling.** Rejected — the legal/ethics semantics are genuinely different. Two separate opt-in flags (`--include-censored` vs `--include-consent-revoked`) is a small cost for preventing accidental publication of withdrawn data.
- **Delete consent-revoked rows entirely.** Rejected — violates append-only; also doesn't match reality (institutions generally retain records, they just suppress from analysis).
- **Per-analysis censoring in v0.4 (iteration 2's design).** **Deferred**, not rejected. The first draft added `qc_events.analysis` (nullable) and dynamic `{analysis}_qc_status` columns on `assays`. The motivating cases to date (e.g. HGSOC002 failed normal ONT-RNA-Seq, §4.5) are whole-assay failures — the library prep didn't work, so every downstream analysis of that assay is equally unusable. The per-analysis complexity (new column surface, new CLI flags, per-analysis view templates) isn't justified until a concrete case appears where an assay passes one analysis's QC and fails another's. A future proposal (0003+) will add it when that case lands.
- **Model paired designs (tumor/normal, longitudinal) as first-class hierarchy entities.** Rejected — pairs are an analysis-time concept, not a biology-at-collection-time concept. The same specimen can be a member of multiple paired designs (tumor/normal for WGS; pre-treatment/post-treatment for RNA). A pair table would have to denormalize that. `cohort --pair-by` reads pairing off existing `specimens` metadata instead — simpler and more flexible.
- **Cascade QC failure from one half of a pair to the other automatically.** Rejected — the user's answer was explicitly "if we want". A broken pair is a cohort-level decision, not a QC event. `cohort --pair-by` surfaces it; the user decides whether to exclude the intact half.
