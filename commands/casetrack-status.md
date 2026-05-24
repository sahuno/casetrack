---
name: casetrack-status
description: Summarize the casetrack project in the current directory — entity counts, censored samples, stale cohort artifacts, and the next obvious work.
---

Run `casetrack status --project-dir .` for the current working directory and present a 5-line summary:

1. Total patients / specimens / assays (and how many are censored).
2. Most-recent activity timestamp from `provenance.jsonl`.
3. Any cohort artifacts flagged `stale`, `ref_stale`, or `derived_stale` (see the casetrack skill, §16 staleness orthogonality).
4. Pending analyses (assays with a NULL `{analysis}_done` column whose `qc_status = 'pass'`).
5. One concrete next command the user could run.

If `casetrack.db` is absent, suggest `casetrack init` and stop.

If the `casetrack` CLI is not on PATH, instruct the user to `pip install casetrack` (or `pip install -e ".[all]" --user` if they're working in the repo).
