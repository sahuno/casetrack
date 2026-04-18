# examples/hgsoc_sim — simulated HGSOC cohort (DNA + RNA) for the v0.4 QC demo

Reproducible mini-cohort of simulated ONT tumor/normal data. Every specimen
has both a **genomic (ONT-DNA)** and a **transcriptomic (ONT-RNA)** assay,
so the demo exercises casetrack's per-assay QC paths — not just per-specimen.

| Patient        | Specimens                 | Shape              | What it shows |
|----------------|---------------------------|--------------------|---------------|
| `HGSOC_SIM_01` | normal + tumor (DNA + RNA)| **complete pair**  | happy path — 4 assays, all pass |
| `HGSOC_SIM_02` | normal + tumor (DNA + RNA)| **broken pair**    | normal **RNA** deliberately truncated → `qc_pass=False` → autoflag fires. Normal DNA passes — per-assay QC on the same specimen. |
| `HGSOC_SIM_03` | tumor only (DNA + RNA)    | **singleton**      | `cohort --pair-by` surfaces the missing normal half for both assay types |

10 assays total · 9 pass · 1 FAIL (the HGSOC_SIM_02 normal RNA).
Matches the §4.5 worked example of proposal 0002 exactly.

BAMs and expression files are NOT checked in — the scripts here produce
them in about 8–10 minutes on a single node with the six Apptainer images
pre-pulled (see [containers/README.md](containers/README.md)). Output lives
under `sandbox/hgsoc_sim/` (gitignored).

## Prerequisites

- **Apptainer** (or Docker) — to run the six biocontainers (VISOR, Badread,
  gffread, NanoSim, minimap2, samtools).
- **curl** — for downloading chr17 + GENCODE + NanoSim model.
- **Python 3.10+** with `pandas`, `numpy`, `pyyaml`.
- **casetrack ≥ 0.4.1** — `pip install -e . --user` from the repo root.

Container pulls happen in [`containers/README.md`](containers/README.md)
(one-shot, ~500 MB total).

## Run it

```bash
# From the repo root, after pulling SIFs + exporting CONTAINER_DIR:
bash examples/hgsoc_sim/run_demo.sh
```

One-shot driver. Runs the nine numbered steps below in sequence; each step
is also runnable on its own for debugging.

## Steps

| Step | Script                                   | Lane | Produces |
|------|------------------------------------------|------|----------|
| 00a  | `scripts/00_fetch_reference.sh`          | ref  | `sandbox/hgsoc_sim/ref/ref.fa` — multi-contig slice of GRCh38 (`chr17_brca1` + `chr17_tp53`) |
| 00b  | `scripts/00b_fetch_gencode.sh`           | RNA  | `ref/transcripts.fa` + `ref/transcripts.tsv` — per-slice transcript sequences from GENCODE v47 |
| 00c  | `scripts/00c_fetch_nanosim_model.sh`     | RNA  | `nanosim_models/human_NA12878_cDNA_Bham1_guppy/` — pre-trained cDNA simulation model |
| 01a  | `scripts/01_prepare_visor_beds.py`       | DNA  | per-assay HACk + LASeR BEDs under `cohort/<PATIENT>/<SPECIMEN>/ONT-DNA/` |
| 01b  | `scripts/01b_prepare_expression.py`      | RNA  | per-assay `expression.tsv` under `cohort/<PATIENT>/<SPECIMEN>/ONT-RNA/` (log-normal baseline × gene multipliers from config.yaml) |
| 02a  | `scripts/02_run_visor.sh`                | DNA  | VISOR HACk → Badread R10.4.1 → minimap2 map-ont → `<ASSAY>/sim.srt.bam` |
| 02b  | `scripts/02b_run_nanosim.sh`             | RNA  | NanoSim cDNA → minimap2 splice → `<ASSAY>/sim.srt.bam` |
| 03   | `scripts/03_break_hgsoc_sim_02.sh`       | RNA  | downsamples HGSOC_SIM_02 **normal RNA** BAM to ~3% of reads (triggers autoflag later) |
| 04   | `scripts/04_summarize_mock.py`           | both | per-assay summary TSVs with `qc_pass` / `qc_fail_reason` columns |
| 05   | `scripts/05_bootstrap_casetrack.py`      | both | `sandbox/hgsoc_sim/project/` — a v0.4 casetrack project with 10 assays registered and appended, `--column-prefix ont_dna`/`ont_rna` for each lane |

## What you should see after `run_demo.sh`

```
$ casetrack status --project-dir sandbox/hgsoc_sim/project --usable
  Usable assays: 9 / 10
  Excluded:      1
    QC-failed:   1   (HGSOC_SIM_02-normal-ONT-RNA)

$ casetrack cohort --project-dir ... --assay-type ONT-DNA --pair-by tissue_site
  PATIENT         TUMOR    NORMAL   GROUP STATUS
  HGSOC_SIM_01    pass     pass     complete
  HGSOC_SIM_02    pass     pass     complete            ← DNA is fine on both
  HGSOC_SIM_03    pass     (none)   singleton

$ casetrack cohort --project-dir ... --assay-type ONT-RNA --pair-by tissue_site
  PATIENT         TUMOR    NORMAL   GROUP STATUS
  HGSOC_SIM_01    pass     pass     complete
  HGSOC_SIM_02    pass     FAIL     broken              ← RNA broken on normal
  HGSOC_SIM_03    pass     (none)   singleton
```

The two `cohort --pair-by` invocations produce different answers for the
same patient set — that's the whole point of per-assay QC. HGSOC_SIM_02 is
fine for paired DNA analyses (tumor-vs-normal variant calling) but
broken for paired RNA analyses (differential expression).

## Cohort design — `config.yaml`

One YAML file describes the cohort. Edit it to add patients / variants /
adjust purity / change expression profiles; the per-assay input files
are regenerated from this file by `01` and `01b`.

Each patient has:

- **germline variants** (present in both normal and tumor haplotypes)
- **somatic variants** (tumor-only)
- **specimens**, each with an **`assays`** list — one entry per assay type

DNA assay fields: `coverage` (×), `purity` (0–100).
RNA assay fields: `n_reads` (NanoSim target), `expression.gene_multipliers`
(gene → scaling factor on the log-normal baseline).

See `config.yaml` for the full cohort.

## Tradeoffs / caveats

- **DNA reads are R10.4.1** via Badread's `nanopore2023` models — matches
  real HGSOC cohort chemistry.
- **RNA reads are R9.4.1 cDNA** via NanoSim's
  `human_NA12878_cDNA_Bham1_guppy` model. NanoSim's pre-trained set doesn't
  ship R10.4.1 cDNA. For the casetrack QC demo that doesn't matter; for
  caller benchmarking it would. See `containers/README.md` for the
  `NANOSIM_MODEL` override.
- **Two slices, ~1.1 Mb total** (`chr17_brca1` 1 Mb + `chr17_tp53` 100 kb)
  keep runtime around 8–10 minutes. Extend `config.yaml` with additional
  regions (e.g. CCNE1 on chr19) — all four prep + sim scripts loop over
  `reference.slices` automatically.
- **DNA faults are common in real cohorts too** — we only deliberately
  break RNA here because that matches proposal 0002 §4.5. To demo a DNA
  failure, edit `03_break_hgsoc_sim_02.sh` to target a DNA BAM, or write
  a sibling break script.
- **Don't treat the truth VCFs as production ground truth.** VISOR reports
  what it inserted; NanoSim reports transcript read counts; that's enough
  for a reproducibility check but not for variant-caller benchmarking.
