# Migrating from casetrack v0.3.x to v0.4.0

v0.4 adds the QC / censoring / consent subsystem described in
[proposal 0002](proposals/0002-qc-events-and-censoring.md). This guide walks
through upgrading an existing v0.3 project in place.

## What changes

**Additive. No v0.3 data is removed or re-shaped.**

New objects inside `casetrack.db`:

- `qc_events` table — append-only audit log of every QC / consent event.
- `qc_status` column on `patients`, `specimens`, `assays` (default `pass`).
- `consent_status`, `consent_date`, `withdrawal_date` columns on `patients`
  (default `consent_status='consented'`).

New commands:

- `casetrack censor` / `uncensor` / `qc-history` — manual QC.
- `casetrack cohort` — readiness summary (`--pair-by` for paired designs).
- `casetrack migrate-qc` — the one-shot upgrade covered here.

Changed defaults on existing commands:

- `append` refuses to land data on a censored entity (exit 2) unless
  `--force-append-on-censored --yes`. It also reads a new TSV convention
  (`qc_pass` / `qc_fail_reason` / `qc_warn` columns — see §6 of the proposal).
- `rerun` skips censored rows by default (`--force-censored` to include).
- `status` has new flags `--usable`, `--include-censored`,
  `--include-consent-revoked`; default counts exclude fail + consent-revoked.
- `export` default excludes fail + consent-revoked and prints a stderr audit
  line. Opt back in with `--include-censored` / `--include-consent-revoked`.
- `query` exposes an `_active` DuckDB view alongside the raw `_` view.
- `validate` checks QC ↔ events consistency + consent invariants.
- `dashboard` shows QC chips and an "Excluded (active QC events)" section.
- `recover` replays `censor` / `uncensor` / `ethics_override` / `migrate_qc`.

## Upgrade — one command

```bash
casetrack migrate-qc --project-dir /path/to/your_project
```

What it does, inside a single `BEGIN IMMEDIATE` transaction:

1. Creates `qc_events` (+ indexes) if absent.
2. Adds `qc_status` to `patients`, `specimens`, `assays`.
3. Adds `consent_status` / `consent_date` / `withdrawal_date` to `patients`.
4. If a legacy `qc_pass BOOLEAN` column exists on `assays`, for each row:
   - `qc_pass=TRUE` → `qc_status='pass'` (no event).
   - `qc_pass=FALSE` → `qc_status='fail'` + an active `qc_events` row with
     `kind='qc_fail'`, `reason='migrated from legacy qc_pass'`,
     `source='import'`.
   - `qc_pass IS NULL` → `qc_status='pass'` (default).
5. Drops the `qc_pass` column.
6. Appends a default `[qc]` / `[qc.kind_scopes]` block to `casetrack.toml`.
7. Writes a single `action='migrate_qc'` provenance entry summarizing the
   executed DDL and the migrated-row list.

### Preview first

```bash
casetrack migrate-qc --project-dir DIR --dry-run
```

No changes — prints the migration plan.

### Override the legacy column name

Default is `qc_pass`. If your project used a different name:

```bash
casetrack migrate-qc --project-dir DIR --qc-pass-column pass_qc
```

## Validate after migration

```bash
casetrack validate --project-dir DIR
```

Should report no issues. If it surfaces any `consent invariant` or `qc_status
mismatch` entries, fix them manually and re-run — those indicate pre-existing
drift between the DB and the expected v0.4 shape.

## Rollback

`migrate-qc` is irreversible on the DB (columns get added, legacy column
dropped). The safe rollback path is:

1. Restore the pre-migration `casetrack.db` backup.
2. Optionally `casetrack recover --project-dir DIR --force` to rebuild the DB
   from `provenance.jsonl` — which stops at the `init_project` entry unless
   you also truncate the `migrate_qc` line.

## What stays the same

- Your existing analysis columns on `assays` / `specimens` / `patients`.
- All prior `_done` columns and their values.
- `provenance.jsonl` — unchanged, only appended to.
- Flat-mode (`--manifest`) projects are out of scope; they stay on v0.3's
  deprecation trajectory and will be removed in v1.0.

## Backwards compatibility during the v0.4 window

- Existing v0.3 tooling that reads `casetrack.db` directly still works —
  new columns are additive and old columns are unchanged except for the
  dropped `qc_pass`.
- `casetrack recover` replays v0.3-era provenance entries the same way it
  always did, and now also handles the v0.4 QC actions.

## Example — end-to-end on a real HGSOC project

```bash
# 1. Back up.
cp -r hgsoc_2026 hgsoc_2026.bak

# 2. Migrate.
casetrack migrate-qc --project-dir hgsoc_2026

# 3. Manually censor the known-bad assay (if it wasn't already in qc_pass).
casetrack censor --project-dir hgsoc_2026 \
    --level assay --id HGSOC002-normal-ONT-RNA \
    --kind library_prep_failed \
    --reason "library prep failed, cDNA yield 8 ng, need >100"

# 4. Verify paired-design readiness — the HGSOC002 broken pair should show up.
casetrack cohort --project-dir hgsoc_2026 \
    --assay-type ONT-RNA-Seq --pair-by tissue_site

# 5. Validate, regenerate dashboard.
casetrack validate --project-dir hgsoc_2026
casetrack dashboard --project-dir hgsoc_2026 --output dashboard.html
```
