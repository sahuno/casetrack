# Annotated `casetrack.toml` — complete example

Fully-worked example for a cancer genomics tumor-normal cohort using ONT WGS.

```toml
# ─── Project identity ───────────────────────────────────────────────────────
[project]
project_id = "project-17424"     # DNS-label slug; used by casetrack_list_projects (MCP) and registry
name       = "project_17424"     # human-readable name (underscores ok here)
schema_v   = 1                   # auto-bumped by `casetrack schema apply`
created    = "2026-04-17T12:57:20"

# ─── Engine settings ────────────────────────────────────────────────────────
[engine]
wal             = true           # WAL mode — safe concurrent reads under NF fan-in
busy_timeout_ms = 30000          # SQLite busy_timeout (ms) for concurrent writers

[analysis_defaults]
default_level = "assay"          # used by `append` when --level is omitted

# ─── Level 1: patients ──────────────────────────────────────────────────────
[levels.patient]
key = "patient_id"

[levels.patient.columns]
patient_id        = { type = "TEXT", required = true, unique = true }
sex               = { type = "TEXT", enum = ["F", "M", "intersex", "unknown"] }
cohort            = { type = "TEXT" }
trio_role         = { type = "TEXT", enum = ["proband", "father", "mother", "sibling", "unrelated"] }
# clinical metadata — add as needed:
sample_id         = { type = "TEXT" }    # external/institutional sample label
internal_id       = { type = "TEXT" }    # lab-internal label
tube_id           = { type = "TEXT" }    # biobank / wetlab tube ID
tumor_type        = { type = "TEXT" }
timepoint         = { type = "TEXT" }    # Pre_ALLO, Post_ALLO, Relapse, etc.
collection_date   = { type = "TEXT" }    # ISO yyyy-mm-dd
age_at_collection = { type = "INTEGER" }

# ─── Level 2: specimens ─────────────────────────────────────────────────────
[levels.specimen]
key        = "specimen_id"
parent     = "patient"
parent_key = "patient_id"

[levels.specimen.columns]
specimen_id   = { type = "TEXT", required = true, unique = true }
patient_id    = { type = "TEXT", required = true }
specimen_type = { type = "TEXT", enum = ["whole_genome_dna", "lymphoblastoid_dna", "whole_blood", "buccal"] }
source        = { type = "TEXT" }        # tumor / germline / etc. (free text)
cell_line     = { type = "TEXT" }

# ─── Level 3: assays ────────────────────────────────────────────────────────
[levels.assay]
key        = "assay_id"
parent     = "specimen"
parent_key = "specimen_id"

[levels.assay.columns]
assay_id         = { type = "TEXT", required = true, unique = true }
specimen_id      = { type = "TEXT", required = true }
assay_type       = { type = "TEXT", required = true, enum = ["ONT_WGS", "ONT_target", "ONT_cDNA", "ONT_direct_RNA"] }
flowcell_id      = { type = "TEXT" }
chemistry        = { type = "TEXT", enum = ["R9.4.1", "R10.4.1", "R10.4.1_dorado"] }
basecaller_model = { type = "TEXT" }
# Path columns — critical for DB-driven samplesheet generation:
pod5_path        = { type = "TEXT" }     # raw signal data (input to basecaller)
bam_path         = { type = "TEXT" }     # basecalled BAM (output of basecaller)
condition        = { type = "TEXT" }     # "tumor" | "normal" — drives most batch queries
qc_pass          = { type = "BOOLEAN" }

# ─── QC system ──────────────────────────────────────────────────────────────
[qc]
kinds = [
  "qc_fail",
  "qc_warn",
  "consent_revoked",
  "protocol_deviation",
  "superseded",
  "library_prep_failed",
  "basecall_accuracy_low",
  "contamination",
  "batch_effect_flagged",
  "sequencing_run_failed",
  "other",
]
default_source   = "manual"
default_exclude  = ["fail", "censored", "consent_revoked"]

[qc.kind_scopes]
consent_revoked        = ["patient"]
library_prep_failed    = ["assay"]
basecall_accuracy_low  = ["assay"]
sequencing_run_failed  = ["assay"]
protocol_deviation     = ["specimen", "assay"]
contamination          = ["specimen", "assay"]

# ─── Results layout (v0.5+) ─────────────────────────────────────────────────
[layout]
results_dir = "results"

[layout.path_templates]
patient  = "{tool}/{run_tag}/{patient_id}"
specimen = "{tool}/{run_tag}/{patient_id}/{specimen_id}"
assay    = "{tool}/{run_tag}/{patient_id}/{specimen_id}/{assay_id}"

# ─── Analyses ───────────────────────────────────────────────────────────────
# Each entry defines ONE analysis tracked at ONE level. column_prefix is
# prepended to every result column from the summary TSV (except the key column
# and the auto-written {analysis}_done timestamp).

[analyses.dorado_basecaller]
level         = "assay"
column_prefix = "dorado"
summary_tsv   = "dorado_basecaller_summary.tsv"
nf_process    = "DORADO_BASECALLER"      # NF process name — L2/L3 trace import

[analyses.samtools_sort]
level         = "specimen"
column_prefix = "sort"
summary_tsv   = "samtools_sort_summary.tsv"

[analyses.samtools_flagstat]
level         = "specimen"
column_prefix = "flagstat"
summary_tsv   = "flagstat_summary.tsv"

[analyses.samtools_index]
level         = "specimen"
column_prefix = "index"
summary_tsv   = "index_summary.tsv"

[analyses.modkit_pileup]
level         = "specimen"
column_prefix = "modkit"
summary_tsv   = "modkit_summary.tsv"

[analyses.modkit_callmods]
level         = "specimen"
column_prefix = "modkit_cm"
summary_tsv   = "modkit_callmods_summary.tsv"

[analyses.sniffles2]
level         = "specimen"
column_prefix = "sv"
summary_tsv   = "sniffles2_summary.tsv"
nf_process    = "SNIFFLES"
```

## Notes

- `schema_v` is auto-managed. Do not edit by hand — `casetrack schema apply` bumps it.
- `column_prefix` must be a valid non-empty identifier. An empty string is rejected.
- The `enum` attribute on a column is enforced by casetrack on insert/update, not by SQLite.
- `required = true` on a non-key column means `add-metadata` rejects TSVs where that column is null.
- When adding a new analysis, after editing the TOML run `casetrack schema apply` to add the `{prefix}_*` and `{analysis}_done` columns to the level's table.
- `pod5_path` and `bam_path` on `assays` are optional for casetrack itself but essential if you want to generate Nextflow samplesheets from DB queries.
