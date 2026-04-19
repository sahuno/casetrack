# Proposal 0004 — Nextflow integration via `casetrack-nf-subworkflows`

**Status**: accepted (v0.1.0 pilot shipped 2026-04-18)
**Target release**: casetrack v0.5.0 + casetrack-nf-subworkflows v0.1.0
**Breaking**: no (additive; opt-in via `[layout]` / `[analyses]` TOML sections)
**Author**: Samuel Ahuno

## Motivation

`casetrack` already supports a three-phase SLURM pattern: (1) run a tool, (2) summarize to TSV, (3) `casetrack append`. This works, but reinvents orchestration that Nextflow already provides: DAG dependency, resume, resource requests, container binding, retry, trace/report generation.

At the same time, the nf-core ecosystem has ~1,500 vetted DSL2 modules covering the major bioinformatics tools. Using them directly gives us maintained software at zero cost — but only if we can add casetrack bookkeeping *without editing the modules*. Editing forks the module from upstream, and `nf-core modules update` silently clobbers the edits on every refresh.

The goal of this proposal is to make casetrack + Nextflow compose cleanly:

- **For users**: run any nf-core module and get a single row per sample in `casetrack.db`, with per-tool metadata (`{tool}_*` columns), run tag, job resources, and completion timestamp — without leaving Nextflow.
- **For maintainers**: every tracked subworkflow is ≤ 50 lines of Groovy + a summarize script + a `[analyses.<tool>]` block. Adding a new wrapper does not require modifying casetrack or the stock nf-core module.

## Goals

1. Register Nextflow process outputs into a casetrack project exactly once per completed assay, with the same schema guarantees as direct `casetrack append` invocation (typed columns, FK enforcement, provenance log, QC autoflag).
2. Do it without forking nf-core modules.
3. Keep pipeline code (Nextflow repo) and project data (casetrack project directory) strictly separated — the casetrack project is never inside the pipeline repo, and vice versa.
4. Capture Nextflow-native metadata (SLURM job id, runtime, peak RSS, exit status, tool versions) as casetrack columns, so the manifest can answer "did this fail biologically or infrastructurally?" from one query.
5. Provide one worked reference (MODKIT_PILEUP) with an end-to-end stub smoke test that new wrappers can be modeled on mechanically.

## Non-goals

- Modifying upstream nf-core modules. Ever.
- Replacing `casetrack append` with a Nextflow-only flow — direct CLI usage stays fully supported for non-Nextflow pipelines.
- Becoming an nf-core pipeline. `casetrack-nf-subworkflows` is a **library of subworkflows**, consumed by any pipeline (bespoke or nf-core) via `include { … }`.
- Integrating with the nf-core test data machinery at this stage — the pilot uses a local stub, not `test-datasets`.

## Architecture — three layers, each independently useful

| Layer | What it does | Where it lives | Status |
|---|---|---|---|
| **L1 — wrapper subworkflow** | Calls the stock nf-core module, distills its output into a one-row-per-assay TSV, registers via `casetrack append --infer-from-path`. | `casetrack-nf-subworkflows/subworkflows/local/<tool>_tracked.nf` | ✅ v0.1.0 (MODKIT_PILEUP_TRACKED) |
| **L2 — trace → manifest** | `workflow.onComplete` parses Nextflow's `execution_trace.txt` / `execution_report.html` into per-assay columns (`slurm_job_id`, `realtime`, `peak_rss`, `exit_status`, `attempts`). | New `casetrack trace-import` subcommand + Nextflow hook | ⏳ planned |
| **L3 — versions → manifest** | The nf-core `topic: versions` channel is collected and written as run-level metadata via `casetrack add-metadata`. | `workflow.onComplete` hook + helper script | ⏳ planned |

L1 alone is useful — it answers "is this assay done." L2 adds "did it complete cleanly." L3 adds reproducibility. The layers compose but can be adopted incrementally.

## Design decisions (locked in for v0.1)

These are the choices made during design review; deviations need a new proposal.

### §0.1 — Wrap, don't fork

Every stock nf-core module is imported unchanged. Bookkeeping lives in a separate `<tool>_tracked.nf` subworkflow that composes: stock module → `SUMMARIZE_<TOOL>` → `CASETRACK_REGISTER`. The subworkflow is what pipelines import.

**Why**: `nf-core modules update <tool>` keeps the upstream fresh. Forks rot.

### §0.2 — Extended samplesheet over derive-from-id

The Nextflow samplesheet carries explicit `patient`, `specimen`, `assay_id`, `genome` columns. These populate `meta.patient`, `meta.specimen`, `meta.assay_id`, `meta.genome` in every channel. `meta.id` mirrors `assay_id` so stock nf-core modules (`tag "${meta.id}"`) keep working unchanged.

**Rejected alternative**: derive `patient`/`specimen` by regex-splitting `meta.id` (e.g. `P01_primary_ONT1` → patient=`P01`). Easier for existing samplesheets, but couples ID format to casetrack and breaks on any non-conforming sample.

**Why**: the nf-core `assets/schema_input.json` pattern is the standard extension point. Upstream pipelines (methylseq, sarek) already allow extra columns, and `nf-validation`/`nf-schema` enforce them at parse time.

### §0.3 — Separate repo for subworkflows

`casetrack-nf-subworkflows` lives as a sibling directory, not inside the casetrack repo, not inside any pipeline repo.

**Why**: subworkflows are reusable across many pipelines (bespoke, nf-core/methylseq, nf-core/sarek). Embedding them in casetrack couples release cadence (the CLI and the wrappers ship at different tempos). Embedding them in a pipeline forces copy-paste between projects.

### §0.4 — Tool-first path layout is the registration contract

The casetrack project's `casetrack.toml` declares a `[layout]` block with per-level `path_templates`. Every tracked subworkflow writes its summary TSV to the canonical leaf:

```
results/{tool}/{run_tag}/{patient_id}/{specimen_id}/{assay_id}/
```

`CASETRACK_REGISTER` stages the TSV at that leaf, `cd`'s there, and runs `casetrack append --infer-from-path`. The append resolves `--project-dir`, `--level`, `--analysis`, `--column-prefix`, and `--results` from the path.

**Why**: one source of truth for the layout (the TOML), inferable from any leaf (no flag proliferation), idempotent, archivable-by-tool.

### §0.5 — `CASETRACK_REGISTER` runs on local executor

The register process sets `executor = 'local'` + `maxForks = 1` unconditionally. Never submitted to SLURM.

**Why**: the work is sub-second DB writes against SQLite on shared storage. Submitting N parallel jobs to SLURM costs 20+ seconds of Prolog per job (from live IRIS measurements), fights for queue slots, and produces WAL lock contention that produces no faster registration. Registration is a coordination step, not a compute step.

### §0.6 — One summary TSV per assay, keyed on assay_id

Every tracked subworkflow ends with a `SUMMARIZE_<TOOL>` process that emits exactly one row with `assay_id` as the first column. `[analyses.<tool>].summary_tsv` in casetrack.toml names the file. Column names become casetrack column names after the `[analyses.<tool>].column_prefix` rename.

**Why**: matches casetrack's existing three-phase contract. Keeps the summarize step generic (pure Python, no Nextflow awareness). `casetrack append` does not need to understand tool-specific output formats.

### §0.7 — `run_tag` as first-class metadata, injected automatically

When `casetrack append --infer-from-path` runs, it auto-injects a `run_tag` column into the summary TSV using the value parsed from the path. Under the `[analyses.<tool>].column_prefix` rename it becomes `{prefix}_run_tag`. Re-runs against the same assay with a new run_tag require `--overwrite` (default fill-only COALESCE preserves the first-landed value).

**Why**: `run_tag = {date}_{genome}_{description}` is per-run metadata the user needs to compare re-runs, but it's not produced by the tool. Auto-injection eliminates the "summarize script must know about run_tag" concern.

### §0.8 — `CASETRACK_REGISTER` is synchronous and serialized

`maxForks = 1` plus synchronous exit. No batching. The append is fast enough (< 200 ms typical) that batching would complicate failure handling without meaningful throughput gain.

**Why**: simpler. If we ever have enough scale to care, switch to a `collect { ... } | batch_casetrack_append` terminal process — but that's a v0.6+ concern.

### §0.9 — Stub-mode smoke test, not nf-test

Each wrapper ships with a bash smoke test that runs `nextflow -stub`, then asserts the SQLite DB matches expected values. No dependency on `nf-test`.

**Why**: stub mode exercises 100% of the wiring (channels, publishDir, register) without needing real BAMs or containers. The shipping pilot runs in ~5 seconds.

### §0.10 — JSON Schema for samplesheet validation

`assets/schema_input.json` in the subworkflows repo encodes the required columns + enums (genome ∈ {mm10, mm39, hg38, GRCh37, t2t, chm13}). Compatible with `nf-validation` / `nf-schema` so pipelines can opt into formal validation.

**Why**: standard nf-core practice; samplesheets are the most common source of pipeline failures.

## Cross-repo contract

```
~/apps/
├── casetrack/                                 # the CLI + QC subsystem
│   ├── casetrack.py
│   ├── casetrack_qc/
│   │   └── path_infer.py                      # v0.5.0 — resolves path → casetrack row
│   └── docs/proposals/0004-nextflow-integration.md   # ← this doc
│
├── casetrack-nf-subworkflows/                 # the Nextflow library
│   ├── main.nf                                # demo pipeline
│   ├── subworkflows/local/<tool>_tracked.nf   # wrappers (one per tool)
│   ├── modules/local/casetrack_register.nf    # shared register process
│   ├── modules/nf-core/<tool>/                # vendored upstream, refreshed via nf-core modules update
│   └── assets/schema_input.json
│
└── <your-pipeline>/                           # consumer — a real analysis repo
    ├── main.nf
    └── samplesheet.csv                        # patient,specimen,assay_id,genome,bam,bai

── casetrack projects live elsewhere ──
/data1/greenbab/.../cohort_X/
├── casetrack.toml                             # declares [layout] + [analyses.<tool>]
├── casetrack.db
├── provenance.jsonl
└── results/<tool>/<run_tag>/<patient>/<specimen>/<assay>/   # populated by wrappers
```

Three concerns, three repos, one data directory. None of them are cloned inside any of the others.

## Nextflow contract

### Per-sample `meta` map

Every tracked subworkflow's input channel is `tuple(meta, ...)` where:

```groovy
meta = [
    id:       <assay_id>,      // == meta.assay_id, for nf-core module compatibility
    patient:  <patient_id>,
    specimen: <specimen_id>,
    assay_id: <assay_id>,
    genome:   <genome_build>,  // one of: mm10 mm39 hg38 GRCh37 t2t chm13
]
```

### Required pipeline params

```
--input                  samplesheet.csv           // extended schema (see assets/schema_input.json)
--casetrack_project_dir  /abs/path/to/project      // must contain casetrack.toml + casetrack.db
--run_tag                {date}_{genome}_{description}
--fasta / --fai                                    // tool-dependent
```

### Required TOML blocks in the casetrack project

```toml
[layout]
results_dir = "results"

[layout.path_templates]
assay = "{tool}/{run_tag}/{patient_id}/{specimen_id}/{assay_id}"

[analyses.<tool>]
level         = "assay"
column_prefix = "<prefix>"
summary_tsv   = "<tool>_summary.tsv"
```

### Register process contract

```groovy
CASETRACK_REGISTER(tuple(meta, tool_name, summary_filename, summary_tsv))
// -> emits tuple(meta, tool_name) on .out.ok after the DB write succeeds
```

## Current state (v0.1.0)

| Component | Status | Notes |
|---|---|---|
| `casetrack [layout] + [analyses]` TOML validation | ✅ shipped | Additive; any `schema_v` |
| `casetrack append --infer-from-path` | ✅ shipped | Auto-injects `run_tag` column |
| `casetrack-nf-subworkflows` scaffold | ✅ shipped | MIT, public at github.com/sahuno/casetrack-nf-subworkflows |
| `MODKIT_PILEUP_TRACKED` subworkflow | ✅ shipped | Pilot reference |
| Extended samplesheet schema | ✅ shipped | JSON Schema, `nf-validation`-compatible |
| Stub smoke test | ✅ shipped | ~5 sec end-to-end; now covers L1 + L2 |
| L2 trace parser | ✅ shipped v0.2.0 | `bin/trace_to_casetrack.py` + `workflow.onComplete` hook; one `casetrack append --analysis <tool>_trace` per tool |
| L3 versions manifest | ⏳ pending | Section below |
| Additional wrappers (DORADO, CALLMODS, SORT, SNIFFLES2) | ⏳ pending | Mechanical — one per week |
| nf-core/methylseq drop-in config | ⏳ pending | v0.3+ |
| Real-data validation | ⏳ pending | GIAB chr21 ONT next |
| Publish to GitHub | ⏳ pending | After L2 + real-data |

## Implemented — L2: trace → manifest (v0.2.0)

Parses `results/_nextflow/<run_tag>/execution_trace.txt` (Nextflow's tab-separated per-task resource log) after every pipeline run, merges against `[analyses.<tool>]` tool declarations, writes per-assay columns:

| Column | Source (trace.txt field) | Type |
|---|---|---|
| `{prefix}_slurm_job_id`     | `native_id` or extracted from `hash` when `executor=slurm` | TEXT |
| `{prefix}_realtime_seconds` | `realtime` | INTEGER |
| `{prefix}_peak_rss_bytes`   | `peak_rss` | INTEGER |
| `{prefix}_exit_status`      | `exit` | INTEGER |
| `{prefix}_attempts`         | `attempt` | INTEGER |
| `{prefix}_queue`            | `queue` | TEXT |

### What shipped

- `casetrack-nf-subworkflows/bin/trace_to_casetrack.py` — stdlib-only Python helper, reads casetrack.toml, parses trace.txt, pivots to per-tool TSVs, shells out to `casetrack append`.
- `workflow.onComplete` hook in `main.nf` — invokes the helper with `--project-dir`, `--trace`, `--run-tag`. Always runs (success OR failure) so partial traces still land. Controlled by `params.casetrack_import_trace` (default `true`).
- `nextflow.config` — enables extended trace fields: `task_id,hash,native_id,process,tag,name,status,exit,submit,start,complete,duration,realtime,%cpu,peak_rss,peak_vmem,rchar,wchar,queue,attempt` (default set omits `process`/`tag`/`queue`/`attempt`).
- Extended `test/run_test.sh` — asserts `modkit_exit_status`, `modkit_attempts`, `modkit_realtime_sec`, `modkit_queue`, `modkit_peak_rss_bytes`, `modkit_slurm_job_id` columns are created by the L2 import.

### Why `casetrack append` instead of `casetrack add-metadata`

Original design proposed `add-metadata`. Turns out `add-metadata` rejects columns not pre-declared in casetrack.toml (that's its job — fill known columns). Trace columns are per-tool and not known until a tool's `[analyses.<tool>]` entry exists, so pre-declaring them would require code-generating TOML. `append` auto-creates columns via ALTER TABLE, which is exactly what we need. The helper calls one `casetrack append --analysis <tool>_trace --column-prefix <prefix>` per tool; the `<tool>_trace_done` timestamp it creates doubles as "when did we last import this tool's trace for this assay."

### Trace-row → (tool, assay_id) mapping

Nextflow's `process` trace field contains `SUBWORKFLOW:MODULE` (e.g. `MODKIT_PILEUP_TRACKED:MODKIT_PILEUP`). Last `:`-separated segment is the tool name (matched case-insensitively against `[analyses.<tool>]` keys). Nextflow's `tag` trace field carries `tag "${meta.id}"` from each process — because we require `meta.id == meta.assay_id` in §0.2, that's our join key. Rows with a tool not in `[analyses]` (e.g. `SUMMARIZE_MODKIT`, `CASETRACK_REGISTER`) are silently skipped.

## Planned — L3: versions → manifest

nf-core modules emit a `topic: versions` channel that collects tool + version tuples across all processes. We collect to one `versions.yml` per run and write as run-level metadata:

```toml
[analyses.<tool>]
level = "assay"
# …
versions_column = "{prefix}_tool_version"   # optional: stores the version in the per-assay row
```

Or, simpler: a single `results/_nextflow/<run_tag>/versions.yml` next to the trace files, referenced from the run_manifest.json without per-row duplication.

## Roadmap — priorities

Short-term (next two weeks):
1. **Commit & tag casetrack v0.5.0** (this proposal + v0.5 code).
2. **Implement L2 trace parser** (~2 hours).
3. **Run MODKIT_PILEUP_TRACKED on one real GIAB chr21 ONT BAM** (~1 hour) to validate container binding, real summarize script, run_tag semantics.

Medium-term (next month):
4. Add `MODKIT_CALLMODS_TRACKED`, `DORADO_BASECALLER_TRACKED`, `SAMTOOLS_SORT_TRACKED` wrappers.
5. Push `casetrack-nf-subworkflows` to GitHub with CI (Nextflow + casetrack + stub smoke test).
6. Document on mkdocs/Pages with one fully-worked example pipeline.

Long-term:
7. Drop-in `-c casetrack.config` for nf-core/methylseq.
8. `casetrack-nf-subworkflows` publishes to `nf-core/subworkflows` if the upstream community wants it.
9. L3 (versions → manifest) formalization.

## Open questions

### Q1 — sample-level vs run-level metadata for Nextflow traces

If the same assay is run twice (different `run_tag`), do we overwrite the first `{prefix}_peak_rss_bytes` or keep both? Proposal: same as data columns — fill-only by default, `--overwrite` on re-run. Alternative: add a `trace` table keyed on `(assay_id, run_tag, tool)` for historical comparison. Probably wait until someone asks.

### Q2 — container pinning

Stock nf-core modules use `https://depot.galaxyproject.org/singularity/...` URLs that hit rate limits from compute nodes. Do we override via profile-level `process.container` for high-traffic modules to point at group-shared SIFs? Recommendation: yes, document in README, but don't fork the modules.

### Q3 — failure semantics

If `CASETRACK_REGISTER` fails (e.g. DB busy), does the wrapper's `.out.casetrack_done` never emit and the pipeline hangs? Current behavior: `errorStrategy 'retry'`, `maxRetries 2`. If all 3 attempts fail, pipeline fails. Is that right, or should we treat registration as advisory? Recommendation: strict — unregistered data is worse than pipeline failure. Document loudly.

### Q4 — patient-level and specimen-level tracked subworkflows

All shipped wrappers are assay-level. A cohort-level DMR caller would be patient-level (or "cohort-level" which doesn't map to casetrack today). Do we need to extend the scheme? Probably. Defer to when the first non-assay wrapper is requested.

### Q5 — interaction with QC autoflag

If a `SUMMARIZE_<TOOL>` produces `qc_pass=False`, casetrack's autoflag already fires a qc_event. Do we want to also halt the downstream processes in the Nextflow DAG for that assay? Currently: no — data lands in the DB, downstream Nextflow processes don't know. Could add a Groovy helper that filters `.out.ok` by reading qc_status. Defer until someone needs it.

## References

- Proposal 0001 — SQLite normalized backend
- Proposal 0002 — QC events + censoring + consent
- Proposal 0003 — init scaffold
- casetrack CHANGELOG §0.5.0 — `[layout]` + `[analyses]` + `--infer-from-path`
- `casetrack-nf-subworkflows/README.md`
- `casetrack-nf-subworkflows/test/run_test.sh` — working stub smoke test
