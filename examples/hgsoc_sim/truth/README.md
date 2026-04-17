# truth/ — ground-truth artifacts for the simulated cohort

VISOR HACk writes a `variants.bed.gz` alongside each haplotype FASTA
summarizing exactly what it inserted. Those files are the ground-truth
records for the simulated variants — use them as the "truth set" if you
plug a variant caller into the demo.

After `scripts/02_run_visor.sh` completes, find them under:

```
sandbox/hgsoc_sim/cohort/<PATIENT>/<SPECIMEN>/hack/h1.bed.gz
sandbox/hgsoc_sim/cohort/<PATIENT>/<SPECIMEN>/hack/h2.bed.gz
```

The format mirrors VISOR HACk's input BED — six tab-separated columns
(chrom, start, end, type, info, extra). A small helper can aggregate these
into a single per-patient truth TSV; not included by default since the
scaffold focuses on the casetrack QC story, not on caller benchmarking.

## Why not ship a pre-built truth VCF?

Two reasons:
1. The BAMs aren't checked in — so a "truth VCF" committed here would
   drift from whatever BAMs a user regenerates.
2. VISOR's own output is already ground truth in a deterministic format;
   turning it into a VCF adds a dependency on `bcftools` or similar
   without making anything more correct.

If you want a VCF, convert with a short Python script or use
`VISOR --vcf` (not enabled by default in v1.1.2.1).
