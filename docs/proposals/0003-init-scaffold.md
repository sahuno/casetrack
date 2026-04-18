# Proposal 0003 — `casetrack init` scaffolds a full project tree

**Status**: draft
**Target release**: v0.4.2
**Breaking**: no (purely additive; opt-out with `--bare`)
**Author**: Samuel Ahuno

## Motivation

Projects accumulate layout debt. Three weeks in, figures live under `work/`, references are wherever they landed off `wget`, and the manuscript draft is a single `.docx` in the home dir. By the time a PI asks for the "project folder," it's a full day's work to assemble one.

Casetrack already enforces three files (`casetrack.db`, `casetrack.toml`, `provenance.jsonl`) as the analysis manifest. Extending `casetrack init` to scaffold a full directory tree paves the road for the other 90% of a project's artifacts — raw inputs, references, results, scripts, docs, manuscript figures, logs, containers — so a new project starts publication-ready on day 0.

## Scope

Currently, `casetrack init --project-dir <path>` writes 4 files into the target directory. This proposal extends that command to additionally create a fixed directory tree, with `.gitkeep` files in every leaf so the tree survives `git clone`.

A new `--bare` flag opts out for users who are retrofitting an existing layout.

## Directory tree

```
<project>/
├── casetrack.toml                  # schema + analysis column types (git-tracked)
├── casetrack.db                    # SQLite — source of truth (gitignored)
├── provenance.jsonl                # append-only audit log (git-trackable)
├── .gitignore                      # excludes db, wal/shm, raw data, sifs, large outputs
│
├── data/
│   ├── raw/                        # immutable inputs — never rewritten
│   ├── ref/                        # references: fastas, gencode GTFs, CpG islands,
│   │                               # chain files, chrom.sizes. Flat on purpose —
│   │                               # references aren't all genome-specific.
│   └── validation/                 # truth sets, ground-truth BEDs, benchmark VCFs
│
├── results/                        # analysis outputs
│                                   # Subdirs (e.g. results/modkit/<assay_id>/) are
│                                   # created by pipelines at append time. No fixed
│                                   # taxonomy — different analyses run over the life
│                                   # cycle of a project.
│
├── scripts/                        # top-level analysis scripts (01_, 02_, ...)
│                                   # General-purpose project code, NOT manuscript
│                                   # figure composition.
│
├── docs/
│   ├── research/                   # literature notes, prior-work summaries
│   └── hypothesis/                 # pre-registered hypotheses, analysis plans
│
├── manuscript/
│   ├── figures/
│   │   └── scripts/                # figure COMPOSITION code for the manuscript
│   │       │                       # (distinct from top-level scripts/ — these
│   │       │                       # assemble final publication figures from
│   │       │                       # per-analysis outputs in results/)
│   │       ├── png/                # rendered manuscript figures — PNG
│   │       ├── pdf/                # PDF
│   │       └── svg/                # SVG
│   ├── draft/                      # working manuscript drafts
│   ├── proofs/                     # journal proofs / revisions
│   └── references/                 # bib files, reference PDFs
│
├── logs/                           # SLURM + CLI logs
├── containers/                     # Apptainer .sif files
└── sandbox/                        # ad-hoc / migration artifacts
```

### What is NOT pre-created

- `results/<analysis>/<assay_id>/` — populated by pipelines at first `casetrack append` with matching paths. Every project runs a different mix of analyses over its life cycle, so hardcoding a taxonomy would be wrong.
- `data/ref/<genome>/` — references aren't all genome-scoped (annotation files, chain files, etc. transcend builds). If a project wants per-genome subdirs, the user makes them.

### Why `.gitkeep` in every leaf

Empty directories don't survive `git clone`. Each leaf gets a zero-byte `.gitkeep` so the scaffold round-trips through git.

## CLI contract

```
casetrack init --project-dir <path>              # scaffolds full tree (default)
casetrack init --project-dir <path> --bare       # emits only the 4 files
```

All scaffolding is **idempotent**: re-running `init` on a project with the tree already present is a no-op — no warnings, no `.gitkeep` overwrites, no errors.

## Updated `.gitignore` default

```gitignore
# casetrack SQLite + WAL/SHM
casetrack.db
casetrack.db-wal
casetrack.db-shm

# Large artifacts — tracked in the manifest, not in git
data/raw/*
!data/raw/.gitkeep
containers/*.sif
results/**/*.bam
results/**/*.bam.bai
results/**/*.cram
results/**/*.cram.crai
results/**/*.bedMethyl.gz
results/**/*.tbi
results/**/*.vcf.gz
results/**/*.fastq.gz

# Exports and working artifacts
exports/
sandbox/*
!sandbox/.gitkeep
```

The `!data/raw/.gitkeep` negation ensures the directory survives commit even though its contents are gitignored.

## Implementation

Single point of change: `cmd_init_project` in `casetrack.py`.

```python
SCAFFOLD_LEAVES = [
    "data/raw",
    "data/ref",
    "data/validation",
    "results",
    "scripts",
    "docs/research",
    "docs/hypothesis",
    "manuscript/figures/scripts/png",
    "manuscript/figures/scripts/pdf",
    "manuscript/figures/scripts/svg",
    "manuscript/draft",
    "manuscript/proofs",
    "manuscript/references",
    "logs",
    "containers",
    "sandbox",
]

def _scaffold_project_tree(project_dir: Path) -> None:
    for leaf in SCAFFOLD_LEAVES:
        d = project_dir / leaf
        d.mkdir(parents=True, exist_ok=True)
        (d / ".gitkeep").touch(exist_ok=True)
```

Invoked after the existing file-writes in `cmd_init_project`, gated by the new `--bare` flag.

`SCAFFOLD_LEAVES` is a module-level constant so tests can import and assert against it.

## Tests

Add to `tests/test_init.py`:

1. `test_init_creates_full_scaffold` — default init produces every dir in `SCAFFOLD_LEAVES` plus a `.gitkeep` in each.
2. `test_init_bare_skips_scaffold` — `--bare` produces only the 4 files; none of the tree leaves exist.
3. `test_init_idempotent` — re-running `init --project-dir <same>` doesn't raise, and `.gitkeep` file mtimes don't change (modulo filesystem granularity).
4. `test_gitignore_contains_new_patterns` — the default `.gitignore` includes `data/raw/*`, `containers/*.sif`, `results/**/*.bam`, `!data/raw/.gitkeep`.

## Docs

- **README.md** — new "Project layout" section, short, showing the tree.
- **CHANGELOG.md** — entry under v0.4.2.
- **CLAUDE.md** (repo) — append a "Project layout" paragraph with a pointer to this proposal.

## Open questions

1. Should `casetrack doctor` report missing scaffold directories as a warning? **Recommendation**: yes, but as `INFO`, not `WARN` — projects retrofitted via `--bare` shouldn't be treated as unhealthy.
2. Should `--scaffold {full,bare,minimal}` be a tri-state instead of a binary `--bare`? **Recommendation**: binary, because a middle tier invites bikeshedding. Users who want partial layouts can `rm` after init.

## Migration

None required. Existing projects keep their current layout. Running `casetrack init --project-dir <existing>` on a project with the legacy 4-file layout is safe: the db/toml/provenance checks pass, and the scaffold dirs + `.gitkeep` files are added idempotently.
