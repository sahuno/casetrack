#!/usr/bin/env python3
"""bench_report.py — post-run benchmarking for the HPC pipeline.

Reads casetrack's provenance.jsonl for this run, queries SLURM sacct for
every append/register provenance entry that carries a slurm_job_id, and
emits a markdown report under examples/hgsoc_sim/hpc/benchmarks/.

Report sections:
  1. Per-phase wall clock table (min, max, median across jobs in a phase)
  2. CPU-hours consumed per phase
  3. Cohort state (from `casetrack status --usable` + cohort --pair-by)
  4. Per-specimen methylation summary
  5. Cross-reference of provenance action → SLURM job IDs

No assumption that sacct has finished accounting every job — falls back
to "N/A" if sacct returns nothing for a job_id.

Author: Samuel Ahuno <ekwame001@gmail.com>
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def _sacct(job_ids: list[str]) -> dict[str, dict]:
    """Return {job_id: {state, elapsed_s, cpu_seconds}} via sacct."""
    if not job_ids:
        return {}
    res = subprocess.run(
        ["sacct", "-j", ",".join(job_ids), "-X",
         "--format=JobID,State,ElapsedRaw,TotalCPU,ReqCPUS", "-P", "-n"],
        capture_output=True, text=True, check=False,
    )
    out = {}
    for line in res.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        job_id, state, elapsed_raw, total_cpu, req_cpus = parts[:5]
        try:
            elapsed_s = int(elapsed_raw)
        except ValueError:
            elapsed_s = 0
        out[job_id] = {"state": state, "elapsed_s": elapsed_s,
                       "total_cpu": total_cpu, "req_cpus": req_cpus}
    return out


def _format_elapsed(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    project_dir = Path(args.project_dir)
    prov_path = project_dir / "provenance.jsonl"
    if not prov_path.exists():
        sys.exit(f"provenance not found: {prov_path}")

    # Bucket provenance entries by action → collect their slurm_job_ids.
    entries = []
    for line in prov_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # The SLURM jobs we care about were launched by submit_pipeline.sh.
    # We identify them by action + analysis. register entries carry no
    # useful job_id (they're invoked by the bootstrap script on the
    # login node); append entries from the pipeline DO carry slurm_job_id.
    phase_buckets = defaultdict(list)        # phase_name → [slurm_job_id]
    for e in entries:
        jid = e.get("slurm_job_id")
        if not jid:
            continue
        action = e.get("action", "?")
        analysis = e.get("analysis", "")
        if action == "append":
            if analysis == "premerge_flagstat":
                phase_buckets["premerge_flagstat"].append(jid)
            elif analysis == "merge":
                phase_buckets["merge_ont"].append(jid)
            elif analysis.startswith("modkit_"):
                phase_buckets["modkit_merged"].append(jid)
            elif analysis == "mock_scrna":
                phase_buckets["mock_scrna"].append(jid)
            elif analysis == "attach_bams":
                phase_buckets["attach_bams"].append(jid)
            else:
                phase_buckets[f"other:{analysis}"].append(jid)
        elif action == "censor":
            phase_buckets["censor"].append(jid)

    # synth_align doesn't land a casetrack append — its job IDs live in
    # $SANDBOX/synth/*/*/ log filenames, which we don't parse here.
    # Users who want synth_align timings can grep sacct directly.

    all_jids = sorted({j for jids in phase_buckets.values() for j in jids})
    stats = _sacct(all_jids)

    # ── Build report ─────────────────────────────────────────────────────────
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(out_path, "w") as f:
        f.write(f"# hgsoc_sim HPC run — benchmark\n\n")
        f.write(f"- **Project**: `{project_dir}`\n")
        f.write(f"- **Provenance entries**: {len(entries)}\n")
        f.write(f"- **Jobs accounted**: {len(stats)} / {len(all_jids)}\n")
        f.write(f"- **Generated**: {stamp}\n\n")

        f.write("## Per-phase wall clock\n\n")
        f.write("| Phase | Jobs | Min | Median | Max | CPU-hours |\n")
        f.write("|---|---:|---:|---:|---:|---:|\n")
        for phase in sorted(phase_buckets):
            jids = phase_buckets[phase]
            elapsed = [stats[j]["elapsed_s"] for j in jids if j in stats]
            cpu_hours = 0.0
            for j in jids:
                s = stats.get(j)
                if s and s["elapsed_s"] > 0:
                    try:
                        cpu_hours += (s["elapsed_s"] * int(s["req_cpus"])) / 3600
                    except ValueError:
                        pass
            if not elapsed:
                f.write(f"| {phase} | {len(jids)} | — | — | — | — |\n")
                continue
            elapsed.sort()
            mn = elapsed[0]; mx = elapsed[-1]; md = elapsed[len(elapsed) // 2]
            f.write(f"| {phase} | {len(jids)} | {_format_elapsed(mn)} | "
                    f"{_format_elapsed(md)} | {_format_elapsed(mx)} | "
                    f"{cpu_hours:.2f} |\n")
        f.write("\n")

        f.write("## Per-job detail\n\n")
        f.write("| Phase | Job ID | State | Elapsed | CPUs |\n")
        f.write("|---|---|---|---:|---:|\n")
        for phase in sorted(phase_buckets):
            for jid in sorted(phase_buckets[phase]):
                s = stats.get(jid)
                if s:
                    f.write(f"| {phase} | {jid} | {s['state']} | "
                            f"{_format_elapsed(s['elapsed_s'])} | "
                            f"{s['req_cpus']} |\n")
                else:
                    f.write(f"| {phase} | {jid} | N/A | — | — |\n")
        f.write("\n")

        f.write("## How to extend\n\n")
        f.write("- `synth_align` jobs aren't in this report because their "
                "only casetrack trace is a later `append`. To time them, "
                "`sacct -j $(grep -h 'Submitted batch job' "
                "$SANDBOX/logs/*.out | awk '{print $4}' | paste -sd,)` "
                "against the SLURM job IDs in the synth phase's submit log.\n")

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
