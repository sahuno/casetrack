# hgsoc_sim/hpc — HPC-scale cohort demo on IRIS

A real SLURM-driven run of the `hgsoc_sim` cohort on MSKCC IRIS. Produces a
populated casetrack project, a live cohort dashboard, per-phase benchmarks, and
a test fixture that matches what production pipelines look like.

This sits **next to** `examples/hgsoc_sim/` (the laptop demo) — both consume
the same cohort definition in the parent `config.yaml` (3 patients, BRCA1 +
TP53 slice). The parent stays small and deterministic; this folder scales it
across SLURM with real VISOR / Badread / minimap2 for the ONT side and
deterministic mocks for the scRNA side.

## What you get

- **3 patients** — `HGSOC_SIM_01` (complete tumor/normal pair), `HGSOC_SIM_02`
  (broken pair: normal deliberately under-sequenced), `HGSOC_SIM_03`
  (tumor-only singleton).
- **5 specimens × 3 assays** each = **15 assays** — 10 ONT_WGS (2 flowcell
  runs per specimen) + 5 scRNA (1 run each).
- **Real ONT pipeline** — VISOR HACk → Badread (R10.4.1) → minimap2 → sort
  → pre-merge flagstat → merge → modkit pileup. Reuses
  `examples/patterns/premerge_runs/` verbatim.
- **Mock scRNA** — hash-deterministic 10x-Chromium-style metrics; enough to
  exercise the v0.4 autoflag path (`HGSOC_SIM_02_normal` trips the
  `min_cells` threshold on purpose).
- **One casetrack project** — all 15 assays registered, bam_paths attached,
  flagstat/merge/modkit/scrna metrics landed, QC events emitted for the
  broken entities.
- **Seven-phase dependency graph** — one sbatch-chained run, from raw
  synthesis to final dashboard.

## Layout

```
examples/hgsoc_sim/hpc/
├── config.yaml                  # HPC overlay (flowcell count, resources, container paths)
├── scripts/
│   ├── bootstrap_casetrack.py   # register 3 × 5 × 15 = patients / specimens / assays
│   ├── _specimen_synth_params.py # deterministic (coverage, purity, seed) per run
│   ├── mock_scrna_summary.py    # hash-deterministic scRNA summary + autoflag
│   └── bench_report.py          # post-run sacct → markdown benchmark
├── slurm/
│   ├── submit_pipeline.sh       # orchestrator — 7 phases, afterok chained
│   ├── run_synth_align.sh       # phase 1: VISOR + Badread + minimap2 + sort (per run)
│   ├── run_attach_bams.sh       # phase 2: push bam_path back to casetrack
│   ├── run_mock_scrna.sh        # phase 6: one deterministic scRNA summary
│   └── run_summary.sh           # phase 7: status + cohort + dashboard + benchmark
├── tests/
│   └── test_hpc_scaffold.py     # CI-safe smoke test (bootstrap + mocks, no cluster)
└── benchmarks/                  # run_YYYYMMDD_HHMMSS.md dropped here
```

Phases 3–5 (pre-merge flagstat / merge / modkit) are dispatched by
`submit_pipeline.sh` but run **from** `examples/patterns/premerge_runs/`.
Nothing is duplicated.

## Dependency graph

```
synth_align ──► attach_bams ──► premerge_flagstat ──► merge_ont ──► modkit_merged ──┐
                                                                                    ├──► summary
mock_scrna  ────────────────────────────────────────────────────────────────────────┘
```

## Design choices worth calling out

- **Parent cohort definition is the source of truth.** `hpc/config.yaml`
  only carries HPC-specific knobs (flowcell runs, SLURM resources, container
  paths). Swap patients or variants in the parent and the HPC run follows
  automatically.
- **Native binaries preferred over containers.** `run_synth_align.sh` probes
  PATH first (badread / minimap2 / samtools live in the `snakemake` conda
  env on IRIS) and falls back to containers only when a native binary is
  absent. VISOR is the one exception — it uses
  `/data1/greenbab/software/images/visor_latest.sif` because no env on the
  cluster ships it.
- **bam_path round-trips through casetrack.** `synth_align` writes a
  per-run `metadata.tsv`; `attach_bams` appends those back onto the assays
  table. Downstream phases then query casetrack for bam_path instead of
  reconstructing paths — which is exactly what a production pipeline does.
- **scRNA uses `--column-prefix scrna`** (v0.4.1) so metrics land as
  `scrna_n_cells`, `scrna_pct_mito`, etc. Prevents collisions when more
  assay types are added later.
- **Autoflag is wired in, not simulated.** `mock_scrna_summary.py` emits
  `qc_pass` / `qc_fail_reason` columns; casetrack consumes them inside the
  same `append` transaction. The broken specimen (`HGSOC_SIM_02_normal`)
  ends up with a real `qc_events` row.

## Running it

All commands assume you're on IRIS and `casetrack` is on your PATH
(`pip install -e ".[all]" --user`).

### 0. Set up shared env

```bash
export PROJECT_DIR=/data1/greenbab/users/$USER/casetrack_projects/hgsoc_sim_hpc/project
export SANDBOX=/data1/greenbab/users/$USER/casetrack_projects/hgsoc_sim_hpc
export CASETRACK_BIN=$(command -v casetrack)
```

### 1. Bootstrap (login node — no cluster)

Registers the 3 patients, 5 specimens, 15 assays; creates `casetrack.db`.

```bash
python3 examples/hgsoc_sim/hpc/scripts/bootstrap_casetrack.py \
    --project-dir "$PROJECT_DIR"
casetrack status --project-dir "$PROJECT_DIR"
```

You should see `patients=3, specimens=5, assays=15`.

### 2. Plan (dry-run — no submission)

Prints every sbatch command that `all` would issue:

```bash
bash examples/hgsoc_sim/hpc/slurm/submit_pipeline.sh plan
```

### 3. Prepare the sandbox

`run_synth_align.sh` expects `$SANDBOX/ref/ref.fa` and
`$SANDBOX/cohort/<patient>/<specimen>/haplotype{1,2}.hack.bed`. The parent
demo's `examples/hgsoc_sim/scripts/prepare_cohort.sh` produces both; copy
them into `$SANDBOX` (the HPC variant intentionally does not re-implement
the prep step).

### 4. Submit

```bash
bash examples/hgsoc_sim/hpc/slurm/submit_pipeline.sh all --submit
```

Watch with `squeue -u $USER` or `casetrack status --project-dir "$PROJECT_DIR"`
(QC-aware by default — the broken SIM_02 normal surfaces once autoflag lands).

### 5. Inspect

After the `summary` phase completes:

```bash
open "$PROJECT_DIR/../hpc/benchmarks/dashboard.html"      # cohort dashboard
cat  "$PROJECT_DIR/../hpc/benchmarks/run_*.md"            # per-phase wall clock + CPU-hours
casetrack cohort --project-dir "$PROJECT_DIR" \
    --pair-by tissue_site --partition-order tumor,normal --assay-type ONT
casetrack qc-history --project-dir "$PROJECT_DIR"
```

## Tests

CI-safe (no SLURM, no synthesis):

```bash
python3 -m pytest examples/hgsoc_sim/hpc/tests/test_hpc_scaffold.py -q
```

Exercises bootstrap, counts, mock-scrna determinism + autoflag, synth
param derivation, and SBATCH header sanity on `run_synth_align.sh`.

## Scaling up

Everything is driven by the parent `config.yaml`. To stress-test with 10
or 50 patients:

1. Extend `patients:` in `examples/hgsoc_sim/config.yaml`.
2. Re-run `bootstrap_casetrack.py` against a fresh `$PROJECT_DIR`.
3. `submit_pipeline.sh all --submit` scales with it — one synth job per new
   (specimen × flowcell-run), one modkit job per new specimen.

No changes to the SLURM wrappers are required.
