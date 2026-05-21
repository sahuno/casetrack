"""Tests for the Nextflow integration example.

We don't install Nextflow in CI (and the user's HPC env may not always
have it), so instead we:

1. Assert the expected files exist and have the pieces a Nextflow DSL2
   module needs (DSL2 switch, process block, correct input/output
   shape, the `casetrack append` command, etc.).
2. Extract the shell block from `casetrack.nf` and execute it under
   bash with the Nextflow variables substituted, verifying the produced
   manifest is updated. This locks the CLI contract the module depends on.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import casetrack
from conftest import write_tsv


REPO_ROOT = Path(casetrack.__file__).resolve().parent
NF_DIR = REPO_ROOT / "examples" / "nextflow"


# ── static presence + shape ────────────────────────────────────────────────────


@pytest.mark.parametrize("fname", [
    "casetrack.nf", "example_pipeline.nf", "nextflow.config", "README.md",
])
def test_nextflow_file_present(fname: str):
    assert (NF_DIR / fname).is_file(), f"missing {fname}"


def test_module_declares_dsl2_and_process():
    src = (NF_DIR / "casetrack.nf").read_text()
    assert "nextflow.enable.dsl = 2" in src
    assert re.search(r"process\s+casetrack_append\s*\{", src)
    assert re.search(r"process\s+casetrack_add_metadata\s*\{", src)


def test_module_process_inputs_outputs():
    """`casetrack_append` must accept (analysis, tsv) and re-emit it
    so downstream processes can chain off confirmed-logged results."""
    src = (NF_DIR / "casetrack.nf").read_text()
    # Isolate the casetrack_append process block.
    m = re.search(
        r"process\s+casetrack_append\s*\{(?P<body>.*?)^\}",
        src, re.S | re.M,
    )
    assert m, "could not isolate casetrack_append process body"
    body = m.group("body")
    assert re.search(r"input:\s*\n\s*tuple\s+val\(analysis\),\s*path\(results_tsv\)", body)
    assert re.search(r"output:\s*\n\s*tuple\s+val\(analysis\),\s*path\(results_tsv\)", body)
    # Retries + serialized appends are load-bearing for NFS-backed shared dirs.
    assert "maxForks 1" in body
    assert "errorStrategy 'retry'" in body


def test_module_references_casetrack_append_command():
    src = (NF_DIR / "casetrack.nf").read_text()
    assert re.search(r"\$\{params\.casetrack_bin\}\s+append", src)
    assert "--manifest" in src and "--results" in src and "--analysis" in src


def test_module_pairs_allow_new_with_yes():
    """The Nextflow module must pass --allow-new together with --yes so a
    config-level opt-in serves as the confirmation. Passing --allow-new
    alone from Nextflow would now be rejected by casetrack with exit 2."""
    src = (NF_DIR / "casetrack.nf").read_text()
    # Every occurrence of '--allow-new' in the module must also carry '--yes'.
    allow_lines = [ln for ln in src.splitlines() if "--allow-new" in ln]
    assert allow_lines, "expected at least one --allow-new occurrence in module"
    for ln in allow_lines:
        assert "--yes" in ln, (
            f"--allow-new without --yes in module line: {ln!r}"
        )


def test_example_pipeline_includes_module():
    src = (NF_DIR / "example_pipeline.nf").read_text()
    assert "include { casetrack_append }" in src
    assert "nextflow.enable.dsl = 2" in src


def test_nextflow_config_has_apptainer_and_slurm_profiles():
    src = (NF_DIR / "nextflow.config").read_text()
    # The user's CLAUDE.md standardizes on SLURM + Apptainer on the MSKCC cluster.
    assert re.search(r"slurm\s*\{", src)
    assert re.search(r"apptainer\s*\{", src)
    assert "apptainer.enabled" in src
    assert "apptainer.autoMounts" in src


# ── dynamic: execute the module's shell block under bash ───────────────────────


def _extract_casetrack_append_script(src: str) -> str:
    '''Return the literal text inside the triple-quoted `script:` block of
    the casetrack_append process. Preserves the shell command exactly.'''
    # Scope to the casetrack_append process, then grab the first triple-
    # quoted block that follows the `script:` label. The prelude between
    # them can contain Groovy `def` lines, `//` comments, or be empty —
    # we just want the shell body.
    m = re.search(
        r"process\s+casetrack_append\s*\{.*?script:\s*.*?\"{3}(?P<body>.*?)\"{3}",
        src, re.S,
    )
    assert m, "could not find script block in casetrack_append"
    return m.group("body")


def _render_script(script: str, substitutions: dict) -> str:
    """Substitute Nextflow-style `${name}` placeholders and collapse the
    Groovy-level `\\\\` escapes that Nextflow would normally unescape before
    handing the script to bash."""
    out = script
    for k, v in substitutions.items():
        out = out.replace("${" + k + "}", v)
    # Groovy triple-quoted strings pass `\\` → `\` through to the shell.
    out = out.replace("\\\\", "\\")
    return out


def test_nextflow_module_command_runs_end_to_end(
    tmp_project: Path, samples_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """Execute the exact shell block the Nextflow module would run and
    verify the manifest is updated correctly."""
    manifest = tmp_project / "manifest.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples_file),
        key="sample_id", metadata=None, cols=None, force=False,
    ))

    results = tmp_project / "SAMPLE_01_modkit.tsv"
    write_tsv(
        results,
        pd.DataFrame({"sample_id": ["SAMPLE_01"], "modkit_mean_meth": [0.72]}),
    )

    src = (NF_DIR / "casetrack.nf").read_text()
    raw_script = _extract_casetrack_append_script(src)

    # Pick an invocation that matches what Nextflow would inject into the
    # shell: the CLI binary, the manifest param, the key, and the runtime
    # values (analysis, results_tsv, allow_flag).
    casetrack_bin = f"{sys.executable} {Path(casetrack.__file__)}"
    rendered = _render_script(raw_script, {
        "params.casetrack_bin":      casetrack_bin,
        "params.casetrack_manifest": str(manifest),
        "params.casetrack_key":      "sample_id",
        "params.casetrack_extra":    "",
        "analysis":                  "modkit_methylation",
        "results_tsv":               str(results),
        "allow_flag":                "",
    })

    # Anything still un-substituted would be a test-env gap — fail loudly.
    remaining = re.findall(r"\$\{[^}]+\}", rendered)
    assert not remaining, f"unsubstituted placeholders: {remaining}"

    subprocess.run(
        ["bash", "-c", rendered],
        check=True, capture_output=True, text=True,
    )

    # Manifest must now have the modkit column for SAMPLE_01.
    df = pd.read_csv(manifest, sep="\t").set_index("sample_id")
    assert "modkit_mean_meth" in df.columns
    assert df.loc["SAMPLE_01", "modkit_mean_meth"] == 0.72
    assert pd.notna(df.loc["SAMPLE_01", "modkit_methylation_done"])


# ── cohort-artifact process (proposal 0009) ────────────────────────────────


def test_module_declares_cohort_process():
    src = (NF_DIR / "casetrack.nf").read_text()
    assert re.search(r"process\s+casetrack_append_cohort\s*\{", src)


def test_cohort_process_inputs_outputs():
    """`casetrack_append_cohort` takes the (analysis, run_tag, artifact,
    inputs_tsv, stats_json) fan-in tuple and re-emits (analysis, run_tag)."""
    src = (NF_DIR / "casetrack.nf").read_text()
    m = re.search(
        r"process\s+casetrack_append_cohort\s*\{(?P<body>.*?)^\}",
        src, re.S | re.M,
    )
    assert m, "could not isolate casetrack_append_cohort process body"
    body = m.group("body")
    assert re.search(
        r"tuple\s+val\(analysis\),\s*val\(run_tag\),\s*path\(artifact\),"
        r"\s*path\(inputs_tsv\),\s*path\(stats_json\)",
        body,
    )
    assert re.search(r"output:\s*\n\s*tuple\s+val\(analysis\),\s*val\(run_tag\)", body)
    assert "maxForks 1" in body
    assert "errorStrategy 'retry'" in body
    assert "append-cohort" in body
    assert "--inputs-from" in body


def _extract_script(src: str, process_name: str) -> str:
    m = re.search(
        rf"process\s+{process_name}\s*\{{.*?script:\s*.*?\"{{3}}(?P<body>.*?)\"{{3}}",
        src, re.S,
    )
    assert m, f"could not find script block in {process_name}"
    return m.group("body")


def test_nextflow_cohort_command_runs_end_to_end(tmp_path: Path):
    """Execute the exact shell block casetrack_append_cohort would run and
    verify the cohort artifact + its assay lineage land in the project."""
    from casetrack_qc import cohort_artifacts as ca

    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="nf_cohort", force=False,
    ))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES ('P1'), ('P2');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "  VALUES ('P1-t', 'P1', 'tumor'), ('P2-t', 'P2', 'tumor');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('P1-t-ONT', 'P1-t', 'ONT'), ('P2-t-ONT', 'P2-t', 'ONT');"
            )
    finally:
        conn.close()

    artifact = tmp_path / "cohort.vcf.gz"
    artifact.write_text("MOCK")
    inputs_tsv = tmp_path / "inputs.txt"
    inputs_tsv.write_text("P1-t-ONT\nP2-t-ONT\n")
    stats_json = tmp_path / "stats.json"
    stats_json.write_text('{"n_variants": 42}')

    src = (NF_DIR / "casetrack.nf").read_text()
    raw_script = _extract_script(src, "casetrack_append_cohort")
    casetrack_bin = f"{sys.executable} {Path(casetrack.__file__)}"
    rendered = _render_script(raw_script, {
        "params.casetrack_bin":         casetrack_bin,
        "params.casetrack_project_dir": str(proj),
        "params.casetrack_extra":       "",
        "analysis":                     "joint_genotype",
        "run_tag":                      "20260520_demo",
        "artifact":                     str(artifact),
        "inputs_tsv":                   str(inputs_tsv),
        # Nextflow evaluates `def stats_arg = stats_json ? "--stats ..." : ''`;
        # the static renderer can't run Groovy, so we substitute the resolved
        # value the way Nextflow would when a stats file is present.
        "stats_arg":                    f"--stats '{stats_json}'",
    })
    remaining = re.findall(r"\$\{[^}]+\}", rendered)
    assert not remaining, f"unsubstituted placeholders: {remaining}"

    subprocess.run(["bash", "-c", rendered], check=True,
                   capture_output=True, text=True)

    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "joint_genotype", "20260520_demo")
        assert art is not None
        assert art.n_inputs == 2
        assert ca.artifact_inputs(conn, art.artifact_id) == ["P1-t-ONT", "P2-t-ONT"]
    finally:
        conn.close()


def test_nextflow_cohort_command_stats_optional(tmp_path: Path):
    """The process must register an artifact even with NO stats file —
    `stats_arg` resolves to empty and no `--stats` flag is emitted."""
    from casetrack_qc import cohort_artifacts as ca

    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="hgsoc",
        project_name="nf_cohort_nostats", force=False,
    ))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        with casetrack.begin_immediate(conn):
            conn.executescript(
                "INSERT INTO patients (patient_id) VALUES ('P1');"
                "INSERT INTO specimens (specimen_id, patient_id, tissue_site) "
                "  VALUES ('P1-t', 'P1', 'tumor');"
                "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES "
                "  ('P1-t-ONT', 'P1-t', 'ONT');"
            )
    finally:
        conn.close()

    artifact = tmp_path / "cohort.vcf.gz"
    artifact.write_text("MOCK")
    inputs_tsv = tmp_path / "inputs.txt"
    inputs_tsv.write_text("P1-t-ONT\n")

    src = (NF_DIR / "casetrack.nf").read_text()
    raw_script = _extract_script(src, "casetrack_append_cohort")
    casetrack_bin = f"{sys.executable} {Path(casetrack.__file__)}"
    rendered = _render_script(raw_script, {
        "params.casetrack_bin":         casetrack_bin,
        "params.casetrack_project_dir": str(proj),
        "params.casetrack_extra":       "",
        "analysis":                     "joint_genotype",
        "run_tag":                      "nostats_run",
        "artifact":                     str(artifact),
        "inputs_tsv":                   str(inputs_tsv),
        "stats_arg":                    "",   # no stats → no --stats flag
    })
    remaining = re.findall(r"\$\{[^}]+\}", rendered)
    assert not remaining, f"unsubstituted placeholders: {remaining}"
    assert "--stats" not in rendered

    subprocess.run(["bash", "-c", rendered], check=True,
                   capture_output=True, text=True)

    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        art = ca.get_artifact_by_key(conn, "joint_genotype", "nostats_run")
        assert art is not None
        assert art.stats_json is None
    finally:
        conn.close()


# ── packaged subworkflow ────────────────────────────────────────────────────

SUBWF = NF_DIR / "subworkflows" / "local" / "cohort_artifact_tracked.nf"


def test_cohort_subworkflow_file_present():
    assert SUBWF.is_file(), "missing subworkflows/local/cohort_artifact_tracked.nf"


def test_cohort_subworkflow_shape():
    src = SUBWF.read_text()
    assert "nextflow.enable.dsl = 2" in src
    assert re.search(r"workflow\s+COHORT_ARTIFACT_TRACKED\s*\{", src)
    # Includes + uses the registration process.
    assert "casetrack_append_cohort" in src
    assert "include {" in src
    # Gathers the lineage via collectFile and declares take:/emit:.
    assert "collectFile" in src
    assert re.search(r"\btake:", src)
    assert re.search(r"\bemit:", src)


def test_nextflow_module_command_with_allow_new(
    tmp_project: Path, samples_file: Path
):
    """Same shell-block contract, but with `--allow-new` on, mirroring
    what Nextflow would substitute when params.casetrack_allow_new is set."""
    manifest = tmp_project / "manifest.tsv"
    casetrack.cmd_init(argparse.Namespace(
        manifest=str(manifest), samples=str(samples_file),
        key="sample_id", metadata=None, cols=None, force=False,
    ))

    results = tmp_project / "NEW_99_modkit.tsv"
    write_tsv(
        results,
        pd.DataFrame({"sample_id": ["NEW_99"], "modkit_mean_meth": [0.4]}),
    )

    src = (NF_DIR / "casetrack.nf").read_text()
    raw_script = _extract_casetrack_append_script(src)

    casetrack_bin = f"{sys.executable} {Path(casetrack.__file__)}"
    rendered = _render_script(raw_script, {
        "params.casetrack_bin":      casetrack_bin,
        "params.casetrack_manifest": str(manifest),
        "params.casetrack_key":      "sample_id",
        "params.casetrack_extra":    "",
        "analysis":                  "modkit_methylation",
        "results_tsv":               str(results),
        # Module emits both flags together when params.casetrack_allow_new
        # is true — the Nextflow config file is itself the --yes confirmation.
        "allow_flag":                "--allow-new --yes",
    })
    subprocess.run(
        ["bash", "-c", rendered],
        check=True, capture_output=True, text=True,
    )
    df = pd.read_csv(manifest, sep="\t")
    assert "NEW_99" in df["sample_id"].tolist()
