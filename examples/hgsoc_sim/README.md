# examples/hgsoc_sim — simulated HGSOC cohort for the v0.4 QC demo

A reproducible mini-cohort of simulated ONT tumor/normal data that exercises
the v0.4 QC workflow end-to-end:

| Patient        | Specimens          | Shape              | What it shows |
|----------------|--------------------|--------------------|---------------|
| `HGSOC_SIM_01` | tumor + normal     | **complete pair**  | happy path — both halves pass QC |
| `HGSOC_SIM_02` | tumor + normal     | **broken pair**    | normal deliberately truncated → `qc_pass=False` → autoflag fires |
| `HGSOC_SIM_03` | tumor only         | **singleton**      | `cohort --pair-by` surfaces the missing partition |

The generated BAMs are NOT checked into the repo — the scripts here produce
them in about a minute on a single node with the two Apptainer images
pre-pulled (see [containers/README.md](containers/README.md)). Output lives
under `sandbox/hgsoc_sim/` (gitignored).

## Prerequisites

- **Apptainer** (or Docker) — to run the four biocontainers (VISOR,
  Badread, minimap2, samtools).
- **curl** — for downloading chr17.
- **Python 3.10+** with `pandas`, `pyyaml` for the config + summary scripts.
- **casetrack** — `pip install -e . --user` from the repo root.

Container pulls happen in [`containers/README.md`](containers/README.md)
(one-shot, ~300 MB total).

## Run it

```bash
# From the repo root:
bash examples/hgsoc_sim/run_demo.sh
```

One-shot driver. Runs the six steps below in sequence; each step is also
runnable on its own for debugging.

## Steps

| Step | Script                                      | Produces |
|------|---------------------------------------------|----------|
| 0    | `scripts/00_fetch_reference.sh`             | `sandbox/hgsoc_sim/ref/ref.fa` — multi-contig slice of GRCh38 (`chr17_brca1` + `chr17_tp53`) |
| 1    | `scripts/01_prepare_visor_beds.py`          | per-patient HACk + LASeR BEDs under `sandbox/hgsoc_sim/cohort/<PATIENT>/{normal,tumor}/` |
| 2    | `scripts/02_run_visor.sh`                   | HACk → haplotype FASTAs → Badread R10.4.1 reads → minimap2 → `sim.srt.bam` |
| 3    | `scripts/03_break_hgsoc_sim_02.sh`          | downsamples the `HGSOC_SIM_02` normal BAM to ~2× (triggers autoflag later) |
| 4    | `scripts/04_summarize_mock.py`              | per-assay summary TSVs with `qc_pass` / `qc_fail_reason` columns |
| 5    | `scripts/05_bootstrap_casetrack.py`         | `sandbox/hgsoc_sim/project/` — a v0.4 casetrack project with the cohort registered and summaries appended |

## What you should see after `run_demo.sh`

```
$ casetrack status --project-dir sandbox/hgsoc_sim/project --usable
  Usable assays: 4 / 5
  Excluded:      1
    QC-failed:   1   (HGSOC_SIM_02-normal-ONT-WGS)

$ casetrack cohort --project-dir sandbox/hgsoc_sim/project \
      --assay-type ONT --pair-by tissue_site
  PATIENT         TUMOR    NORMAL   GROUP STATUS
  HGSOC_SIM_01    pass     pass     complete
  HGSOC_SIM_02    pass     FAIL     broken
  HGSOC_SIM_03    pass     (none)   singleton
  Summary:
    Complete groups:  1
    Broken groups:    1
    Singletons:       1
```

The HGSOC_SIM_02 broken pair mirrors the §4.5 worked example in proposal
0002 (HGSOC002's failed normal ONT-RNA). The simulation is a different
assay type (WGS instead of RNA) for tooling convenience — VISOR doesn't
simulate RNA directly. The QC story is identical.

## Cohort design — `config.yaml`

One YAML file describes the cohort. Edit it to add patients / variants /
adjust purity; the VISOR BEDs are regenerated from this file by
`01_prepare_visor_beds.py`. Each patient has:

- **germline variants** (present in both normal and tumor haplotypes)
- **somatic variants** (tumor-only)
- **specimens** — one entry per specimen, with coverage and tumor purity

See `config.yaml` for the full spec.

## Tradeoffs / caveats

- **Reads are R10.4.1 via Badread** (`nanopore2023` error + qscore models).
  VISOR HACk still drives haplotype construction; its bundled pbsim2
  simulator (R9.4.1) is bypassed. See `containers/README.md` for the
  read-mix math and purity weighting.
- **WGS, not RNA.** The motivating HGSOC002 case in proposal 0002 is an
  ONT-RNA failure, but VISOR simulates DNA reads only. The QC path being
  exercised (append → autoflag → status --usable → cohort --pair-by) is
  identical.
- **Two slices, ~1.1 Mb total** (`chr17_brca1` 1 Mb + `chr17_tp53` 100 kb)
  keep runtime under a minute. Extend `config.yaml` to add CCNE1
  (chr19:29.8M) or any other region — the scripts loop over `reference.slices`
  automatically.
- **Don't treat the truth VCFs as production ground truth.** VISOR reports
  what it inserted; that's enough for a reproducibility check on the
  pipeline but not for variant-caller benchmarking.
