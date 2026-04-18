# Containers for the hgsoc_sim demo

Six images are needed. All are pullable from public registries without auth.
Total disk: ~500 MB.

## Recommended pulls (Apptainer — IRIS-style)

```bash
# Set this to wherever your local SIF stash lives.
# Matches the layout at profiles/software_configs/softwares_containers_config.yaml:
#   LOCAL_SOFTWARE_PATH_PERSONAL: /data1/greenbab/users/ahunos/apps/containers/
CONTAINER_DIR="${CONTAINER_DIR:-$HOME/apps/containers}"
mkdir -p "$CONTAINER_DIR"

# ── DNA lane ──

# VISOR — haplotype construction (HACk). LASeR's built-in long-read
# simulation is NOT used; reads come from Badread for R10.4.1 fidelity.
apptainer pull --dir "$CONTAINER_DIR" \
    visor_1.1.2.1.sif \
    docker://quay.io/biocontainers/visor:1.1.2.1--pyh7cba7a3_0

# Badread — R10.4.1 ONT read simulation (nanopore2023 error + qscore models).
apptainer pull --dir "$CONTAINER_DIR" \
    badread_0.4.1.sif \
    docker://quay.io/biocontainers/badread:0.4.1--pyhdfd78af_0

# ── RNA lane ──

# gffread — slice + rewrite GENCODE GTF → transcript FASTAs.
apptainer pull --dir "$CONTAINER_DIR" \
    gffread_0.12.9.sif \
    docker://quay.io/biocontainers/gffread:0.12.9--hf426362_0

# NanoSim — transcriptome-mode cDNA read simulation.
apptainer pull --dir "$CONTAINER_DIR" \
    nanosim_3.2.3.sif \
    docker://quay.io/biocontainers/nanosim:3.2.3--hdfd78af_2

# ── Shared ──

# minimap2 — aligner for both DNA (map-ont) and RNA (splice) reads.
apptainer pull --dir "$CONTAINER_DIR" \
    minimap2_2.28.sif \
    docker://quay.io/biocontainers/minimap2:2.28--he4a0461_0

# samtools — sort / index / downsample.
apptainer pull --dir "$CONTAINER_DIR" \
    samtools_1.21.sif \
    docker://quay.io/biocontainers/samtools:1.21--h50ea8bc_0
```

The demo scripts resolve images via `$CONTAINER_DIR`. Export it before
running `run_demo.sh`, or edit the scripts if your stash is elsewhere.

## Docker fallback

Same images via Docker on a dev laptop:

```bash
docker pull quay.io/biocontainers/visor:1.1.2.1--pyh7cba7a3_0
docker pull quay.io/biocontainers/badread:0.4.1--pyhdfd78af_0
docker pull quay.io/biocontainers/gffread:0.12.9--hf426362_0
docker pull quay.io/biocontainers/nanosim:3.2.3--hdfd78af_2
docker pull quay.io/biocontainers/minimap2:2.28--he4a0461_0
docker pull quay.io/biocontainers/samtools:1.21--h50ea8bc_0
```

Then `export RUNNER=docker` and the scripts route through `docker run` instead.

## Pipeline overview

### DNA lane (`02_run_visor.sh`)

| Tool      | Step  | Role |
|-----------|-------|------|
| VISOR HACk | 2.1 | turns the per-haplotype variant BEDs into two haplotype FASTAs |
| Badread    | 2.2 | R10.4.1 genomic reads per haplotype (`nanopore2023` models) |
| Badread    | 2.3 | normal-contamination reads from the raw reference (when purity < 100) |
| minimap2   | 2.4 | aligns the merged FASTQs (`-ax map-ont`) |
| samtools   | 2.5 | sort + index → `<ASSAY>/sim.srt.bam` |

Coverage mix at `coverage_x`, `purity = P`:
- each tumor haplotype contributes `coverage_x × P / 200`
- the reference contributes `coverage_x × (1 − P/100)`
- at `P = 100`, no contamination reads emitted

### RNA lane (`02b_run_nanosim.sh`, ships in phase f)

| Tool      | Step  | Role |
|-----------|-------|------|
| gffread    | (00b) | slices GENCODE GTF + extracts transcript FASTAs per slice |
| NanoSim    | 2b.1  | cDNA read simulation from transcripts + per-specimen expression vector |
| minimap2   | 2b.2  | splice-aware alignment (`-ax splice`) |
| samtools   | 2b.3  | sort + index → `<ASSAY>/sim.srt.bam` |

Per-specimen expression vectors come from `config.yaml`'s `expression_profile`
blocks — a log-normal baseline with per-transcript/gene multipliers for
tumor/normal differentials (e.g. `BRCA1: 0.1` in a tumor with BRCA1 LOF).

## Chemistry caveats

**DNA reads**: R10.4.1 via Badread's `nanopore2023` models — matches the HGSOC
cohort. Good.

**RNA reads**: R9.4.1 via NanoSim's `human_NA12878_cDNA_Bham1_guppy` model.
NanoSim's pre-trained model set does not yet ship R10.4.1 cDNA. The
cDNA-vs-DNA distinction matters more than R9-vs-R10 for exercising
casetrack's multi-assay QC paths, so this is an acceptable compromise.

If R10.4.1 cDNA matters for your use, two options:
1. Train a custom NanoSim model from your real R10.4.1 cDNA data via
   `read_analysis.py transcriptome` — out of scope for this demo.
2. Use the `human_giab_hg002_sub1M_kitv14_dorado_v3.2.1` R10.4.1 model —
   but it's genomic, not cDNA, so reads won't have the cDNA error profile.
   Override via `NANOSIM_MODEL=human_giab_hg002_sub1M_kitv14_dorado_v3.2.1`.
