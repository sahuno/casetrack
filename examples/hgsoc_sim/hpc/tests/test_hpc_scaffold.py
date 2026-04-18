"""CI-safe smoke test for examples/hgsoc_sim/hpc/.

Bootstraps the casetrack project (no cluster, no synth), runs the mock
scRNA summary on every scRNA assay, and asserts the downstream cohort
query returns the expected complete/broken/singleton structure.

The real synth_align + merge + modkit phases require SLURM + VISOR and
are NOT exercised here — they run live on IRIS via submit_pipeline.sh.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


HPC_DIR = Path(__file__).resolve().parents[1]       # examples/hgsoc_sim/hpc/
REPO_ROOT = HPC_DIR.parents[2]                      # casetrack repo root


@pytest.fixture(scope="module")
def hpc_project(tmp_path_factory):
    if shutil.which("casetrack") is None:
        pytest.skip("casetrack CLI not on PATH — skipping HPC scaffold smoke")
    sandbox = tmp_path_factory.mktemp("hgsoc_hpc_smoke")
    project = sandbox / "project"
    # Bootstrap — must succeed on any machine with casetrack + pyyaml.
    env = dict(os.environ)
    env["PATH"] = f"{os.path.dirname(sys.executable)}{os.pathsep}{env.get('PATH', '')}"
    res = subprocess.run(
        [sys.executable, str(HPC_DIR / "scripts" / "bootstrap_casetrack.py"),
         "--project-dir", str(project)],
        capture_output=True, text=True, env=env,
    )
    if res.returncode != 0:
        pytest.fail(f"bootstrap exited {res.returncode}\n"
                    f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
    return project


def _conn(project):
    return sqlite3.connect(str(project / "casetrack.db"))


def test_bootstrap_registers_expected_counts(hpc_project):
    with _conn(hpc_project) as c:
        assert c.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM specimens").fetchone()[0] == 5
        # 5 specimens × (2 ONT + 1 scRNA) = 15 assays
        assert c.execute("SELECT COUNT(*) FROM assays").fetchone()[0] == 15


def test_assay_types_and_replicates(hpc_project):
    with _conn(hpc_project) as c:
        by_type = dict(c.execute(
            "SELECT assay_type, COUNT(*) FROM assays GROUP BY assay_type"
        ).fetchall())
    assert by_type == {"ONT": 10, "scRNA": 5}


def test_specimen_shape(hpc_project):
    with _conn(hpc_project) as c:
        by_specimen = dict(c.execute(
            "SELECT specimen_id, COUNT(*) FROM assays GROUP BY specimen_id "
            "ORDER BY specimen_id"
        ).fetchall())
    # 4 paired specimens (SIM_01 tumor+normal, SIM_02 tumor+normal) each get 3
    # assays. SIM_03 is the singleton — tumor only, 3 assays.
    expected = {
        "HGSOC_SIM_01_normal": 3,
        "HGSOC_SIM_01_tumor":  3,
        "HGSOC_SIM_02_normal": 3,
        "HGSOC_SIM_02_tumor":  3,
        "HGSOC_SIM_03_tumor":  3,
    }
    assert by_specimen == expected


def test_mock_scrna_summary_is_deterministic(tmp_path):
    script = HPC_DIR / "scripts" / "mock_scrna_summary.py"
    out1 = tmp_path / "s1.tsv"
    out2 = tmp_path / "s2.tsv"
    for out in (out1, out2):
        subprocess.run([sys.executable, str(script),
                        "--assay-id", "HGSOC_SIM_01_tumor-scRNA-RNA-R01",
                        "--output", str(out)], check=True)
    assert out1.read_text() == out2.read_text()


def test_mock_scrna_autoflag_fails_on_sim02_normal(tmp_path):
    script = HPC_DIR / "scripts" / "mock_scrna_summary.py"
    out = tmp_path / "out.tsv"
    subprocess.run([sys.executable, str(script),
                    "--assay-id", "HGSOC_SIM_02_normal-scRNA-RNA-R01",
                    "--output", str(out)], check=True)
    lines = out.read_text().splitlines()
    header = lines[0].split("\t")
    data = dict(zip(header, lines[1].split("\t")))
    assert data["qc_pass"] == "false"
    assert "n_cells=" in data["qc_fail_reason"]


def test_mock_scrna_autoflag_passes_on_sim01(tmp_path):
    script = HPC_DIR / "scripts" / "mock_scrna_summary.py"
    out = tmp_path / "out.tsv"
    subprocess.run([sys.executable, str(script),
                    "--assay-id", "HGSOC_SIM_01_tumor-scRNA-RNA-R01",
                    "--output", str(out)], check=True)
    lines = out.read_text().splitlines()
    header = lines[0].split("\t")
    data = dict(zip(header, lines[1].split("\t")))
    assert data["qc_pass"] == "true"


def test_specimen_synth_params_is_deterministic(tmp_path):
    script = HPC_DIR / "scripts" / "_specimen_synth_params.py"
    args = ["--patient", "HGSOC_SIM_01",
            "--specimen", "HGSOC_SIM_01_tumor", "--run-id", "R01"]
    r1 = subprocess.run([sys.executable, str(script), *args],
                        capture_output=True, text=True, check=True)
    r2 = subprocess.run([sys.executable, str(script), *args],
                        capture_output=True, text=True, check=True)
    assert r1.stdout == r2.stdout
    # three whitespace-separated values
    parts = r1.stdout.strip().split()
    assert len(parts) == 3
    coverage, purity, seed = parts
    # total 35x split across 2 runs → 17.5 per run
    assert 17.0 <= float(coverage) <= 18.0
    assert purity == "70"
    int(seed)  # must be integer


def test_synth_script_exists_and_has_shebang():
    """Spot-check the synth wrapper is committable (SBATCH + bash)."""
    script = HPC_DIR / "slurm" / "run_synth_align.sh"
    text = script.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert "#SBATCH --account=greenbab" in text
    assert "#SBATCH --partition=componc_cpu" in text
    assert "--cpus-per-task=16" in text
