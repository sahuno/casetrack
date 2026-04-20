# Proposal 0007 — Project lifecycle status (active / complete / archived)

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | **Draft** |
| **Date** | 2026-04-20 |
| **Target release** | v0.6.0 |
| **Depends on** | Proposal 0001 (SQLite backend, v0.3.0), Proposal 0005 (project identity, v0.5.0) |
| **Target HPC** | IRIS @ MSKCC (WekaFS, SLURM, Apptainer) |

## 0. Accepted decisions

| # | Question | Accepted answer |
|---|---|---|
| 1 | Storage location | `project_meta` table in `casetrack.db`. Registry JSON (`~/.casetrack/registry.json`) mirrors `last_seen` only — status is DB-authoritative. |
| 2 | Transition mechanism | **Manual only** (`casetrack project set-status --status <s>`). No auto-inference from completion rates (deferred). |
| 3 | Completion definition | Deferred. No quantitative threshold encoded in v0.6.0. `complete` means "the researcher decided this is done." |
| 4 | `archived` semantics | **Hard read-only gate.** `append`, `register`, `censor`, `uncensor`, `add-metadata` all refuse on an archived project (exit 2). `--force-archived --yes` is the override pair (matching existing `--allow-new --yes` idiom). Read-only commands (`status`, `export`, `query`, `dashboard`) always work. |
| 5 | QC cascade trigger | Deferred. No auto-transition based on censored specimen/assay rates. |
| 6 | MCP / AI default | `active` is the default filter. `casetrack_list_projects` and `casetrack_query` surface only `active` projects unless `--include-complete` or `--include-archived` is passed. |

## 1. Summary

Add a `status` column to `project_meta` with three values: `active` (default), `complete`, `archived`. Provide a `casetrack project set-status` command for manual transitions. Enforce a hard write-gate on archived projects. Surface status in all read paths and MCP tools.

This is intentionally minimal — the goal is to give the AI assistant (and dashboard) a reliable signal about which projects are live data sources, and to prevent accidental writes to frozen studies.

## 2. Motivation

Casetrack today has no notion of project lifecycle. Every project in the registry looks identical whether it is actively being sequenced, wrapped up months ago, or a retired pilot run. This causes two problems:

1. **AI context noise.** The MCP `casetrack_list_projects` tool returns every project ever initialized. An AI assistant asked to "check my active analyses" should not surface archived pilot studies from 2024.

2. **Accidental mutations on closed studies.** After a project's results are finalized and submitted, there is no guard against a pipeline re-run accidentally appending new rows or a mis-fired `casetrack register` creating phantom assays.

## 3. Goals

- Add `project_meta.status` with enum `active | complete | archived`.
- `casetrack project set-status` command with `--reason` (optional, logged to `provenance.jsonl`).
- Hard write-gate on `archived` projects across all mutation commands.
- `casetrack project status` command to display current status.
- `casetrack projects` list command respects `--status` filter (default: `active`).
- MCP `casetrack_list_projects` respects status filter.
- `migrate-status` subcommand to add the column to existing DBs.

## 4. Non-goals (v0.6.0)

- Auto-inference of `complete` from analysis completion rates (D3 deferred).
- QC-driven auto-transitions (D5 deferred).
- Status on specimen or assay rows (project-level only).
- Status history / audit trail (provenance.jsonl entry is sufficient for now).
- `complete` enforcing a write-gate (only `archived` does; `complete` is informational).

## 5. Schema

### 5.1 Migration

```sql
-- Idempotent; safe to run on existing DBs.
ALTER TABLE project_meta ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
    CHECK(status IN ('active', 'complete', 'archived'));
```

All existing projects silently become `active` — correct default.

### 5.2 project_meta table (after migration)

| column | type | notes |
|---|---|---|
| `project_id` | TEXT PK | unchanged |
| `name` | TEXT | unchanged |
| `schema_v` | TEXT | unchanged |
| `created_at` | TEXT | unchanged |
| `casetrack_version` | TEXT | unchanged |
| `status` | TEXT | `active` \| `complete` \| `archived`; default `active` |

## 6. Commands

### 6.1 `casetrack project set-status`

```
casetrack project set-status \
    --project-dir /path/to/proj \
    --status      complete \
    [--reason     "Final manuscript submitted 2026-04-20"]
```

- Validates `status` is one of the three values.
- Logs a `project_status_change` action to `provenance.jsonl` with `from`, `to`, `reason`, `timestamp`, `user`.
- No `--yes` required — status changes are reversible (just run again with `--status active`).
- `active → complete → archived` is the natural flow, but any direction is allowed.

### 6.2 `casetrack project status`

```
casetrack project status --project-dir /path/to/proj
```

Prints:

```
project:    hgsoc_pilot
status:     complete
changed:    2026-04-20T14:32:11  (by sahuno)
reason:     Final manuscript submitted
```

### 6.3 `casetrack projects` list filter

Existing `casetrack projects` command gains `--status` flag:

```bash
casetrack projects                    # default: active only
casetrack projects --status all       # every project
casetrack projects --status archived  # archived only
casetrack projects --status complete,active
```

### 6.4 `casetrack migrate-status`

```
casetrack migrate-status --project-dir /path/to/proj
```

Idempotent DDL migration (adds the `status` column if absent). Called automatically by any command that opens the DB if `status` column is missing, with a warning:

```
[casetrack] project_meta missing 'status' column — auto-migrating to v0.6 schema
```

## 7. Write-gate on `archived`

Any mutation command (`append`, `register`, `censor`, `uncensor`, `add-metadata`, `add-batch`, `link-sources`) checks `project_meta.status` at entry and raises:

```
Error: project 'hgsoc_pilot' is archived (status=archived).
       Mutation commands are refused on archived projects.
       To override: add --force-archived --yes
       To unarchive: casetrack project set-status --status active
```

Exit code: 2 (matching existing guard pattern).

Override pair: `--force-archived --yes` (both flags required, matching `--allow-new --yes` idiom).

`complete` projects are **not** gated — `complete` is informational only.

## 8. MCP / AI integration

`casetrack_list_projects` tool (proposal 0005, `casetrack_mcp/tools.py`) gains a `status` filter:

```python
# Default behaviour — only active projects
list_projects(status='active')

# Override
list_projects(status='all')
list_projects(status='complete')
```

This means an AI assistant doing `list_projects()` with no args will only see projects the researcher is actively working on. Archived pilot runs and finished studies stay invisible unless explicitly requested.

`casetrack_query` tool similarly defaults to `active` projects; the MCP schema exposes `status` as an optional filter param.

## 9. Provenance

Every `set-status` call appends to `provenance.jsonl`:

```json
{
  "action": "project_status_change",
  "timestamp": "2026-04-20T14:32:11",
  "user": "sahuno",
  "project_id": "hgsoc_pilot",
  "from_status": "active",
  "to_status": "complete",
  "reason": "Final manuscript submitted 2026-04-20",
  "casetrack_version": "0.6.0"
}
```

No separate audit table — the JSONL is the record.

## 10. Implementation plan

| Step | File | What |
|---|---|---|
| 1 | `casetrack_lifecycle/__init__.py` | new subpackage |
| 2 | `casetrack_lifecycle/schema.py` | `STATUS_DDL` constant; `migrate_status(conn)` function |
| 3 | `casetrack_lifecycle/lifecycle.py` | `cmd_set_status`, `cmd_project_status`; provenance write |
| 4 | `casetrack_lifecycle/gate.py` | `assert_not_archived(conn, project_dir)` helper imported by every mutation command |
| 5 | `casetrack.py` | wire `gate.assert_not_archived()` into `cmd_append_project`, `cmd_register`, `cmd_censor`, `cmd_uncensor`, `cmd_add_metadata` |
| 6 | `casetrack.py` | `casetrack projects` gains `--status` filter |
| 7 | `casetrack_mcp/tools.py` | `list_projects` / `query` gain `status` param; default `active` |
| 8 | `tests/test_lifecycle.py` | ~15 tests (see §11) |
| 9 | `casetrack.py` `main()` | `project set-status` + `project status` dispatch |

## 11. Test plan

| Test | What it verifies |
|---|---|
| `test_default_status_is_active` | New project has `status='active'` |
| `test_set_status_complete` | active → complete succeeds; provenance entry written |
| `test_set_status_archived` | active → archived succeeds |
| `test_reverse_archived` | archived → active succeeds |
| `test_write_gate_append` | `append` on archived exits 2 |
| `test_write_gate_register` | `register` on archived exits 2 |
| `test_write_gate_censor` | `censor` on archived exits 2 |
| `test_force_archived_override` | `--force-archived --yes` allows append |
| `test_complete_not_gated` | `append` on `complete` project succeeds |
| `test_projects_list_default_active` | `projects` returns only active |
| `test_projects_list_status_filter` | `projects --status complete` returns correct subset |
| `test_migrate_idempotent` | `migrate_status` runs twice without error |
| `test_migrate_auto_on_open` | opening old DB auto-adds column with warning |
| `test_provenance_logged` | provenance.jsonl has correct action entry |
| `test_mcp_list_default_active` | `casetrack_list_projects()` excludes archived |

## 12. Open questions (deferred)

| # | Question | When to revisit |
|---|---|---|
| Q1 | Auto-infer `complete` from analysis completion rate | When a concrete threshold emerges from real-study usage |
| Q2 | QC cascade (>X% censored → `needs_review`) | Proposal 0008 if motivated |
| Q3 | `archived` → compress/move to cold storage hook | If storage cost becomes a concern on WekaFS |
| Q4 | Status history table (full audit of every transition) | If JSONL provenance proves insufficient for audit |

## 13. Release notes entry (draft)

**v0.6.0 — Project lifecycle status**

Projects now carry a `status` field: `active` (default), `complete`, or `archived`. Use `casetrack project set-status --status archived` to freeze a finished study — all mutation commands will refuse on archived projects. The MCP assistant and `casetrack projects` list default to active-only, so finished studies stay out of AI context automatically. Run `casetrack migrate-status` to upgrade existing project DBs.
