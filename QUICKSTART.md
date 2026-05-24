# Quickstart — first 15 minutes with casetrack

This walks you from a fresh clone to a working casetrack project tracking a
3-sample tumor/normal cohort, recording an analysis result, and querying it
with SQL. No prior knowledge of the tool assumed.

Pre-reqs: `git`, Python ≥ 3.10, `pip`.

---

## 1. Install (1 min)

```bash
git clone https://github.com/sahuno/casetrack.git
cd casetrack
pip install -e ".[all]" --user
casetrack --version       # confirms 0.11.0
```

(PyPI/conda packaging is on the roadmap. For now, editable install from the
clone is the supported path.)

---

## 2. Initialize a project (2 min)

```bash
cd /tmp
casetrack init --project-dir ./demo --project-name demo
cd demo
```

You'll get a scaffold with `casetrack.toml` (the schema declaration — edit
before registering), `casetrack.db` (SQLite, auto-created, gitignored),
`provenance.jsonl` (append-only audit log), and ~16 standard project leaf
directories (`data/`, `results/`, `logs/`, `scripts/`, etc.). The schema
declares **three levels**: `patients → specimens → assays`. Open
`casetrack.toml` and skim the `[levels.*.columns]` blocks — they already
declare `patient_id`, `specimen_id`, `assay_id`, and a required `assay_type`.

For this quickstart you'll add two more columns and one analysis block:

```bash
# add tissue_site to the existing specimen-columns block
sed -i '/^\[levels\.specimen\.columns\]/a tissue_site = { type = "TEXT" }' casetrack.toml

# add pod5_path to the existing assay-columns block
sed -i '/^\[levels\.assay\.columns\]/a pod5_path   = { type = "TEXT" }' casetrack.toml

# declare the samtools_sort analysis at the specimen level
cat >> casetrack.toml <<'TOML'

[analyses.samtools_sort]
level         = "specimen"
column_prefix = "sort"
summary_tsv   = "samtools_sort_summary.tsv"
TOML

casetrack schema apply --project-dir .
# → "Applied 2 schema change(s); schema_v 1 → 2"
#   + specimens.tissue_site TEXT
#   + assays.pod5_path TEXT
```

---

## 3. Load a cohort in one shot (3 min)

Write a wide sample sheet — one row per assay, all three levels' columns in
the same TSV:

```bash
cat > cohort.tsv <<'TSV'
patient_id	specimen_id	assay_id	tissue_site	assay_type	pod5_path
P1	P1_tumor	P1_tumor_a1	tumor	ONT_WGS	/data/pod5/P1_tumor_a1/
P1	P1_normal	P1_normal_a1	normal	ONT_WGS	/data/pod5/P1_normal_a1/
P2	P2_tumor	P2_tumor_a1	tumor	ONT_WGS	/data/pod5/P2_tumor_a1/
P2	P2_normal	P2_normal_a1	normal	ONT_WGS	/data/pod5/P2_normal_a1/
P3	P3_tumor	P3_tumor_a1	tumor	ONT_WGS	/data/pod5/P3_tumor_a1/
P3	P3_normal	P3_normal_a1	normal	ONT_WGS	/data/pod5/P3_normal_a1/
TSV

casetrack register-cohort --project-dir . --samplesheet cohort.tsv --dry-run
# inspect what it'll do, then commit:
casetrack register-cohort --project-dir . --samplesheet cohort.tsv
```

That single command upserts 3 patients, 6 specimens, and 6 assays in one
atomic transaction.

Sanity check:

```bash
casetrack status --project-dir .
# → Counts: patients=3, specimens=6, assays=6
```

---

## 4. Record an analysis result (3 min)

Pretend you just ran `samtools sort` on the 6 specimens. Write the kind of
per-entity summary TSV your wrapper would produce:

```bash
cat > /tmp/sort_summary.tsv <<'TSV'
specimen_id	sorted_bam_path	sorted_bam_size_bytes	n_reads
P1_tumor	/data/processed/hg38/P1/P1_tumor.hg38.sorted.bam	85134617284	142593118
P1_normal	/data/processed/hg38/P1/P1_normal.hg38.sorted.bam	71028346811	118429475
P2_tumor	/data/processed/hg38/P2/P2_tumor.hg38.sorted.bam	88241973506	146852019
P2_normal	/data/processed/hg38/P2/P2_normal.hg38.sorted.bam	73194820137	121937046
P3_tumor	/data/processed/hg38/P3/P3_tumor.hg38.sorted.bam	83507419628	139184307
P3_normal	/data/processed/hg38/P3/P3_normal.hg38.sorted.bam	70283956104	117139594
TSV

casetrack append --project-dir . \
  --level specimen \
  --analysis samtools_sort \
  --column-prefix sort \
  --results /tmp/sort_summary.tsv \
  --overwrite
# → Appended 'samtools_sort' to 6 specimen row(s) (+4 new columns).
```

Three things to internalize:
- `--overwrite` is **almost always what you want** for analysis results. Default
  is fill-only — existing non-NULL values are never updated, so a rerun
  silently no-ops at the DB level.
- `--level specimen` is required because `analysis_defaults.default_level` is
  `assay` in the template; explicit `--analysis` invocations don't auto-pick
  up the level from `[analyses.samtools_sort].level`.
- `--column-prefix sort` is also required for explicit `--analysis`
  invocations. (The TOML `column_prefix` field auto-applies only on the
  Nextflow `--infer-from-path` workflow, where the analysis name + level +
  prefix are all derived together from the results directory layout.)

Result: each TSV column lands prefixed in the DB — `sort_sorted_bam_path`,
`sort_n_reads`, etc. — plus a `samtools_sort_done` timestamp.

Re-run `casetrack status`:

```
samtools_sort   specimen   6   6   100.0%   ██████████
```

---

## 5. Query the DB (2 min)

casetrack queries are real SQL via DuckDB. The SQL goes as a **positional
argument** (not `--sql`). Two views are pre-built:

- `_active` — applies the QC/consent cascade automatically (excludes censored
  or consent-revoked entities)
- `_cohort_artifacts` — staleness-aware view over cohort-level outputs (SKILL.md §15)

Show the sorted tumor specimens with their stats:

```bash
casetrack query --project-dir . "
  SELECT s.specimen_id, s.sort_n_reads, s.sort_sorted_bam_size_bytes
  FROM specimens s
  WHERE s.tissue_site = 'tumor' AND s.samtools_sort_done IS NOT NULL
  ORDER BY s.sort_n_reads DESC
"
# → 3 rows: P2_tumor (146.8M reads), P1_tumor (142.6M), P3_tumor (139.2M)
```

**The DB as work queue.** Flip the predicate to `samtools_sort_done IS NULL`
on a real cohort where only some specimens have run, and the query returns
exactly the pending set — pipe to CSV and you have a Nextflow samplesheet
that auto-shrinks as the analysis completes. (Note: the analysis column only
exists once you've run `append` at least once, so this pattern lights up
after your first batch.)

---

## 6. Flag a bad sample (1 min)

QC events are append-only — `censor` adds an event and flips the
`qc_status`; `uncensor` records a resolution but never deletes.

```bash
# Hold a sample with a temporary QC warn (e.g. pod5 rsync incomplete)
casetrack censor --project-dir . \
  --level assay --id P3_tumor_a1 \
  --kind qc_warn --reason "pod5 rsync still in progress"

# Lift the hold when resolved
casetrack uncensor --project-dir . \
  --level assay --id P3_tumor_a1 \
  --reason "rsync confirmed complete"

# Full history for one entity
casetrack qc-history --project-dir . --level assay --id P3_tumor_a1
```

While censored, the `_active` view excludes the sample automatically — your
pending-work query naturally skips it without any code change.

---

## 7. Where to go next

You've used the v0.3 + v0.4 core (project model, register-cohort, append,
status, query, QC events). The interesting depth from here:

- **`.claude/skills/casetrack/SKILL.md`** — the canonical operating manual.
  Read §3 (`add-metadata` vs `append`), §5 (the `--overwrite` footgun), and
  §13 (24-row pitfall table) first.
- **Cohort-level artifacts** (proposal 0009) — joint VCFs, panels-of-normals,
  cohort matrices that span many assays. See SKILL.md §15.
- **Reference artifacts** (0010) — versioned upstream inputs (genome, GTF,
  dbSNP) that cascade staleness *down* when their version bumps. SKILL.md §16.
- **Nextflow integration** — the `CASETRACK_REGISTER` subworkflow records
  results at end-of-rule. See `.claude/skills/casetrack/references/nextflow-integration.md`.
- **The HTML dashboard** — `casetrack dashboard --project-dir .` produces a
  single-page status view.

Clean up the demo when you're done:

```bash
rm -rf /tmp/demo /tmp/sort_summary.tsv
```
