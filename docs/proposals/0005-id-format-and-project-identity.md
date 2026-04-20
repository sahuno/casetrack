# Proposal 0005 — Hierarchy ID format enforcement + project identity

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | ✅ Fully shipped 2026-04-19 (Part A + Part B alpha/beta/final + MCP wrapper) |
| **Date** | 2026-04-19 |
| **Target release** | v0.6.0 |
| **Breaking** | Yes — new required field (`project_id`) + stricter validation on patient/specimen/assay IDs. One-shot migration path. |
| **Depends on** | Proposal 0001 (normalized backend, shipped v0.3.0); Proposal 0004 (Nextflow integration, v0.4.0) |

## Shipped progress snapshot (2026-04-19)

| Part | Status | Notes |
|---|---|---|
| **Part A — hierarchy ID format enforcement** | ✅ shipped | Validators wired into `cmd_register`, `_insert_rows_by_level` (migrate), `cmd_add_metadata_project --allow-new`. Recover paths tolerant of legacy IDs by design. 37 new tests. casetrack commit `1849cc6`. |
| **Part A — Nextflow integration surface** | ✅ shipped | Part A surfaces through `casetrack register` called from init scripts. Negative smoke test (`test/run_test_malformed.sh`) + tutorial notes shipped in `casetrack-nf-subworkflows` commit `9f958a1`. |
| **Part A — `casetrack doctor --id-format`** | ✅ shipped | Scan-only health check + rename suggestions; `--fmt table\|tsv`. casetrack commit `9e4369e`. |
| **Part B alpha — project identity (`project_id`, `project_meta`, registry)** | ✅ shipped | `_PROJECT_ID_PATTERN`, `project_meta` table, `~/.casetrack/registry.json` (single-user), `casetrack init --project-id`, `casetrack --project <id>`, `casetrack projects {list,register,deregister,scan}`. casetrack commit `3b3f5f1`. 51 new tests. Legacy v0.5 projects continue to work — no enforcement. |
| **Part B beta — `casetrack migrate-project-id`** | ✅ shipped | Interactive single-project + `--scan` batch mode. Idempotent; refuses slug conflicts; writes provenance entry per migration. casetrack commit `3a678fa`. 15 new tests. |
| **Part B final — hard requirement gate** | ✅ shipped | Runtime refuses un-migrated projects at every command (read + write). `CASETRACK_ALLOW_LEGACY=1` bypass for audits; upgrade-path commands (`migrate-qc`, `migrate-project-id`, `recover`) bypass internally. casetrack commit `48dcbf2`. 18 new tests. |
| **Part B — MCP wrapper for AI agents** | ✅ shipped | `casetrack_mcp/` subpackage + `casetrack-mcp` console script. Two tools (`casetrack_list_projects`, `casetrack_query`). Closed-world project lookup, read-only SQL, 10k row cap, hard-gate respected with `CASETRACK_ALLOW_LEGACY` bypass. casetrack commit `5c517d0`. 29 new tests. Install: `pip install casetrack[mcp]`. |

## 0. Accepted decisions

| # | Question | Accepted answer |
|---|---|---|
| 1 | Hierarchy ID format rule | ASCII: `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` for `patient_id`, `specimen_id`, `assay_id`. Plus reject `.` / `..` literals; case-insensitive duplicate check within a level. Per-level `id_pattern` override in TOML for cohorts that need to loosen it. Default strict. |
| 2 | Non-ASCII IDs | Disallowed by default. Opt-in via `[project] allow_unicode_ids = true`. Rationale: most bioinformatics tools mangle non-ASCII silently. |
| 3 | Project identity shape | Split `project_id` (machine-queryable slug, required, immutable) from `project.name` (free-form human label, unchanged from today). |
| 4 | `project_id` format | DNS-label shape: `^[a-z0-9][a-z0-9-]{2,63}$`. Lowercase, hyphens allowed, 3–64 chars. Matches Docker repo / Kubernetes namespace conventions. |
| 5 | `project_id` persistence | Both: written to `casetrack.toml` `[project]` block AND into a new one-row `project_meta` SQLite table. Cross-checked at every command — mismatch is a hard error. |
| 6 | `project_id` mutability | **Immutable after init.** Renaming invalidates every notebook, script, and registry entry that references it. If you must "rename," archive + re-init is the honest path. |
| 7 | Registry scope | **Single-user for now** (`~/.casetrack/registry.json`). Team-shared registry is noted for later implementation (§8, open question Q2). |
| 8 | Registry uniqueness | **Registry-unique for now.** Globally unique UUID backing is noted for later implementation (§8, open question Q3). |

## 1. Motivation

Two gaps in the current schema, shipped together because they share a migration vector (both touch `casetrack init` and the `[project]` block) and reinforce each other (a strict `project_id` format is the same decision as strict hierarchy IDs, one namespace simpler).

### 1A. Hierarchy IDs are `TEXT` with no format enforcement

`patient_id`, `specimen_id`, `assay_id` are validated today only for `NOT NULL` + `UNIQUE` + FK. Anything else goes: spaces, tabs, shell metacharacters, path separators, leading hyphens, null bytes, emoji.

This silently breaks at distance from the mistake:

- A `patient_id` with whitespace makes `casetrack query` fail with "No rule to produce"-style errors (the same papercut `@rules/snakemake.md` calls out for sample sheets).
- `patient_id = "P01;rm -rf /"` is a SQL-injection vector the moment any wrapper builds a query via string concat (several `examples/` wrappers do this for expedience).
- IDs ending up in `{patient_id}` path template slots produce broken filesystem paths — `results/modkit/v1/P 01/...` creates a directory named `P` and a file named `01`.
- `HG006` and `hg006` as two separate patient rows is almost always a typo that survives because SQLite is case-sensitive by default.

Fix: validate format at insert time with a single regex + two semantic checks. Loud failures at the root of the problem, not three scripts downstream.

### 1B. "Project" is a directory path, not an addressable entity

Today:
- `[project] name = "..."` is a free-form label written once at `init`, used only as the dashboard `<h1>`. Not stored in the DB. Not cross-checked against anything.
- The canonical identifier for "which project am I looking at" is the absolute path to the project directory — a string that moves when a WekaFS mount changes, when a sandbox is rsynced, when a project is archived to cold storage.
- AI agents, SLURM wrappers, and humans all address projects via free-text paths. An LLM asked to "query the HGSOC cohort" has to either be told the path verbatim or guess at likely paths — both unreliable.

The user-facing goal (verbatim from the 2026-04-19 discussion): *"clearly label casetrack database so that anyone including AI agent can query specific projects, close the gap between data and insight while reducing hallucinations."*

Fix: introduce `project_id` as a machine-addressable slug, persist it inside the DB so the DB is self-describing, and add a single-user registry so `casetrack --project hgsoc-2026 query ...` resolves without path memorization.

## 2. Goals

1. ✅ **Shipped.** Reject malformed hierarchy IDs at `register` / `migrate` / `add-metadata --allow-new` time with a clear error naming the offending value and the rule it violated. (`append` doesn't create IDs — it requires them to exist — so the effective INSERT surface is covered.)
2. ✅ **Shipped (Part B alpha).** Give every casetrack project a globally-meaningful-within-a-registry identifier that does not depend on filesystem location. New projects get a `project_id` at init; `casetrack --project <id>` resolves via `~/.casetrack/registry.json`.
3. ✅ **Shipped (Part B alpha).** Make every `casetrack.db` self-describing — `SELECT project_id FROM project_meta` answers "what project is this" without reading the TOML. Cross-checked against the TOML on every command.
4. ✅ **Shipped.** Give AI agents a closed-world project lookup: `list_projects()` → `query(project_id, sql)`. Shipped as the `casetrack_mcp/` subpackage with the `casetrack-mcp` stdio server (commit `5c517d0`). Tools refuse unknown `project_id`s with the valid set enumerated, reject non-SELECT SQL, enforce the v0.6 hard gate, and cap result rows at 10,000.
5. ✅ **Shipped.** Keep the escape hatch: existing projects with non-conforming IDs (real LIMS IDs with colons, cohorts imported from legacy systems) can opt out via `[levels.<level>] id_pattern` / `allow_case_variants` / `[project] allow_unicode_ids` in TOML.

## 3. Non-goals

- Changing the three-level hierarchy (patient/specimen/assay) itself — that's proposal 0001 territory.
- Centralizing project metadata across users or machines — team-shared registry is explicitly deferred to a later iteration (§8 Q2).
- Globally unique IDs that survive cross-machine archive/restore cycles — UUID backing is deferred to a later iteration (§8 Q3).
- Renaming support for `project_id` — explicitly immutable (§0 #6).
- Validating the *content* of IDs for biological coherence (e.g. "this patient_id looks like a sample_id"). Out of scope.

## 4. Design — Part A: hierarchy ID format ✅ shipped v0.6.0-part-a

### 4.1 The rule

One regex applies to all three levels (the actual shipped code uses `\A`/`\Z` instead of `^`/`$` because Python's `$` matches before a trailing `\n` by default — same meaning, stricter anchor):

```python
_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
```

Plus two semantic checks:

1. **Reserved literals rejected**: `.`, `..`, and empty-after-strip are never valid.
2. **Case-insensitive duplicate check**: within a level, `casefold()`-equal IDs cannot coexist. `HG006` and `hg006` in the same `patients` table → hard error at insert time naming both IDs. Opt out per-level via `[levels.<level>] allow_case_variants = true`.

### 4.2 What the regex catches

| Input | Rejected because |
|---|---|
| `"P 01"` | whitespace |
| `"P\t01"` | whitespace |
| `"P01\n"` | whitespace |
| `"P01;rm"` | `;` not in allowed set |
| `"-P01"` | leading hyphen (CLI flag ambiguity) |
| `".hidden"` | leading dot (hidden file on Unix) |
| `"P01/v2"` | path separator |
| `"P01\x00"` | null byte |
| `""` / `"   "` | empty / whitespace-only |
| `"."` / `".."` | path traversal (reserved-literal check) |
| `"αβγ"` | non-ASCII (unless `allow_unicode_ids = true`) |
| 65+ chars | length limit |

### 4.3 What the regex allows

| Input | Valid |
|---|---|
| `P01` | ✓ plain |
| `HG006_PAY77227` | ✓ underscore |
| `MSK-001` | ✓ hyphen mid-string |
| `HG002.v2` | ✓ dot mid-string |
| `2026_cohort_A` | ✓ starts with digit |
| up to 64 chars | ✓ within length |

### 4.4 Escape hatch: per-level `id_pattern`

Projects with legacy LIMS IDs containing colons (`MSK-001:2024`) or other non-standard characters can override the default:

```toml
[levels.patient]
key = "patient_id"
id_pattern = "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"   # allows colons, 80 chars max

[levels.patient.columns]
patient_id = { type = "TEXT", required = true, unique = true }
```

The override is validated at `init` / schema-reload time (it must itself be a valid Python regex and anchor with `^` / `$`).

### 4.5 Length budget — why 64

- SLURM job name limit: 255
- Filesystem filename limit: 255
- When the ID gets joined into a path like `results/modkit_pileup/20260419_hg38_v1/P01/SPEC_A/ASSAY_001/summary.tsv`, the three IDs contribute ~120 chars in the worst case. 64 per ID leaves ~63 chars of headroom for `{tool}/{run_tag}/` + `summary.tsv`.
- 64 is the git-short-SHA length users are already used to seeing.

## 5. Design — Part B: project identity ✅ alpha shipped v0.6.0a1

### 5.1 Two fields, two purposes

```toml
[project]
project_id = "hgsoc-methylation-2026"       # machine — slug, required, immutable
name       = "HGSOC methylation cohort, spring 2026"   # human — free-form label
schema_v   = 1
created    = "2026-04-19 10:32:14"
```

- `project_id` is what scripts, LLMs, and registry entries use. Stable across rename, mount changes, rsync.
- `name` is what shows up in dashboard `<h1>`. Free-form, mutable, not unique.

### 5.2 `project_id` format

```python
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
```

Lowercase only, hyphens allowed, 3–64 chars. DNS-label shape. Matches Docker Hub repository names and Kubernetes namespace rules — conventions users have seen before.

Why stricter than hierarchy IDs (no dots, no underscores, lowercase only): `project_id` appears in URL paths and CLI flags (`casetrack --project hgsoc-2026`). Minimizing surprise in those contexts matters more than matching heterogeneous LIMS conventions.

### 5.3 Persistence: DB + TOML + registry

Three writes at `init`, all three cross-checked at every subsequent command:

1. **`casetrack.toml`** `[project] project_id = "..."` — human-editable source.
2. **`project_meta` SQLite table** — one row, columns: `project_id, name, schema_v, created_at, casetrack_version`. The DB is self-describing: `SELECT project_id FROM project_meta` answers "which project is this" without reading TOML.
3. **`~/.casetrack/registry.json`** — one entry per known project. Schema:
   ```json
   {
     "schema_v": 1,
     "projects": {
       "hgsoc-methylation-2026": {
         "path": "/data1/greenbab/users/ahunos/cohorts/hgsoc_2026",
         "name": "HGSOC methylation cohort, spring 2026",
         "created": "2026-04-19T10:32:14",
         "last_seen": "2026-04-19T15:47:02"
       }
     }
   }
   ```

Cross-check rules at command start:
- **TOML vs DB `project_id` mismatch** → hard error. Someone copied `casetrack.db` into the wrong project directory, or edited `project_id` in TOML after init. Either way, refuse to proceed.
- **Registry stale path** → `last_seen` updated on every successful command. Warn (don't fail) if the registry entry points at a different path than the one currently in use; auto-update on `casetrack doctor`.
- **Registry missing entry** (project was `init`'d before v0.6, or registry was deleted) → auto-register on first command, log to `provenance.jsonl`.

### 5.4 Immutability

`project_id` is set once at `init` and cannot be changed. The `[project]` block in `casetrack.toml` can be edited — the cross-check at command start will catch any drift and fail loudly.

Rationale: the whole point of `project_id` is that it's a stable handle. Allowing rename re-introduces the identity-drift problem at a higher level. Users who truly need to rename a project should:
1. `casetrack export` → TSVs
2. `casetrack init` a new project with the new `project_id`
3. Re-append the TSVs

This is destructive-by-choice, visible in `provenance.jsonl`, and correctly invalidates every notebook that hardcoded the old `project_id`.

### 5.5 New CLI surface

```bash
# Init: project_id required unless --project-id-auto derives one from directory name.
casetrack init --project-dir /data/hgsoc_2026 \
               --project-id hgsoc-methylation-2026 \
               --project-name "HGSOC methylation cohort, spring 2026"

# Query by project_id instead of --project-dir. Resolves via registry.
casetrack --project hgsoc-methylation-2026 query "SELECT * FROM patients LIMIT 5"

# Enumerate known projects (reads registry, one row per project_id).
casetrack projects list
casetrack projects list --json    # LLM-friendly

# Manually (re)register a project (e.g. restored from archive on a new machine).
casetrack projects register --project-dir /new/path/to/hgsoc_2026

# Remove a registry entry without touching the project directory.
casetrack projects deregister hgsoc-methylation-2026
```

`--project-dir` remains supported — both modes coexist. The `--project <id>` form is additive, not a replacement.

### 5.6 AI-agent contract

The hallucination-reduction lever is that agents never see filesystem paths. An MCP wrapper exposes two tools:

```
casetrack_list_projects() -> list[{project_id, name, last_seen}]
casetrack_query(project_id: str, sql: str) -> rows
```

`project_id` is a closed enum (drawn from the registry). If an agent asks for a project_id not in the registry, the tool returns a fail-fast error listing the valid set. Agents cannot invent a path that "might work" because paths are never in the tool signature.

## 6. Schema additions

### 6.1 New table: `project_meta` ✅ shipped v0.6.0a1

```sql
CREATE TABLE project_meta (
    project_id         TEXT NOT NULL PRIMARY KEY,
    name               TEXT NOT NULL,
    schema_v           INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    casetrack_version  TEXT NOT NULL,
    CHECK (project_id GLOB '[a-z0-9]*' AND length(project_id) BETWEEN 3 AND 64)
);
```

One row per database. Inserted at `init`, never updated, never deleted.

The `CHECK` constraint is a defense-in-depth echo of the Python-side regex — even someone hand-editing the SQLite file can't land a malformed `project_id`.

### 6.2 TOML additions

Part A ✅ shipped — `[project] allow_unicode_ids`, `[levels.<level>] id_pattern`, `[levels.<level>] allow_case_variants` are live and validated at `load_schema()` time. Part B alpha ✅ shipped — `[project] project_id` is now written by `casetrack init` and validated at `load_schema()` time:

```toml
[project]
project_id = "hgsoc-methylation-2026"  # ✅ Part B alpha — auto-derived or
                                        #   passed via --project-id at init.
                                        #   Optional in TOML for legacy
                                        #   projects until v0.6.0 final.
allow_unicode_ids = false              # ✅ Part A — default false (ASCII-only)
name       = "HGSOC methylation cohort"   # existing: free-form
schema_v   = 1
created    = "..."
```

Per-level (Part A ✅ shipped):

```toml
[levels.patient]
key                 = "patient_id"
id_pattern          = "..."            # optional regex override, must anchor ^/$
allow_case_variants = false            # default false
```

### 6.3 Registry file ✅ shipped v0.6.0a1

`~/.casetrack/registry.json` — created on first `casetrack init` on a given user account. fcntl-locked atomic writes via `_registry_locked()` context manager. Path overridable via `CASETRACK_REGISTRY` env var (used by tests + power users with non-default home layouts).

## 7. Migration path

✅ **Shipped (Part B beta) — `casetrack migrate-project-id`** (commit `3a678fa`):

```bash
$ casetrack migrate-project-id --project-dir /data/hgsoc_2026

[/data/hgsoc_2026]
  Current project name: 'HGSOC methylation cohort, spring 2026'
  Suggested project_id: hgsoc-methylation-cohort-spring-2026
  Enter project_id (Enter to accept, ^C to abort): hgsoc-2026
Migrated  /data/hgsoc_2026  → project_id='hgsoc-2026' (updated: toml, project_meta, registry)

Done: 1 migrated, 0 no-op, 0 skipped.
```

- Slug suggestion via `suggest_project_id()`: lowercase the `name`, collapse runs of non-alnum to hyphens, strip leading/trailing hyphens, truncate to 64 chars.
- User can override the suggestion with any valid slug at the prompt, or pass `--project-id <slug>` to skip the prompt.
- `--yes` accepts the auto-suggestion non-interactively (required for `--scan`, recommended for automation).
- Idempotent: a project already wired through TOML + `project_meta` + registry is a no-op.
- Refuses to overwrite drift (TOML `project_id` ≠ DB `project_meta.project_id`) — the user must resolve manually before migration can proceed.
- Refuses to claim a slug that's already in the registry pointing at a different directory (chosen v0.6 design call: keep the user in control rather than auto-suffixing). The error tells them to either pass `--project-id <other>` or `casetrack projects deregister <slug>` first.
- Batch mode: `casetrack migrate-project-id --scan /data1/greenbab/... --yes` walks the tree (uses `_find_v03_projects`), migrates every casetrack project missing a `project_meta` row.
- Each successful migration writes a `migrate_project_id` entry to `provenance.jsonl` with the list of artifacts touched (`["toml", "project_meta", "registry"]`).
- Exit codes: 0 if all targets either migrated or no-op'd, 1 if any were skipped (so CI can catch silent failures).

For hierarchy IDs, `casetrack doctor --id-format` ✅ shipped — scans all three tables, reports non-conforming IDs, and exits non-zero if any are found. Each violation includes a `_suggest_clean_id()` heuristic rename when the cleaned slug passes the default regex; otherwise the report flags "no safe suggestion — manual rename needed." No auto-rename — patient/specimen/assay renames have FK cascade implications that shouldn't be automatic. Output: `--fmt table` (default, human-readable) or `--fmt tsv` (machine-readable for CI).

### 7.1 Backward compatibility with v0.5 and earlier projects

- ✅ **Alpha (tolerant).** Projects without a `project_meta` table or without `[project] project_id` in TOML continued to work normally; the cross-check silently skipped when either side was absent.
- ✅ **Beta.** Added the `casetrack migrate-project-id` command so users could upgrade their legacy projects.
- ✅ **Final.** Runtime now refuses un-migrated projects with a one-line migration suggestion. Override via `CASETRACK_ALLOW_LEGACY=1` env var (documented bypass for read-only audits of inherited legacy cohorts). Upgrade-path commands (`migrate-qc`, `migrate-project-id`, `recover`) bypass the gate internally — they operate on legacy state by definition.
- ✅ **Shipped.** Hierarchy IDs that already exist and violate the new regex: commands continue to work on read paths (query, export, dashboard, recover); INSERT paths (register, migrate, add-metadata --allow-new) reject malformed values on new rows until the offending existing row is renamed or `id_pattern` is loosened in TOML. Rationale: strict-on-new, tolerant-of-existing.

## 8. Open questions

### Q1 — Registry location on shared filesystems

Single-user `~/.casetrack/registry.json` is accepted for v0.6.0 (§0 #7). The HPC home directories at MSKCC are per-user, so two researchers working on the same cohort will each maintain their own registry with a possibly-different `project_id` pointing at the same path.

Later-iteration proposal (punted): a team-shared registry at `/data1/greenbab/.casetrack/registry.json` with file-locking on write + a `--registry <path>` flag to point casetrack at it. Implementation complications worth resolving before shipping it:
- Permissions: who can register/deregister. Likely a unix-group write bit on the registry file, but that requires setting up the group membership consistently.
- Write contention: SQLite-style WAL isn't available for plain JSON; need a `flock`-based wrapper or switch the registry to a SQLite file.
- Identity conflicts: two users registering the same `project_id` pointing at different paths. Detect and require manual disambiguation.

### Q2 — Globally unique IDs across machines

Registry-unique `project_id` is accepted for v0.6.0 (§0 #8). This is enough while all projects live on one HPC cluster with one user's registry.

Later-iteration proposal (punted): UUID backing. Schema becomes `project_id` (human-readable slug, registry-unique) + `project_uuid` (globally unique, immutable, generated at init). The UUID is the stable handle for cross-machine archive/restore cycles; the slug is the human face. Precedent: PostgreSQL's `oid` (stable) + `relname` (renameable) dual-identity. Worth implementing when the first real archive-and-restore-to-a-different-machine scenario lands.

### Q3 — `project_id` in provenance records

Every `provenance.jsonl` entry should include `project_id` so archived logs are self-describing even when separated from the DB. This is effectively a schema bump on provenance. Minor — just agreeing to the field before implementation.

### Q4 — Interaction with `casetrack register` ✅ resolved

Part A shipped: `cmd_register` validates `--id` and `--parent` before opening the DB, surfacing errors like `Error: patient_id 'HG 006' is not a valid identifier. Must match '\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z' ...` Verified end-to-end through the Nextflow integration (`casetrack-nf-subworkflows/test/run_test_malformed.sh`, commit `9f958a1`).

### Q5 — What happens to `[project] name` mutability

`name` is already free-form, mutable, not cross-checked. Proposal leaves it that way. But: should the dashboard surface a warning when TOML `name` has drifted from DB `project_meta.name`? Probably — silent drift is low-stakes but still a quality signal. Punt to implementation time.

### Q6 — `allow_unicode_ids` and summary-TSV encoding

If a project opts in to `allow_unicode_ids`, the per-assay summary TSVs must be UTF-8, and every tool in the chain (modkit, samtools, pandas, bedtools) must round-trip non-ASCII correctly. In practice this is fragile — need to document loudly that opt-in is "you tested this end-to-end and it works," not a free pass.

## 9. Rollout plan

1. ✅ **v0.6.0-part-a (shipped 2026-04-19)**: hierarchy ID format enforcement landed in `casetrack.py` commit `1849cc6` + tests `test_id_format.py` (37 tests) + Nextflow integration tests in `casetrack-nf-subworkflows` commit `9f958a1` + `casetrack doctor --id-format` scanner in commit `9e4369e`. **Deviation from original plan**: shipped as enforcement-by-default, not warn-mode alpha, because the validator errors are actionable and the escape hatches (`id_pattern`, `allow_case_variants`, `allow_unicode_ids`) cover every legitimate legacy case.
2. ✅ **v0.6.0-part-b alpha (shipped 2026-04-19)**: `project_id` + `project_meta` table + `--project <id>` resolver + registry read+write path + `casetrack projects list` / `register` / `deregister` / `scan` shipped in commit `3b3f5f1` with 51 new tests. No enforcement — new projects get `project_id`, legacy v0.5 projects continue to work because cross-check skips when either TOML or DB lacks the field.
3. ✅ **v0.6.0-part-b beta (shipped 2026-04-19)**: `casetrack migrate-project-id` interactive single-project + `--scan` batch mode shipped in commit `3a678fa` with 15 new tests. Idempotent; refuses drift + slug conflicts; provenance entry per migration. Hard-requirement gate intentionally deferred to step 4.
4. ✅ **v0.6.0 final (shipped 2026-04-19)**: hard error on un-migrated projects at command start. `_resolve_project` now calls `require_project_identity_or_fail` for every read or write command. Bypass via `CASETRACK_ALLOW_LEGACY=1` for one-off audits; upgrade-path commands (`migrate-qc`, `migrate-project-id`, `recover`) bypass internally. Shipped in commit `48dcbf2` with 18 new tests.
5. ⏳ **v0.7.x**: revisit team-shared registry (Q1) and UUID backing (Q2) as separate proposals.

### Remaining Part A items

✅ All Part A items shipped. Part A is complete.

### Remaining Part B items

✅ All Part B items shipped. Part B is complete.

### Proposal 0005 — fully shipped

Everything in the proposal body landed in v0.6.0a1 + beta + final + MCP wrapper over the 2026-04-19 session. The only outstanding work is the punted-to-later open questions in §8 (team-shared registry Q1, UUID backing Q2) — tracked for a future proposal when a concrete driver lands, not blocking.

## 10. References

- Proposal 0001 — SQLite normalized backend (established the three-level hierarchy `project_id` will sit above)
- Proposal 0002 — QC events + censoring (established the `provenance.jsonl` + TOML config pattern this proposal reuses)
- Proposal 0003 — init scaffold (established that `casetrack init` is the right place to do project-level decisions)
- Proposal 0004 — Nextflow integration (consumer of `project_id` via `--casetrack_project_dir` — will gain `--casetrack_project_id` in v0.6)
- RFC 1035 §2.3.1 — DNS label format (source of the `project_id` regex shape)
- Docker Hub repository naming rules — same convention
