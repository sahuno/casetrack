# Proposal 0008 — Agentic-AI provenance layer (per-entity agent decisions)

| | |
|---|---|
| **Author** | Samuel Ahuno ([ekwame001@gmail.com](mailto:ekwame001@gmail.com)) |
| **Status** | **Draft / parked** (design captured 2026-05-20; not on the active roadmap) |
| **Date** | 2026-05-20 |
| **Target release** | TBD — not scheduled. Lives on branch `feature/agentic-ai-provenance`. |
| **Depends on** | Proposal 0001 (SQLite backend), Proposal 0002 (QC events + cascade), Proposal 0004 (Nextflow integration) |
| **Target HPC** | IRIS @ MSKCC (WekaFS, SLURM, Apptainer) |

## 0. Why this document exists

This proposal captures a design discussion (2026-05-20) about extending casetrack to record
**AI-agent decisions as first-class, per-entity provenance**. It is **parked**, not scheduled —
it is written down so the framing and the open questions survive, while immediate roadmap work
(v0.6.x lifecycle, v0.5.x batch_id, v1.0 flat-mode removal) proceeds on `main`.

The motivating question that started this: *can casetrack be pitched as "audited bookkeeping for
multi-modal agentic AI research"?* The honest answer (see §7) is **not yet** — the current
SU2C cohort (`casetrack_su2c_git`, 147 ONT methylation assays) demonstrates Claude-Code-assisted
*engineering* and a strong SLURM/Nextflow audit trail, but it contains **no agent-decision
provenance** (no `agent_id`, no `model_version`, no prompt/tool-call lineage). This proposal is
the gap-closing design plus the case study needed to make the pitch real.

## 1. Summary

Add an **agent-run analysis kind** that attaches an AI agent's decision to a biological entity
(patient / specimen / assay), recording:

- *which* agent + model snapshot made the call,
- *what it saw* (prompt + tool-definition hashes; transcripts offloaded),
- *what it decided* (verdict + tool-call sequence),
- *what it cost* (tokens, dollars, latency),
- *whether a human concurred* (gold label for concordance metrics).

The novel angle vs. existing LLM-observability tools (LangSmith, LangFuse, Helicone, W&B Traces,
MLflow Tracing, OpenLLMetry) is **cohort-centric, not run-centric** provenance: an agent decision
is bound to a patient/specimen/assay and inherits casetrack's QC + consent cascade. The question
casetrack answers and they do not: *"which patients had their tumor BAM reviewed by an AI agent,
with what verdict, and is that verdict still trusted under the current consent state?"*

## 2. Motivation

1. **Reproducibility of AI-in-the-loop research.** As agents start making analysis decisions
   (QC triage, model selection, rerun arbitration), those decisions become part of the scientific
   record and must be auditable — by a human reviewer, a journal, or future-you.
2. **The cascade is the differentiator.** casetrack already cascades QC + consent through the
   hierarchy (Proposal 0002). Extending that to agent verdicts means a consent revocation or a
   QC censor automatically invalidates downstream agent decisions on the same entity — something
   no agent-observability tool does, because they have no concept of a biological entity.
3. **It is additive.** An agent run is "just another analysis" in casetrack's existing model. No
   breaking changes to the v0.3 schema or the append flow.

## 3. Goals

- A new analysis kind `agent_review` (configurable name) at any level.
- Agent-specific columns (model id/version, prompt hashes, tokens, cost, verdict, human concurrence).
- A `provenance.jsonl` `agent` sub-object carrying reproducibility primitives (hashes, trace id, token counts, cost).
- A `casetrack append-agent-run` CLI verb that parses an agent transcript into the summary TSV + events.
- A `CASETRACK_AGENT_REVIEW` Nextflow subworkflow mirroring the three-phase pattern (run → summarize → append).
- At least one **real case study** with an agent-vs-human concordance number (see §6).

## 4. Non-goals (for this parked design)

- **Byte-identical replay.** casetrack records what the agent saw and did; it does not promise
  re-running the agent yields identical output (LLM nondeterminism, silent provider model drift).
  Framing is **audit, not replay**.
- A general LLM-observability product. We are not competing with LangSmith on trace UX.
- Per-token / per-reasoning-step capture. Tool-call granularity is sufficient.
- Real-time streaming ingestion. Batch append after the agent run completes is fine.

## 5. Design

### 5.1 Schema (TOML — additive analysis block)

```toml
[analyses.agent_review]
level         = "assay"        # or specimen / patient depending on action scope
column_prefix = "agent"
summary_tsv   = "agent_review_summary.tsv"

[analyses.agent_review.columns]
agent_id            = { type = "TEXT" }      # e.g. "claude-opus-4-7"
agent_provider      = { type = "TEXT" }      # anthropic / openai / local_vllm
model_version       = { type = "TEXT" }      # exact date-stamped snapshot
prompt_hash         = { type = "TEXT" }      # SHA256 of system + user prompts
prompt_id           = { type = "TEXT" }      # human-readable name; prompt text versioned in git
tool_calls_json     = { type = "TEXT" }      # JSON blob of tool-call sequence
tokens_in           = { type = "INTEGER" }
tokens_out          = { type = "INTEGER" }
cost_usd            = { type = "REAL" }
latency_s           = { type = "REAL" }
verdict             = { type = "TEXT", enum = ["pass", "warn", "fail", "abstain"] }
verdict_confidence  = { type = "REAL" }
human_concurred     = { type = "BOOLEAN" }   # gold label, set after human audit
```

This maps onto the existing append flow unchanged — the agent run is a normal analysis whose
columns happen to describe an LLM call. `{agent_review}_done` timestamp comes for free per the
`_done` convention.

### 5.2 Provenance JSONL `agent` sub-object

Alongside the existing per-append fields (`slurm_job_id`, `hostname`, `transaction_id`,
`results_checksum`, `schema_v_before/after`), agent runs add:

```json
{
  "agent": {
    "agent_id": "claude-opus-4-7",
    "model_version": "claude-opus-4-7-20260315",
    "provider": "anthropic",
    "prompt_hash": "sha256:abc123...",
    "system_prompt_hash": "sha256:def456...",
    "tool_definitions_hash": "sha256:789...",
    "temperature": 0.0,
    "seed": null,
    "trace_id": "trace_018E5...",
    "tokens": {"input": 12450, "output": 880, "cache_read": 11200},
    "cost_usd": 0.042
  }
}
```

Reproducibility primitives are **hashes**; the prompt text itself is versioned in git
(`prompts/agent_review_v3.md`) and the hash anchors which version ran. Full transcripts are
offloaded to object storage (see §8 cost note), referenced by hash.

### 5.3 CLI

```bash
casetrack append-agent-run \
  --analysis agent_review \
  --assay-id 17422_69_1 \
  --transcript transcript.jsonl \
  --verdict warn \
  --human-concurred-pending
```

`--transcript` is the agent's structured session log (Claude Code's session JSONL is close to
this already). casetrack parses tokens/cost/tool-calls and computes the prompt hashes.

### 5.4 Nextflow

Add `CASETRACK_AGENT_REVIEW` subworkflow alongside `MODKIT_MERGED_TRACKED`, same three-phase
shape: run agent → summarize to per-assay TSV → `casetrack append-agent-run`.

### 5.5 Cascade interaction (the differentiator)

Agent verdicts are read-filtered exactly like analysis results: censoring an assay (QC) or
revoking patient consent invalidates the agent verdict on that entity in `status`, `export`,
`query`, `cohort`. An invalidated agent verdict remains in the audit log (append-only), but is
excluded from active reads — same semantics as Proposal 0002 §4.4.

## 6. Case study required to make the pitch land (the hard half)

The schema work is easy and additive. The abstract hinges on a **real concordance result**.
Two options, ordered by effort:

- **Option A (recommended, ~1 weekend):** Run an agent over the 147 SU2C bedMethyl outputs to
  produce one automated QC verdict per assay. Human-audit a blind 30-sample subset. Headline:
  *"Agent flagged X/147; human–agent agreement κ = 0.Y on a 30-sample blind audit."*
- **Option B (bigger):** Agent-driven model selection — agent reads each tumor BAM's coverage /
  contamination / basecaller version and chooses the Clair3/ClairS model variant. Show the
  agent's choices match the maintainer's posted compatibility table at >X%.

Without one of these, the abstract is "we designed a schema for hypothetical agent runs," which
will not survive review at an AI-shaped venue.

## 7. Honest assessment of the current state (2026-05-20)

- `casetrack_su2c_git` proves: real 147-sample cohort, comprehensive per-append SLURM provenance
  (job id, host, checksum, transaction id), three-axis tracking (analysis + trace + versions),
  cascading QC scopes configured for real semantics.
- It does **not** prove anything agentic: no agent decision was recorded as data; Claude Code was
  used as a *coding partner*, not a *tracked decision-maker*.
- Therefore: **pitch cancer-genomics now** (SU2C is a strong validation anchor); treat agentic-AI
  as future work backed by this proposal + a §6 case study before claiming it.

## 8. Open questions / risks

1. **Model drift.** Providers update models silently; even temperature=0 is not stable across
   snapshots. Mitigation: pin date-stamped `model_version`; document in methods. Reviewers will ask.
2. **PHI leakage.** If prompts contain patient data, `provenance.jsonl` becomes PHI. Need a
   redaction layer or per-field opt-out before this touches real identifiable cohorts.
3. **Transcript storage cost.** Fine for hundreds of assays; painful at tens of thousands. Store
   hashes in-DB, offload transcripts to object storage.
4. **Verdict semantics.** What does `qc_fail` *mean* for an agent run vs. a deterministic tool?
   Needs a written rubric per `prompt_id` so verdicts are interpretable and the cascade is principled.
5. **Prior-art positioning.** Must clearly state the cohort-centric-vs-run-centric distinction
   against LangSmith / LangFuse / Helicone / W&B / MLflow / OpenLLMetry, or reviewers will call it
   a reskin of existing observability tooling.

## 9. Next actions (when this is unparked)

1. Run case study Option A on the 147 SU2C assays; compute κ.
2. Implement §5.1–5.3 behind the existing analysis-kind machinery (additive, no v0.3 schema break).
3. Add the `CASETRACK_AGENT_REVIEW` subworkflow.
4. Draft the agentic-AI abstract only after a real concordance number exists.
