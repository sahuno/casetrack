# Proposal 0002 — QC events, censoring, and consent tracking

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | **Draft** (iteration 2, open for review) |
| **Date** | 2026-04-16 |
| **Target release** | v0.4.0 |
| **Depends on** | Proposal 0001 (SQLite-backed normalized hierarchy, shipped in v0.3.0) |
| **Target HPC** | IRIS @ MSKCC (WekaFS, SLURM, Apptainer) |

## 0. Accepted decisions (from iteration 2)

| # | Question | Accepted answer |
|---|---|---|
| 1 | Storage shape | **Hybrid**: `qc_events` audit table + materialized `qc_status` columns on each level (fast filters). |
| 2 | Per-analysis censoring | **First-class**. `qc_events.analysis` is nullable; `NULL` = whole-entity, non-NULL = per-analysis. Per-analysis fast-filter columns added to `assays` on first use via `ALTER TABLE`. |
| 3 | Consent revocation | **Distinct from QC**. Patient-level only, cascades at read, requires `--ethics-override --yes` to reverse. Default read paths exclude; `--include-censored` does *not* include consent-revoked — needs separate `--include-consent-revoked`. |
| 4 | SLURM auto-flag | **Via summary-TSV convention**. If a summarize script emits `{analysis}_qc_pass` (BOOLEAN) and/or `{analysis}_qc_fail_reason` (TEXT), `casetrack append` auto-writes a `qc_events` row. No new pipeline commands required. |
| 5 | Reversal model | **Append-only**. `uncensor` writes `resolved_at` / `resolved_by` / `resolved_reason` on the existing row — never deletes. Reversal is the minority path (most flags are hard-fail-at-intake). |
| 6 | Patient consent attributes | **Both**: `patients.consent_status`, `patients.consent_date`, `patients.withdrawal_date` as typed columns for fast queries; `qc_events` keeps the immutable audit trail. |
| 7 | Legacy `qc_pass BOOLEAN` | **Deprecated**. Replaced by `qc_status`. A compatibility view or read-only computed column may be offered in v0.4.x. |
| 8 | Whole-cohort readiness | **Option A + Option C** (see §8): `casetrack status --usable --analysis X` for pipeline driving + new `casetrack cohort` command for the grant-report view. |

## 1. Summary

Add a formal QC / censoring / consent subsystem to casetrack without violating its append-only, provenance-mandatory ethos.

Three layers:

1. **`qc_events` table** — append-only immutable audit log of every flag and its reversal. Stores who, when, why, from where (manual vs. SLURM vs. import), and the transaction ID linking to `provenance.jsonl`.
2. **Materialized `qc_status` columns** — a fast-filter summary on each level (`patients.qc_status`, `specimens.qc_status`, `assays.qc_status`) plus per-analysis columns on assays (`assays.{analysis}_qc_status`). Derivable from `qc_events`; `casetrack recover` rebuilds them deterministically.
3. **Consent subsystem** — patient-level `consent_status` + `consent_date` + `withdrawal_date` columns with constrained enum; special cascade rules; ethics-override gate on reversal.

Integration points:
- Every read path (`status`, `rerun`, `dashboard`, `query`, `export`) learns QC-aware defaults.
- `casetrack append` learns to auto-flag from summary-TSV convention columns.
- New commands: `censor`, `uncensor`, `qc-history`, `cohort`.

## 2. Motivation

Casetrack today answers "*is this sample complete for analysis X?*" It does not answer "*is this sample **usable** for analysis X?*" — which is almost always the question PIs, biostatisticians, and pipeline rerunners actually care about.

Specific gaps in v0.3.1:

1. **No consent provenance.** A patient withdrawing consent has no canonical representation. Downstream tools that consume manifests will happily publish consent-revoked data unless every script hand-filters. Legal/IRB risk.
2. **No per-analysis exclusion.** An assay can be "fine for ATAC, oxidation-damaged for WGS." Today the only way to express this is ad-hoc `{analysis}_bad` columns with no convention, no history, and no cross-pipeline enforcement.
3. **No audit trail for flags.** A `qc_pass BOOLEAN` column was proposed in §4 of proposal 0001, but it's a single mutable boolean — no reason, no timestamp, no history of "we flagged it, then re-sequenced, and it passed."
4. **`rerun` doesn't know about QC.** Today `casetrack rerun --analysis modkit` will resubmit SLURM jobs for a sample that we already know is never going to pass. Wasted cluster hours.
5. **No whole-cohort readiness view.** "*How many patients are usable for the modkit+xtea joint analysis?*" requires writing SQL by hand.

## 3. Goals / non-goals

### Goals
1. Record *why* a sample/assay/analysis is excluded, with structured kinds (QC fail, warn, consent, protocol deviation, superseded, other).
2. Preserve the append-only / provenance-mandatory ethos — no destructive edits, every mutation logged twice (`qc_events` + `provenance.jsonl`), recoverable from provenance alone.
3. Support per-analysis exclusion cleanly — a sample can be excluded for modkit while remaining usable for tldr.
4. Treat consent revocation with ethics-appropriate defaults: irreversible without opt-in, cascades across levels, visibly distinct in all UIs.
5. Make SLURM auto-flag zero-effort for pipelines that follow the summary-TSV convention.
6. Provide both pipeline-driving (`status --usable`) and cohort-readiness (`cohort`) views for the two distinct user personas.

### Non-goals
1. Not a generic "data quality" framework. Continuous QC metrics (coverage, mapping rate, duplication) stay as regular analysis columns on `assays`. This proposal is only about the binary-ish usability decision derived from those metrics.
2. Not building a review workflow / sign-off / approval chain. A lab manager who wants a two-person sign-off on consent revocation builds that above casetrack, not inside it.
3. Not changing what constitutes a `casetrack.toml` schema-wise beyond adding a `[qc]` block.
4. Not altering the Q5 concurrency tier from proposal 0001 — `qc_events` writes sit inside the same `BEGIN IMMEDIATE` envelope as every other mutation.

## 4. Data model

### 4.1 `qc_events` table

```sql
CREATE TABLE qc_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  level           TEXT NOT NULL CHECK(level IN ('patient','specimen','assay')),
  entity_id       TEXT NOT NULL,
  analysis        TEXT,                      -- NULL = whole-entity; non-NULL = per-analysis
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
    ON qc_events(level, entity_id, analysis)
    WHERE resolved_at IS NULL;

CREATE INDEX idx_qc_events_kind
    ON qc_events(kind);
```

Immutable append-only. The only mutation on an existing row is the transition from `resolved_at IS NULL` → non-NULL, which is itself a logged `uncensor` transaction.

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

Per-analysis columns are added lazily on first use:

```sql
ALTER TABLE assays ADD COLUMN "modkit_qc_status" TEXT
    CHECK ("modkit_qc_status" IN ('pass','warn','fail'));
```

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

An entity is **usable for analysis X** iff all of the following are true:

| Level | Condition |
|---|---|
| Patient | `consent_status = 'consented'` AND `qc_status NOT IN ('fail','censored','consent_revoked')` |
| Specimen | `qc_status NOT IN ('fail','censored')` |
| Assay | `qc_status NOT IN ('fail','censored')` |
| Per-analysis | `{X}_qc_status IS NULL OR {X}_qc_status = 'pass'` |

Status `'warn'` is treated as usable-with-caveat (propagates, doesn't exclude).

The read model is a view per-analysis. Concrete implementation: a DuckDB table-valued function, or a stored SQL pattern the CLI templates dynamically.

## 5. CLI changes

### 5.1 New commands

```bash
# Manual censor — whole entity
casetrack censor --project-dir D --level assay --id A001 \
    --kind qc_fail --reason "low coverage: 2.3x on WGS"

# Manual censor — per analysis
casetrack censor --project-dir D --level assay --id A001 \
    --analysis modkit --kind qc_fail \
    --reason "oxidation damage"

# Consent revocation — patient-level only, enforced
casetrack censor --project-dir D --level patient --id P001 \
    --kind consent_revoked --reason "withdrew 2026-03-15" \
    --withdrawal-date 2026-03-15

# Bulk import from clinical handoff
casetrack censor --project-dir D --from intake_qc.tsv
# expected columns: level, entity_id, analysis, kind, reason

# Uncensor — resolves an active event
casetrack uncensor --project-dir D --event-id 42 \
    --reason "re-sequenced on 2026-04-10 batch, passes"

# Uncensor consent — requires extra gate
casetrack uncensor --project-dir D --event-id 17 \
    --ethics-override --yes \
    --reason "re-consent signed 2026-05-01, IRB ref 2026-042"

# History for an entity (all events, resolved or not)
casetrack qc-history --project-dir D --level assay --id A001
casetrack qc-history --project-dir D --level patient --id P001 --include-cascaded

# Cohort readiness (new, replaces grant-report hand-SQL)
casetrack cohort --project-dir D
casetrack cohort --project-dir D --analysis modkit
casetrack cohort --project-dir D --fmt json
```

### 5.2 Changed commands

| Command | Change |
|---|---|
| `init` | Creates `qc_events` table + adds `qc_status` columns; writes default `[qc]` block in TOML |
| `append` | Auto-reads `{analysis}_qc_pass` / `{analysis}_qc_fail_reason` from summary TSV. If present, emits `qc_events` row inside the same transaction. Also bumps `assays.{analysis}_qc_status`. |
| `status` | New flags: `--usable`, `--analysis X`, `--include-censored`, `--include-consent-revoked`. Default shows only usable rows. |
| `rerun` | Default skips rows that fail QC for the target analysis. `--force-censored` overrides (yells in stderr). |
| `dashboard` | New sections: "Censored" (all kinds except consent), "Consent-revoked" (separate block, red). Per-analysis heatmaps grey-out non-usable cells with hover explanation. |
| `query` | New views exposed: `_active` (whole-entity filter), plus per-analysis filter via templated view `_active_{analysis}`. Raw `_` still unfiltered. |
| `export` | Default excludes QC-failed and consent-revoked. `--include-censored` includes QC-failed only. `--include-consent-revoked` is a separate flag. A one-line notice is printed to stderr summarizing what was filtered. |
| `validate` | New checks: `consent_status`/`qc_events` invariant (§4.3), orphan `{analysis}_qc_status` columns, active events whose target entity no longer exists |
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

Phase 3 is enriched: if `summary.tsv` contains either

- `{analysis}_qc_pass` (BOOLEAN) — False triggers `kind='qc_fail'`
- `{analysis}_qc_fail_reason` (TEXT) — non-NULL used as the reason
- `{analysis}_qc_warn` (BOOLEAN) — True triggers `kind='qc_warn'`

then `casetrack append` atomically writes:
- `{analysis}_*` data columns (as today)
- `{analysis}_done` column (as today)
- `{analysis}_qc_status` column value
- `qc_events` row with `source='slurm'`, `created_by='slurm:$SLURM_JOB_ID'`

All inside one `BEGIN IMMEDIATE`. If the append fails for any reason, neither data nor QC events land.

Summarize scripts that don't emit these columns behave identically to today — no QC event is written, nothing is flagged.

Example summarize TSV:
```
assay_id       modkit_mean_meth   modkit_qc_pass   modkit_qc_fail_reason
A001           0.72               True
A002                               False            coverage=2.3x
A003           0.65               True
A004                               False            ambiguous_calls > 15%
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

### 8.1 `casetrack status --usable --analysis X` (pipeline driving)

```
$ casetrack status --project-dir D --analysis modkit --usable
Analysis: modkit
  Usable assays:    43 / 50
    Complete:       38
    Pending:         5
  Excluded:         7
    QC-failed:       4
    Censored:        1
    Consent-rev:     2
```

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

  Assays:    387 total
    Per-analysis usability:

    ANALYSIS       USABLE   QC-FAIL   PENDING   CONSENT-REV   TOTAL
    modkit_meth       43        4         5            2        50
    tldr_L1           38       10         2            2        50
    xtea              47        1         2            2        50
    cn_consensus      45        3         1            1        50

  Cross-analysis joint readiness:
    modkit + tldr:         36 patients usable for both
    modkit + tldr + xtea:  35 patients usable for all three
```

The "joint readiness" block is the one biostatisticians always ask for and always has to be hand-SQLed. Making it a subcommand is the whole point.

Formats: `--fmt {table, tsv, json, md}`.

### 8.3 `query` views (power users)

```sql
-- Usable rows, any analysis
SELECT * FROM _active;

-- Usable rows for a specific analysis
-- (implemented as a templated view or parameterized query)
SELECT * FROM _active_for('modkit');

-- Raw unfiltered — today's `_` view semantics
SELECT * FROM _;
```

## 9. Provenance integration

Every mutation produces two records atomically:

1. A row in `qc_events`.
2. A JSONL line in `provenance.jsonl` with `action IN ('censor', 'uncensor', 'ethics_override')`.

Provenance entry example:

```json
{
  "action": "censor",
  "level": "assay",
  "entity_id": "P001-LOV-WGS-1",
  "analysis": "modkit",
  "kind": "qc_fail",
  "reason": "oxidation damage, coverage 2.3x",
  "source": "slurm",
  "transaction_id": "txn_20260416T174235_a1b2c3",
  "qc_event_id": 42,
  "sql": [
    "INSERT INTO qc_events(level, entity_id, analysis, kind, reason, source, ...) VALUES (...)",
    "UPDATE assays SET modkit_qc_status = 'fail' WHERE assay_id = 'P001-LOV-WGS-1'"
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
| Q2 | Do per-analysis `_qc_status` columns live on `assays` only, or can they appear at specimen/patient level too (e.g. specimen-level CN consensus)? | Per-level. If `--level specimen --analysis cn_consensus`, the column lands on `specimens`. Matches existing `--level` routing for data columns. |
| Q3 | How does `casetrack append` behave if the summary TSV has `{analysis}_qc_pass` values that contradict an existing event? | Refuse; require `--overwrite-qc` flag. Prevents silent overwrites from re-runs. |
| Q4 | Should `superseded` events be auto-emitted when a replicate is added? | No, too magic. Explicit `casetrack censor --kind superseded --reason "rep2 is canonical"` only. |
| Q5 | Time-based consent (e.g. "consent valid through 2028-12-31")? | Out of scope. Model as a `consent_expiry` column if a team needs it. Re-open if common. |
| Q6 | Should `cohort` compute joint readiness for all subset combinations or only user-requested ones? | User-requested via `--joint modkit,tldr`. Default shows single-analysis table only. |
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

- `casetrack cohort` command (both single-analysis and joint)
- `query` views (`_active`, `_active_for`)
- Consent semantics + ethics override
- Migration guide `docs/MIGRATION_v0.3_to_v0.4.md`
- README / CASETRACK_SYNOPSIS updates
- Tests: `test_cohort_command.py`, `test_consent_revocation.py`, `test_ethics_override.py`, `test_compat_qc_pass.py`

### Success criteria

- All v0.3.x tests continue to pass (no regressions on flat or project modes).
- At least 60 new tests covering QC paths.
- `casetrack recover` round-trips a project with a non-trivial QC history byte-identical to the original DB.
- A real HGSOC cohort migrates via `migrate-qc` with zero data loss and a clean audit report.
- CI green on Python 3.10–3.13.
- Dashboard renders correctly for cohorts with 0, 1, and ≥2 consent-revoked patients.

## 14. Alternatives considered

- **Single `qc_status` column, no events table.** Rejected — loses the "why" and reversal history, which is the entire reason this proposal exists.
- **Store QC flags only in `provenance.jsonl`, no `qc_events` table.** Rejected — read paths would need to scan JSONL on every query. Events table is an indexed materialized view of the relevant subset of provenance.
- **Make consent just another `kind` of QC event with no special handling.** Rejected — the legal/ethics semantics are genuinely different. Two separate opt-in flags (`--include-censored` vs `--include-consent-revoked`) is a small cost for preventing accidental publication of withdrawn data.
- **Delete consent-revoked rows entirely.** Rejected — violates append-only; also doesn't match reality (institutions generally retain records, they just suppress from analysis).
- **Per-(assay, analysis) junction table instead of `{analysis}_qc_status` columns.** Rejected for query ergonomics — the column approach keeps `WHERE modkit_qc_status = 'pass'` as a trivial predicate and mirrors the existing `{analysis}_done` pattern.
