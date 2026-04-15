# casetrack × Claude Code integration

Lets Claude Code review each analysis result and write a QC-flag column
back into the manifest — so the manifest carries both the raw numbers and
an independent LLM verdict, all traceable via provenance.

This directory implements **Level 2** of the three integration levels in
`docs/CASETRACK_SYNOPSIS.md`:

| Level | What it is                                         | What this dir ships |
|-------|----------------------------------------------------|---------------------|
| 1     | Interactive chat, human reads `casetrack status`   | (no code needed)    |
| 2     | Non-interactive post-analysis hook → QC column     | **yes, below**      |
| 3     | SDK-driven autonomous agent loop                   | (future work)       |

## Prereqs

- `casetrack` on `$PATH` (`pip install -e ".[all]" --user` from repo root).
- `claude` (Claude Code CLI) on `$PATH`.
- `ANTHROPIC_API_KEY` exported (or your site-specific auth flow completed).
- The analysis must produce a per-sample results TSV with `sample_id` as
  the key column. (This is already the casetrack contract.)

Sanity check:

```bash
which casetrack && casetrack --help | head -5
which claude    && claude --help | head -5
```

## Files in this directory

| File                      | Role                                       |
|---------------------------|--------------------------------------------|
| `post_analysis_hook.sh`   | The shell hook you call after each job.    |
| `qc_review_prompt.md`     | The prompt template the hook substitutes.  |
| `README.md`               | This doc.                                  |

## Contract

The hook reads these env vars:

| Var           | Required | Meaning                                       |
|---------------|----------|-----------------------------------------------|
| `SAMPLE_ID`   | yes      | Row key                                       |
| `ANALYSIS`    | yes      | Name of the just-completed analysis           |
| `MANIFEST`    | yes      | Path to `manifest.tsv`                        |
| `RESULTS_TSV` | yes      | Path to the per-sample summary TSV just logged|
| `CC_BIN`      | no       | Override the claude binary (default `claude`) |
| `CASETRACK_BIN` | no     | Override casetrack (default `casetrack`)      |
| `PROMPT_FILE` | no       | Override prompt template path                 |
| `REVIEW_DIR`  | no       | Where to write the intermediate TSV (default `$PWD`) |

It emits the following **two new manifest columns** under the analysis
`cc_${ANALYSIS}_review`:

- `cc_${ANALYSIS}_qc_pass` — literal `True` / `False`
- `cc_${ANALYSIS}_qc_note` — free-text rationale, ≤ 120 chars
- `cc_${ANALYSIS}_review_done` — timestamp (injected by `casetrack append`)

Failures surface via exit codes:

| Exit | Meaning                                  |
|------|------------------------------------------|
| 1    | Missing env or input file                |
| 3    | Claude invocation failed                 |
| 4    | Review TSV header mismatch               |
| 5    | Review TSV had no data rows              |

## SLURM integration

Drop this at the end of your sbatch script, **after** the main casetrack
append succeeded:

```bash
# ... phases 1 and 2 of the standard casetrack pattern ...
casetrack append \
    --manifest "$MANIFEST" \
    --results summary.tsv \
    --key sample_id \
    --analysis modkit

# phase 3b: have claude code review and log a QC column
export SAMPLE_ID ANALYSIS=modkit MANIFEST RESULTS_TSV=summary.tsv
bash /path/to/post_analysis_hook.sh
```

On IRIS with Apptainer, you usually don't need to bind anything extra —
the hook only reads an existing TSV and calls two binaries. If `claude`
lives inside a container:

```bash
CC_BIN='apptainer exec /path/to/claude.sif claude' \
    bash /path/to/post_analysis_hook.sh
```

## Customizing the prompt

Edit `qc_review_prompt.md`. The placeholders `__SAMPLE_ID__`,
`__ANALYSIS__`, and `__RESULTS_TSV__` are substituted via bash parameter
expansion before the prompt is sent, so you can freely rewrite the
surrounding text, reorder sections, or add analysis-specific rules.

If you want per-analysis prompts, point `PROMPT_FILE` at different files:

```bash
PROMPT_FILE="$HOME/prompts/qc_review_${ANALYSIS}.md" \
    bash post_analysis_hook.sh
```

The header validation in the hook enforces the output shape, so any
prompt edit that still produces the three expected columns in the right
order will work.

## Level 3 future work

A Python driver using the Claude Code SDK that:

1. Reads `casetrack status --fmt json` to find incomplete samples.
2. Plans the next batch of work (which analyses to run, in what order).
3. Dispatches via `casetrack rerun --submit`.
4. Polls SLURM until completion.
5. Closes the loop by running the Level-2 hook on each result.

Not shipped here — it needs a concrete pipeline to target and some
thought about budget/rate-limit behavior. Worth revisiting once you have
a project driving ≥ 100 samples per analysis.
