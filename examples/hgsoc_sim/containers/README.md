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

# VISOR — haplotype construction (HACk). LASeR's built-in long-read
# simulation is NOT used; we drive reads via Badread for R10.4.1 fidelity.
apptainer pull --dir "$CONTAINER_DIR" \
    visor_1.1.2.1.sif \
    docker://quay.io/biocontainers/visor:1.1.2.1--pyh7cba7a3_0

# Badread — R10.4.1 ONT read simulation (nanopore2023 error + qscore models).
apptainer pull --dir "$CONTAINER_DIR" \
    badread_0.4.1.sif \
    docker://quay.io/biocontainers/badread:0.4.1--pyhdfd78af_0

# minimap2 — aligner for the Badread FASTQs.
apptainer pull --dir "$CONTAINER_DIR" \
    minimap2_2.28.sif \
    docker://quay.io/biocontainers/minimap2:2.28--he4a0461_0

# samtools — sort / index / downsample.
apptainer pull --dir "$CONTAINER_DIR" \
    samtools_1.21.sif \
    docker://quay.io/biocontainers/samtools:1.21--h50ea8bc_0
```

The demo scripts resolve images via `$CONTAINER_DIR` — export it in your
shell before running `run_demo.sh`, or edit `scripts/02_run_visor.sh` if
your stash is elsewhere.

## Docker fallback

If you're on a dev laptop instead of IRIS, the same images work via Docker:

```bash
docker pull quay.io/biocontainers/visor:1.1.2.1--pyh7cba7a3_0
docker pull quay.io/biocontainers/badread:0.4.1--pyhdfd78af_0
docker pull quay.io/biocontainers/minimap2:2.28--he4a0461_0
docker pull quay.io/biocontainers/samtools:1.21--h50ea8bc_0
```

Set `RUNNER=docker` in your shell and the scripts will route through
`docker run --rm -v $PWD:$PWD -w $PWD` instead of `apptainer exec`.

## Pipeline overview — what each tool does

| Tool      | Step  | Role |
|-----------|-------|------|
| VISOR HACk | 2.1 | turns the per-haplotype variant BEDs into two haplotype FASTAs |
| Badread    | 2.2 | R10.4.1 read simulation per haplotype (`nanopore2023` models) |
| Badread    | 2.3 | R10.4.1 normal-contamination reads from the raw reference (when purity < 100) |
| minimap2   | 2.4 | aligns the merged FASTQs to the reference (`map-ont` preset) |
| samtools   | 2.5 | sorts + indexes the resulting BAM |

The read mix is weighted so total coverage = `coverage_x`:
- each tumor haplotype contributes `coverage_x × purity / 200`
- the reference contributes `coverage_x × (1 − purity/100)`

At `purity = 100`, no contamination reads are emitted.

## Why not VISOR LASeR's built-in simulator?

VISOR LASeR bundles pbsim2, which models R9.4.1 (ONT 2018-era) reads. The
real HGSOC cohort this demo is patterned on uses R10.4.1 — ~2× better
per-base accuracy and a different systematic error profile. Badread's
`nanopore2023` models match that chemistry directly, so downstream tools
(modkit, sniffles, etc.) behave closer to production.

If you need a faster, container-free fallback, `VISOR LASeR` is still
invokable from the same container and will produce usable R9.4.1 BAMs —
swap the Step-2 section of `02_run_visor.sh` accordingly.
