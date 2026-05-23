# HANDOFF — region-scoped artifacts (proposal 0013)

**Created:** 2026-05-22 17:55 EDT
**Author:** Samuel Ahuno (via Claude)
**Status at handoff:** Spec + plan committed. **Implementation NOT started.** Zero code changes yet.

---

## One-paragraph context

Proposal 0013 adds a nullable `region_scope` to cohort artifacts (0009) and a nullable `role` to
their inputs, so DMR/DE-style contrasts and panel-restricted runs can record the genomic territory
they cover and their tumor/normal design. The single load-bearing idea: a `region_scope` label that
matches a registered reference key (0010) auto-captures a cohort-scope `reference_usage` edge, so
scope-version changes drive the **existing** `ref_stale` flag — no new staleness code, no new tables.
Decisions: **B** (region-scoped artifacts, not a findings store), **C** (reference-resolve door),
**A1** attachment (cohort artifacts only, A3-compatible later), roles folded in.

## Resume in a fresh session — do this first

1. `cd /data1/greenbab/users/ahunos/apps/casetrack`
2. `git checkout feature/region-scoped-artifacts-0013` (branch already exists; PR #23 open)
3. `git log --oneline -5` — confirm HEAD is the plan commit (see "Expected HEAD" below).
4. Read, in order:
   - **The plan:** `docs/superpowers/plans/2026-05-22-region-scoped-artifacts.md` ← the task-by-task spec to execute
   - **The proposal:** `docs/proposals/0013-region-scoped-artifacts.md` (§0 locked decisions, §5 design, §7 rejected)
   - The casetrack skill (invoke the `casetrack` Skill) for command/ontology context.
5. Invoke **`superpowers:subagent-driven-development`** and execute the plan task-by-task
   (8 tasks, each red→green→commit). Dispatch a fresh subagent per task, review between.

## Expected HEAD at resume

```
<this commit> docs: add 0013 implementation plan + handoff
7ed4e2f       docs: add proposal 0013 — region-scoped artifacts + contrast roles
```

## The 8 tasks (from the plan — see plan for full code + tests)

1. **Schema** — `cohort_artifacts.region_scope` + `cohort_artifact_inputs.role` (nullable);
   `ensure_region_scope_columns` (idempotent ALTER + `idx_cohort_artifacts_scope` index);
   fold into `ensure_cohort_artifacts_schema`. → `casetrack_qc/cohort_artifacts.py`
2. **CRUD** — `insert_artifact(region_scope=)`, `add_artifact_inputs(roles=dict)`,
   `artifact_input_roles()`; add `region_scope` to dataclass + `_ARTIFACT_COLS`.
3. **`migrate-region-scope`** command + `casetrack_qc/cli.py` parser + dispatch.
4. **`append-cohort`** — `--region-scope`, `assay:role` parsing in `_read_inputs` (now returns
   `(ids, roles)`), **reference-resolve auto-capture** via `reference_artifacts.record_usage(scope="cohort")`.
5. **`cohort-artifacts`** — show `region_scope`, add `--scope` filter.
6. **`_cohort_artifacts` view** (`casetrack_qc/reader.py`) — `region_scope` + derived `scope_ref_key`,
   presence-guarded across all 3 tiers.
7. **Surface** in `status` (`_emit_cohort_artifacts_section`), dashboard (`_cohort_artifacts_html` +
   its info dict ~casetrack.py:5716), MCP (`casetrack_mcp/tools.py:cohort_artifacts_tool`). Export is free.
8. **Full suite + v0.11.0 bump** (`setup.py`, `casetrack.py:_CASETRACK_VERSION`) + docs
   (CHANGELOG, README, CLAUDE.md, flip proposal status, casetrack skill SKILL.md). Push, update PR #23.

## Two "read-before-write" gotchas flagged in the plan

- **Task 6 test** uses a DuckDB query helper — open `tests/test_cohort_artifacts_readpaths.py`
  and copy its *exact* query-helper invocation; don't assume `run_project_query`.
- **Task 7 MCP test** needs a registered `project_id` — reuse the existing MCP test's
  registration fixture (grep `cohort_artifacts_tool` under `tests/`); skip-gate consistently
  if MCP tests are environment-gated in this repo.

## Key facts to not re-derive

- `reference_artifacts.record_usage(scope="cohort", artifact_id=, ref_key=, version_used=, transaction_id=)`
  already exists and is idempotent (DELETE+INSERT on `(artifact_id, ref_key)` via
  `idx_refusage_cohort`). The reference-resolve door just calls it. **No new staleness code.**
- All cohort CLI wiring is in `casetrack_qc/cli.py::build_qc_subparsers` + `qc_command_dispatch`,
  NOT in casetrack.py's argparse.
- `ensure_cohort_artifacts_schema` is called from `init`, `append-cohort`, and `migrate-cohort` —
  folding `ensure_region_scope_columns` into it means all three paths get the columns automatically.
- ALTER-column idempotency pattern to mirror: `casetrack_qc/schema.py` (`_column_exists`).
- Version lives in: `setup.py` (0.10.0→0.11.0), `casetrack.py:96` (`_CASETRACK_VERSION`),
  CLI help strings (tagged `[v0.11]` in the plan), CHANGELOG/tags.
- Test style to mirror: `tests/test_cohort_artifacts_schema.py` (fixtures, `_init_project`).
- Commit trailer (every commit): `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.

## Done = 

All 8 tasks green, `python3 -m pytest tests/ -q` passes, v0.11.0 bumped, proposal 0013 status
flipped to Shipped, PR #23 updated. Deferred (do NOT scope-creep into this PR): per-region findings
store (C), interval/overlap queries, A2 sample-level scope, A3 any-node scope — all proposal 0013 §7.
