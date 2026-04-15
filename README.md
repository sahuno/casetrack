# casetrack

Manifest-centric case management for bioinformatics pipelines on HPC.

**One manifest. One row per sample. Every analysis appends columns.**

```
sample_id  bam_path       modkit_mean_meth  modkit_done   tldr_l1_count  tldr_done
SAMPLE_01  /data/s01.bam  0.72              2026-04-14    14             2026-04-14
SAMPLE_02  /data/s02.bam  0.81              2026-04-14    3              2026-04-14
SAMPLE_03  /data/s03.bam                                  7              2026-04-14
```

## Install on IRIS

```bash
# Clone or copy to your project
cd /path/to/your/project
git clone https://github.com/sahuno/casetrack.git
cd casetrack

# Install (user-level, no sudo needed)
pip install -e . --user

# Or with Excel/Parquet export support
pip install -e ".[all]" --user

# Verify
casetrack --help
```

## Quick start

```bash
# 1. Create a samples list
cat > samples.txt << EOF
SAMPLE_001
SAMPLE_002
SAMPLE_003
EOF

# 2. Initialize manifest (optionally with metadata)
casetrack init \
    --manifest manifest.tsv \
    --samples samples.txt

# 3. Initialize with existing metadata
casetrack init \
    --manifest manifest.tsv \
    --samples samples.txt \
    --metadata sample_info.tsv

# 4. Run your analysis, then append results
sbatch run_modkit.sh SAMPLE_001 /path/to/bam manifest.tsv

# 5. Check status
casetrack status --manifest manifest.tsv
```

## Commands

### `casetrack init`

Create a new manifest from a sample list.

```bash
casetrack init \
    --manifest manifest.tsv \
    --samples samples.txt \
    --metadata sample_info.tsv \  # optional: adds columns from a TSV
    --cols bam_path,condition      # optional: pre-create empty columns
```

### `casetrack append`

Append analysis results as new columns. Uses file locking for concurrent SLURM safety.

```bash
casetrack append \
    --manifest manifest.tsv \
    --results results/modkit/SAMPLE_001/summary.tsv \
    --key sample_id \
    --analysis modkit_methylation
```

Flags:
- `--overwrite` — replace existing columns if they already exist
- `--allow-new` — permit sample IDs not yet in the manifest

### `casetrack status`

See what's done at a glance.

```
$ casetrack status --manifest manifest.tsv

Manifest: manifest.tsv
Samples:  50
Columns:  18
───────────────────────────────────────────────────────
Analysis                         Done  Total       %
───────────────────────────────────────────────────────
modkit_methylation                 48     50   96.0% ████████░░
tldr_insertions                    50     50  100.0% ██████████
qc_metrics                        45     50   90.0% █████████░
───────────────────────────────────────────────────────

  Missing for modkit_methylation: SAMPLE_033, SAMPLE_047
```

Output formats: `--fmt table` (default), `--fmt tsv`, `--fmt json`

### `casetrack validate`

Check manifest integrity: duplicate keys, null IDs, orphaned columns, schema consistency.

```bash
casetrack validate --manifest manifest.tsv --key sample_id
```

### `casetrack log`

View provenance — who ran what, when, from which SLURM job.

```
$ casetrack log --manifest manifest.tsv --last 5

  2026-04-14T10:32:15  APPEND (modkit_methylation)  sahuno [SLURM 12345] — 5 cols, 1 samples
  2026-04-14T10:33:02  APPEND (modkit_methylation)  sahuno [SLURM 12346] — 5 cols, 1 samples
  2026-04-14T11:15:44  APPEND (tldr_insertions)     sahuno [SLURM 12400] — 4 cols, 1 samples
```

### `casetrack schema`

See which analysis contributed which columns.

```bash
casetrack schema --manifest manifest.tsv
```

### `casetrack export`

Export to Excel, CSV, JSON, or Parquet.

```bash
casetrack export --manifest manifest.tsv --output manifest.xlsx
```

## The three-phase SLURM pattern

Every SLURM job follows the same structure:

```bash
# Phase 1: Run analysis
apptainer exec container.sif tool input output

# Phase 2: Summarize to per-sample TSV
python3 scripts/summarize_tool.py --input output --sample $SAMPLE_ID --output summary.tsv

# Phase 3: Append to manifest
casetrack append --manifest manifest.tsv --results summary.tsv --key sample_id --analysis tool_name
```

See `examples/` for complete SLURM scripts.

## Writing a new summarize script

Each analysis needs a small Python script that distills raw output into manifest columns.
The contract:

1. Accept `--input`, `--sample`, `--output` arguments
2. Produce a TSV with `sample_id` as the first column
3. Include only the metrics you want in the manifest (keep it lean)
4. Full results stay in the per-sample results directory

```python
# Template: scripts/summarize_newtool.py
row = {
    "sample_id": args.sample,
    "newtool_metric1": compute_metric1(data),
    "newtool_metric2": compute_metric2(data),
}
pd.DataFrame([row]).to_csv(args.output, sep="\t", index=False)
```

## Project structure

```
project/
├── manifest.tsv                    # The single source of truth
├── manifest.tsv.provenance.jsonl   # Audit trail
├── manifest.tsv.schema.json        # Column-to-analysis mapping
├── manifest.tsv.lock               # File lock (transient)
├── samples.txt                     # Sample ID list
├── results/
│   ├── modkit/{sample_id}/         # Full modkit output per sample
│   ├── tldr/{sample_id}/           # Full TLDR output per sample
│   └── qc/{sample_id}/
├── scripts/
│   ├── summarize_modkit.py
│   ├── summarize_tldr.py
│   └── summarize_qc.py
├── logs/                           # SLURM logs
└── containers/                     # Apptainer/Singularity .sif files
```

## Concurrency safety

`casetrack append` uses POSIX `flock` for exclusive file locking. When multiple
SLURM array tasks finish simultaneously, they queue up and write one at a time.
The lock is held only during the read-merge-write cycle (typically <1 second).

## Integration with Claude Code

After a Claude Code agent runs an analysis on IRIS, it can call casetrack directly:

```bash
# In a Claude Code session or hook
casetrack append \
    --manifest manifest.tsv \
    --results agent_output/summary.tsv \
    --key sample_id \
    --analysis claude_code_report
```

## License

MIT
