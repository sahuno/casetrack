# Containers for the hgsoc_sim demo

Four images are needed. All are pullable from public registries without auth.
Total disk: ~300 MB.

## Recommended pulls (Apptainer — IRIS-style)

```bash
# Set this to wherever your local SIF stash lives.
# Matches the layout at profiles/software_configs/softwares_containers_config.yaml:
#   LOCAL_SOFTWARE_PATH_PERSONAL: /data1/greenbab/users/ahunos/apps/containers/
CONTAINER_DIR="${CONTAINER_DIR:-$HOME/apps/containers}"
mkdir -p "$CONTAINER_DIR"

# VISOR — haplotype construction + long-read simulation.
apptainer pull --dir "$CONTAINER_DIR" \
    visor_1.1.2.1.sif \
    docker://quay.io/biocontainers/visor:1.1.2.1--pyh7cba7a3_0

# samtools — BAM indexing / downsampling for the "broken pair" step.
apptainer pull --dir "$CONTAINER_DIR" \
    samtools_1.21.sif \
    docker://quay.io/biocontainers/samtools:1.21--h50ea8bc_0

# minimap2 — not used in the default VISOR-LASeR path (LASeR aligns
# internally), but recommended if you swap in Badread for R10.4.1 reads.
apptainer pull --dir "$CONTAINER_DIR" \
    minimap2_2.28.sif \
    docker://quay.io/biocontainers/minimap2:2.28--he4a0461_0

# Badread — optional, for R10.4.1 modelling (see "Follow-up" below).
apptainer pull --dir "$CONTAINER_DIR" \
    badread_0.4.1.sif \
    docker://quay.io/biocontainers/badread:0.4.1--pyhdfd78af_0
```

The demo scripts resolve images via `$CONTAINER_DIR` — export it in your
shell before running `run_demo.sh`, or edit `scripts/02_run_visor.sh` if
your stash is elsewhere.

## Docker fallback

If you're on a dev laptop instead of IRIS, the same images work via Docker:

```bash
docker pull quay.io/biocontainers/visor:1.1.2.1--pyh7cba7a3_0
docker pull quay.io/biocontainers/samtools:1.21--h50ea8bc_0
docker pull quay.io/biocontainers/minimap2:2.28--he4a0461_0
docker pull quay.io/biocontainers/badread:0.4.1--pyhdfd78af_0
```

Set `RUNNER=docker` in your shell and the scripts will route through
`docker run --rm -v $PWD:$PWD -w $PWD` instead of `apptainer exec`.

## Follow-up — R10.4.1 reads via Badread

VISOR LASeR ships with **pbsim2**, which models R9.4.1 ONT reads. For
realistic R10.4.1 (matching the HGSOC cohort's chemistry), replace
`02_run_visor.sh` with:

1. `VISOR HACk` to build the tumor/normal haplotype FASTAs (unchanged).
2. `badread simulate --reference hackout/h1.fa --quantity 30x
      --error_model nanopore2023 --qscore_model nanopore2023`
      → per-haplotype FASTQ.
3. Concat + shuffle haplotype FASTQs (weighted for tumor purity).
4. `minimap2 -ax map-ont ref.fa reads.fq | samtools sort -o sim.srt.bam`.

Not wired up by default because pbsim2-R9.4.1 is sufficient for exercising
the casetrack v0.4 QC paths; the realism upgrade matters for caller
benchmarking, not for demoing the CLI.
