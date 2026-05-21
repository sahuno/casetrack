# tests/test_reference_artifacts_readpaths.py
import subprocess, sys, sqlite3
from pathlib import Path
import casetrack
from casetrack_qc import reference_artifacts as ra
# reuse helpers
from tests.test_reference_artifacts_cli import _init_project, _bootstrap_one_specimen


def _stale_setup(tmp_path):
    """Set up a project where S1/clair3 used hg38_v0 but current version is hg38_v1."""
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n'
        '\n[analyses.clair3]\nlevel="specimen"\ncolumn_prefix="clair3"\nuses=["genome"]\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)
    summary = pdir / "clair3_summary.tsv"
    summary.write_text("specimen_id\tn_snv\nS1\t1\n")
    subprocess.run([sys.executable, "-m", "casetrack", "append", "--project-dir",
                    str(pdir), "--analysis", "clair3", "--level", "specimen",
                    "--results", str(summary),
                    "--overwrite"], check=True, capture_output=True, text=True)
    # bump the reference version so usage row is now stale
    toml.write_text(toml.read_text().replace("hg38_v0", "hg38_v1"))
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    return pdir


def test_query_reference_usage_view_exposes_is_stale(tmp_path):
    """_reference_usage view returns entity_id + is_stale=1 after version bump."""
    pdir = _stale_setup(tmp_path)
    r = subprocess.run(
        [sys.executable, "-m", "casetrack", "query",
         "--project-dir", str(pdir), "--fmt", "tsv",
         "SELECT entity_id, ref_key, version_used, current_version, "
         "is_stale FROM _reference_usage"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"query failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
    assert "S1" in r.stdout, f"S1 not in output:\n{r.stdout}"
    # is_stale must be True (version_used=hg38_v0, current=hg38_v1)
    lines = r.stdout.strip().splitlines()
    # skip header line
    data_lines = [l for l in lines[1:] if "S1" in l]
    assert data_lines, f"No data lines with S1:\n{r.stdout}"
    # last column is is_stale — should be 1 or true
    val = data_lines[0].split("\t")[-1].strip().lower()
    assert val in ("1", "true"), f"Expected is_stale=1/true, got: {val!r}\nFull output:\n{r.stdout}"


def test_query_reference_usage_view_not_stale_when_version_matches(tmp_path):
    """_reference_usage view returns is_stale=0 when version_used equals current."""
    pdir = _init_project(tmp_path)
    toml = pdir / "casetrack.toml"
    toml.write_text(toml.read_text() +
        '\n[references.genome]\npath="/db/hg38.fa"\nversion="hg38_v0"\nkind="genome"\n'
        '\n[analyses.clair3]\nlevel="specimen"\ncolumn_prefix="clair3"\nuses=["genome"]\n')
    subprocess.run([sys.executable, "-m", "casetrack", "schema", "apply",
                    "--project-dir", str(pdir)], check=True, capture_output=True, text=True)
    _bootstrap_one_specimen(pdir)
    summary = pdir / "clair3_summary.tsv"
    summary.write_text("specimen_id\tn_snv\nS1\t1\n")
    subprocess.run([sys.executable, "-m", "casetrack", "append", "--project-dir",
                    str(pdir), "--analysis", "clair3", "--level", "specimen",
                    "--results", str(summary),
                    "--overwrite"], check=True, capture_output=True, text=True)
    # no version bump — version_used == current_version
    r = subprocess.run(
        [sys.executable, "-m", "casetrack", "query",
         "--project-dir", str(pdir), "--fmt", "tsv",
         "SELECT entity_id, ref_key, version_used, current_version, "
         "is_stale FROM _reference_usage"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"query failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
    lines = r.stdout.strip().splitlines()
    data_lines = [l for l in lines[1:] if "S1" in l]
    assert data_lines, f"No data lines with S1:\n{r.stdout}"
    val = data_lines[0].split("\t")[-1].strip().lower()
    assert val in ("0", "false"), f"Expected is_stale=0/false, got: {val!r}\nFull output:\n{r.stdout}"


def test_query_reference_usage_view_absent_on_pre0010_project(tmp_path):
    """_reference_usage view gracefully absent (query returns error, not crash) on pre-0010 DB."""
    pdir = _init_project(tmp_path)
    # drop reference tables to simulate a pre-0010 project
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    conn.execute("DROP TABLE IF EXISTS reference_usage")
    conn.execute("DROP TABLE IF EXISTS reference_artifacts")
    conn.commit(); conn.close()

    # _reference_usage view should not exist — query should fail gracefully (exit 2)
    r = subprocess.run(
        [sys.executable, "-m", "casetrack", "query",
         "--project-dir", str(pdir), "--fmt", "tsv",
         "SELECT * FROM _reference_usage"],
        capture_output=True, text=True,
    )
    # Should fail with SQL error (view doesn't exist), not a crash/traceback
    assert r.returncode != 0, "Expected non-zero exit when _reference_usage view is absent"
    assert "Traceback" not in r.stderr, f"Got unexpected traceback:\n{r.stderr}"


def test_query_cohort_artifacts_still_works_without_reference_tables(tmp_path):
    """Pre-0010 project with cohort artifacts: _cohort_artifacts query still works (ref_stale absent OK)."""
    pdir = _init_project(tmp_path)

    # Seed patients/specimens/assays directly (register --level assay requires
    # assay_type; use direct INSERT as the existing cohort-artifact tests do).
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    conn.executescript(
        "INSERT INTO patients (patient_id) VALUES ('P1');"
        "INSERT INTO specimens (specimen_id, patient_id) VALUES ('S1', 'P1');"
        "INSERT INTO assays (assay_id, specimen_id, assay_type) VALUES ('A1', 'S1', 'ONT');"
    )
    conn.commit(); conn.close()

    # Append a cohort artifact via the CLI
    subprocess.run([sys.executable, "-m", "casetrack", "append-cohort",
                    "--project-dir", str(pdir),
                    "--analysis", "joint_vc",
                    "--run-tag", "r1",
                    "--path", "/results/joint.vcf.gz",
                    "--inputs", "A1"],
                   check=True, capture_output=True, text=True)

    # Drop reference tables to simulate pre-0010
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    conn.execute("DROP TABLE IF EXISTS reference_usage")
    conn.execute("DROP TABLE IF EXISTS reference_artifacts")
    conn.commit(); conn.close()

    # _cohort_artifacts should still work (falls back to view without ref_stale)
    r = subprocess.run(
        [sys.executable, "-m", "casetrack", "query",
         "--project-dir", str(pdir), "--fmt", "tsv",
         "SELECT artifact_id, analysis, stale FROM _cohort_artifacts"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"cohort_artifacts query failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
    assert "joint_vc" in r.stdout, f"joint_vc not in output:\n{r.stdout}"


def test_status_shows_reference_section(tmp_path):
    pdir = _stale_setup(tmp_path)
    r = subprocess.run([sys.executable, "-m", "casetrack", "status",
                        "--project-dir", str(pdir)], capture_output=True, text=True)
    assert r.returncode == 0
    # Assert on the real section content, not a substring the tmp_path could leak.
    assert "References (" in r.stdout  # the section heading "References (N declared; …)"
    assert "genome" in r.stdout       # the declared ref_key is listed


def test_export_include_references(tmp_path):
    pdir = _stale_setup(tmp_path)
    out = pdir / "export.xlsx"
    r = subprocess.run([sys.executable, "-m", "casetrack", "export",
                        "--project-dir", str(pdir), "--include-references",
                        "--output", str(out)], capture_output=True, text=True)
    assert r.returncode == 0 and out.exists()


def test_validate_flags_orphan_usage(tmp_path):
    pdir = _stale_setup(tmp_path)
    # orphan: a usage row whose ref_key isn't in reference_artifacts
    conn = sqlite3.connect(pdir / casetrack.PROJECT_DB_NAME)
    conn.execute("INSERT INTO reference_usage (scope, entity_level, entity_id, "
                 "analysis, ref_key, version_used, recorded_at) VALUES "
                 "('analysis','specimen','S1','modkit','ghostref','v',datetime('now'))")
    conn.commit(); conn.close()
    r = subprocess.run([sys.executable, "-m", "casetrack", "validate",
                        "--project-dir", str(pdir)], capture_output=True, text=True)
    # Assert on the real signal — the orphan ref_key — not the path-leaked "orphan".
    assert "ghostref" in (r.stdout + r.stderr)


def test_dashboard_renders_references_section(tmp_path):
    pdir = _stale_setup(tmp_path)
    out = pdir / "dash.html"
    r = subprocess.run([sys.executable, "-m", "casetrack", "dashboard",
                        "--project-dir", str(pdir), "--output", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and out.exists()
    html = out.read_text()
    assert "References" in html and "genome" in html


def test_mcp_references_tool(tmp_path, monkeypatch):
    pdir = _stale_setup(tmp_path)
    # register the project so the MCP slug resolver finds it
    subprocess.run([sys.executable, "-m", "casetrack", "projects", "register",
                    "--project-dir", str(pdir)], capture_output=True, text=True, check=True)
    from casetrack_mcp import tools
    # find the slug — list_projects_tool returns {"projects": [...]} with
    # each entry having "project_id" and "path" keys (confirmed from tools.py)
    projs = tools.list_projects_tool()["projects"]
    slug = [p["project_id"] for p in projs if str(pdir) in p["path"]][0]
    payload = tools.references_tool(slug, stale_only=True)
    assert any(o["state"] == "STALE" for o in payload["stale_outputs"])
