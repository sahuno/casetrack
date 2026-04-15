# Changelog

All notable changes to `casetrack` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-04-15

First release past the prototype. 152 pytest tests, eight signed commits,
same single-file `casetrack.py` module.

### Added

- **`casetrack rerun`** — emit or submit sbatch commands for samples
  missing a given analysis. `--submit` dispatches and captures each
  SLURM job id into the provenance log; `--list-only` prints bare IDs
  for piping; `--extra` appends extra sbatch args.
- **`casetrack dashboard`** — generate a self-contained HTML report
  (summary metrics, per-analysis progress bars with expandable missing-
  samples lists, sample × analysis heatmap, provenance timeline with
  git short-hash). Fully offline, no external URLs, XSS-safe.
- **`casetrack add-metadata`** — attach metadata columns post-init. No
  `_done` timestamp, no schema entry. Strict collision policy by
  default; opt in via `--fill-only` or `--overwrite`.
- **`casetrack projects`** — cross-project overview. Walks a root for
  manifests matching `--pattern` up to `--max-depth`, skips hidden dirs
  and `sandbox/`. Table (with progress bars), tsv, or json output. One
  corrupted manifest warns + is skipped; does not abort.
- **Git provenance** — every log entry now carries a `git` block with
  `{commit, branch, dirty, toplevel}` of the process CWD. Fail-safe
  (missing git / non-repo / opt-out → `null`). Per-process cache keeps
  parallel appends fast. Dashboard surfaces the short hash inline.
- **`--yes` safety rail on `--allow-new`** — `append` and
  `add-metadata` now refuse to commit new sample rows unless `--yes`
  is also passed, previewing the IDs that would be added. Prevents
  typo'd sample IDs from silently expanding the manifest. Nextflow
  module pairs `--allow-new --yes` whenever
  `params.casetrack_allow_new = true`.
- **Nextflow DSL2 integration** (`examples/nextflow/`) — reusable
  `casetrack_append` + `casetrack_add_metadata` processes, a demo
  pipeline fanning two summarize steps through a single append gate,
  profiles for `standard` / `slurm` / `apptainer` / `test`, and a
  README covering three integration patterns (standalone,
  `afterScript`, collect-and-batch).
- **Claude Code QC hook** (`examples/claude/`) — Level 2 post-analysis
  shell hook. Invokes `claude --print` on a freshly-appended result,
  validates the returned TSV header, and appends the verdict as a
  `cc_<analysis>_review` analysis. Editable prompt template with
  `__SAMPLE_ID__` / `__ANALYSIS__` / `__RESULTS_TSV__` placeholders.
- **pytest suite** (`tests/`) — 152 tests covering every subcommand,
  concurrency (real multi-process `append`), git provenance, Nextflow
  module shell contract, Claude hook end-to-end with stubbed `claude`,
  and a 5000-sample smart-merge perf regression.

### Changed

- **Smart-merge is vectorized.** The `iterrows()` loop in the NaN-fill
  code path became `fill_nan_cells()`, a column-wise `merged_keys.map()`.
  A 5000 sample × 10 column fill went from minutes to ~0.05s.
- **Project tree reorganized** to match the synopsis layout:
  `docs/` for design notes, `examples/{scripts,nextflow,claude}/` for
  runnable artifacts, `sandbox/` for egg-info leftovers (gitignored
  for regenerables).
- **README rewritten** to document every current command, the three-
  phase SLURM pattern, the `--yes` rail, provenance shape, and the two
  integrations.

### Fixed

- `--allow-new` no longer silently admits typo'd sample IDs (was known
  issue #7 in the synopsis).

### Infrastructure

- Initial `git init` and first remote push to
  https://github.com/sahuno/casetrack (private).
- `.gitignore` for pyc / egg-info / pytest caches.

## [0.1.0] — prototype (pre-2026-04-15)

Single-file prototype with seven subcommands (`init`, `append`, `status`,
`validate`, `log`, `schema`, `export`), POSIX-flock-protected concurrent
append, and an append-only manifest model with a JSONL provenance sidecar
and a JSON schema sidecar. No tests.
