# Common SQL queries for casetrack projects

All queries assume v0.3+ three-level schema. Prefer the `_active` view over raw tables — it excludes censored entities and cascades parent censoring.

## Cohort progress matrix

One row per patient × condition, with running totals for each tracked analysis.

```sql
SELECT p.patient_id, p.internal_id, a.condition,
       COUNT(a.assay_id)                        AS n_assays,
       SUM(a.dorado_basecaller_done IS NOT NULL) AS dorado_done,
       MAX(s.samtools_sort_done)                AS sort_done,
       MAX(s.modkit_callmods_done)              AS callmods_done,
       MAX(s.sniffles2_done)                    AS sv_done
FROM patients p
JOIN specimens s ON p.patient_id = s.patient_id
JOIN assays    a ON s.specimen_id = a.specimen_id
GROUP BY p.patient_id, a.condition
ORDER BY p.patient_id, a.condition;
```

## Per-analysis progress breakdown

For a single analysis, show total / done / pending / flagged by any grouping dimension.

```sql
SELECT condition,
       COUNT(*)                                        AS total,
       SUM(dorado_basecaller_done IS NOT NULL)         AS done,
       SUM(dorado_basecaller_done IS NULL
           AND qc_status = 'pass')                     AS pending,
       SUM(qc_status = 'warn')                         AS flagged,
       SUM(qc_status IN ('fail','censored'))           AS blocked
FROM assays
GROUP BY condition;
```

## Generate a Nextflow samplesheet for pending basecalling

Dynamic work queue — query the DB for pending work, pipe to CSV, feed to NF.

```sql
-- Assay-level: basecall normals that are ready and not yet done
SELECT a.assay_id      AS sample,
       p.patient_id    AS patient,
       s.specimen_id   AS specimen,
       a.pod5_path     AS pod5_dir,
       'hg38'          AS genome
FROM assays a
JOIN specimens s ON a.specimen_id = s.specimen_id
JOIN patients  p ON s.patient_id  = p.patient_id
WHERE a.condition = 'normal'
  AND a.qc_status = 'pass'
  AND a.dorado_basecaller_done IS NULL
ORDER BY p.patient_id;
```

Convert to CSV:
```bash
casetrack query --project-dir . --sql "<above>" --fmt csv > pending_normals.csv
nextflow run main.nf --input pending_normals.csv ...
```

## Specimen-level samplesheet for sort/SV/methylation

After all assays in a specimen are basecalled, generate the specimen-level pending list.

```sql
SELECT s.specimen_id AS sample,
       p.patient_id  AS patient,
       s.specimen_id AS specimen,
       (SELECT MAX(a.bam_path) FROM assays a WHERE a.specimen_id = s.specimen_id) AS bam_path,
       'hg38' AS genome
FROM specimens s
JOIN patients p ON s.patient_id = p.patient_id
WHERE s.samtools_sort_done IS NULL
  AND EXISTS (
      SELECT 1 FROM assays a
      WHERE a.specimen_id = s.specimen_id
        AND a.bam_path IS NOT NULL
        AND a.qc_status = 'pass'
  );
```

## QC event timeline

Full censor/uncensor history for an entity.

```sql
SELECT event_id, occurred_at, action, kind, reason, resolved_at
FROM qc_events
WHERE entity_id = 's17424_C_6_1_1_1_1_1'
ORDER BY occurred_at DESC;
```

Or use the CLI shortcut:
```bash
casetrack qc-history --project-dir . --id s17424_C_6_1_1_1_1_1
```

## Find orphan specimens / assays (data-integrity check)

```sql
-- Specimens without a matching patient row (should return zero)
SELECT s.specimen_id FROM specimens s
LEFT JOIN patients p ON s.patient_id = p.patient_id
WHERE p.patient_id IS NULL;

-- Assays without a matching specimen
SELECT a.assay_id FROM assays a
LEFT JOIN specimens s ON a.specimen_id = s.specimen_id
WHERE s.specimen_id IS NULL;
```

FK enforcement should make these impossible, but useful for auditing imports from a legacy flat manifest.

## Tumor-normal pairing (cohort shape)

For each patient, list their tumor and normal assays side by side.

```sql
SELECT p.patient_id,
       GROUP_CONCAT(CASE WHEN a.condition = 'tumor'  THEN a.assay_id END, ',') AS tumor_assays,
       GROUP_CONCAT(CASE WHEN a.condition = 'normal' THEN a.assay_id END, ',') AS normal_assays,
       SUM(a.condition = 'tumor')  AS n_tumor,
       SUM(a.condition = 'normal') AS n_normal
FROM patients p
JOIN specimens s ON p.patient_id = s.patient_id
JOIN assays    a ON s.specimen_id = a.specimen_id
GROUP BY p.patient_id
ORDER BY p.patient_id;
```

## Re-queue stale analyses

Samples where the analysis ran before a given cutoff — useful when a pipeline bug is fixed and all results before a date need reprocessing.

```sql
SELECT specimen_id
FROM specimens
WHERE samtools_sort_done < '2026-04-01';
-- Then: cat those into a samplesheet and rerun NF -resume will skip, so delete
-- the sort_sorted_bam_path column via `casetrack append --overwrite` after the rerun.
```

## Query from Claude Code (MCP)

When operating inside Claude Code, the MCP tool is usually faster than shelling out:

```python
mcp__casetrack__casetrack_list_projects()
mcp__casetrack__casetrack_query(
    project_id="project-17424",
    sql="SELECT condition, COUNT(*) FROM assays GROUP BY condition"
)
```

`project_id` is always the DNS-slug form (e.g. `project-17424`, with hyphens not underscores). Get the exact slug via `casetrack_list_projects` first if unsure.

## Views to know

- `_active` — the canonical read view. Filters out censored entities and cascades parent censoring. Use this for every analysis/reporting query.
- Raw tables (`patients`, `specimens`, `assays`, `qc_events`) — unfiltered. Use only for auditing.
