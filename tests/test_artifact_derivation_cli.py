"""CLI tests for the 0011 derivation commands.

Tests: derived-from, derivation, migrate-derivation — via subprocess so that
the argparse dispatch layer is exercised end-to-end.  Task 5 (cli.py wiring)
must be present for these to pass.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
import argparse
import subprocess
import sys

import casetrack
from casetrack_qc import cohort_artifacts as ca
from casetrack_qc import artifact_derivation as ad


def _run(args):
    return subprocess.run(
        [sys.executable, "-m", "casetrack", *args],
        capture_output=True,
        text=True,
    )


def _init_project(tmp_path) -> "Path":
    """Scaffold a minimal casetrack project via cmd_init (creates toml + provenance.jsonl)."""
    proj = tmp_path / "proj"
    ns = argparse.Namespace(
        manifest=None,
        project_dir=str(proj),
        samples=None,
        key="sample_id",
        metadata=None,
        cols=None,
        from_template="hgsoc",
        project_name="test",
        force=False,
    )
    casetrack.cmd_init(ns)
    return proj


def _project_with_artifacts(tmp_path):
    """A properly-initialized project with two cohort artifacts (joint@v1, annot@v1)
    and the derivation schema in place.
    """
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        # Insert three-level rows and commit so FK-checking add_artifact_inputs can find them.
        # hgsoc template requires tissue_site on specimens and assay_type on assays.
        conn.execute("INSERT OR IGNORE INTO patients(patient_id) VALUES ('P1')")
        conn.execute(
            "INSERT OR IGNORE INTO specimens(specimen_id, patient_id, tissue_site)"
            " VALUES ('S1','P1','tumor')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO assays(assay_id, specimen_id, assay_type)"
            " VALUES ('A1','S1','ONT')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO assays(assay_id, specimen_id, assay_type)"
            " VALUES ('A2','S1','ONT')"
        )
        conn.commit()
        ca.ensure_cohort_artifacts_schema(conn)
        ad.ensure_derivation_schema(conn)
        for analysis, run_tag in (("joint", "v1"), ("annot", "v1")):
            aid = ca.insert_artifact(
                conn,
                analysis=analysis,
                run_tag=run_tag,
                path=f"/x/{run_tag}",
                n_inputs=2,
                transaction_id="t",
                checksum=None,
                stats_json=None,
                created_by="test",
            )
            ca.add_artifact_inputs(conn, aid, ["A1", "A2"])
        conn.commit()
    finally:
        conn.close()
    return proj


def test_derived_from_records_edge(tmp_path):
    """``derived-from`` records the edge in artifact_derivation."""
    p = _project_with_artifacts(tmp_path)
    r = _run(
        [
            "derived-from",
            "--project-dir", str(p),
            "--downstream", "cohort:annot@v1",
            "--upstream", "cohort:joint@v1",
        ]
    )
    assert r.returncode == 0, r.stderr
    conn = casetrack.open_project_db(p / "casetrack.db")
    try:
        assert len(ad.list_edges(conn)) == 1
    finally:
        conn.close()


def test_derived_from_refuses_cycle(tmp_path):
    """``derived-from`` exits non-zero and prints 'cycle' when an edge would close a loop."""
    p = _project_with_artifacts(tmp_path)
    # establish annot <- joint
    _run(
        [
            "derived-from",
            "--project-dir", str(p),
            "--downstream", "cohort:annot@v1",
            "--upstream", "cohort:joint@v1",
        ]
    )
    # attempt joint <- annot — would close a cycle
    r = _run(
        [
            "derived-from",
            "--project-dir", str(p),
            "--downstream", "cohort:joint@v1",
            "--upstream", "cohort:annot@v1",
        ]
    )
    assert r.returncode != 0
    assert "cycle" in (r.stderr + r.stdout).lower()


def test_derivation_lists_and_stale(tmp_path):
    """``derivation --stale-only`` surfaces a node that is derived-stale."""
    p = _project_with_artifacts(tmp_path)
    # link annot <- joint
    _run(
        [
            "derived-from",
            "--project-dir", str(p),
            "--downstream", "cohort:annot@v1",
            "--upstream", "cohort:joint@v1",
        ]
    )
    # censor A2 — makes joint input-stale, which propagates as derived-stale to annot
    conn = casetrack.open_project_db(p / "casetrack.db")
    conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id='A2'")
    conn.commit()
    conn.close()
    r = _run(
        ["derivation", "--project-dir", str(p), "--fmt", "json", "--stale-only"]
    )
    assert r.returncode == 0, r.stderr
    assert "cohort:annot@v1" in r.stdout


def test_migrate_derivation_dry_run(tmp_path):
    """``migrate-derivation --dry-run`` prints 'dry-run' and does NOT create the table."""
    # Use a fully-initialized project but drop the derivation table
    # so migrate-derivation has something to do
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    conn.execute("DROP TABLE IF EXISTS artifact_derivation")
    conn.commit()
    conn.close()
    r = _run(["migrate-derivation", "--project-dir", str(proj), "--dry-run"])
    assert r.returncode == 0
    assert "dry-run" in r.stdout.lower()
    conn = casetrack.open_project_db(proj / "casetrack.db")
    assert not ad.derivation_schema_exists(conn)


# ── Task 6: init hook + TOML derived_from materialization ───────────────────


def test_init_creates_derivation_table(tmp_path):
    """``casetrack init`` creates the artifact_derivation table on fresh projects."""
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        assert ad.derivation_schema_exists(conn)
    finally:
        conn.close()


def test_toml_derived_from_materialized_on_schema_apply(tmp_path):
    """[references.pon].derived_from is materialized into artifact_derivation on ``schema apply``."""
    from casetrack_qc import cohort_artifacts as _ca

    proj = _init_project(tmp_path)

    # Register the upstream cohort artifact the reference derives from.
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        _ca.ensure_cohort_artifacts_schema(conn)
        _ca.insert_artifact(
            conn,
            analysis="make_pon",
            run_tag="v1",
            path="/x/pon",
            n_inputs=0,
            transaction_id="t",
            checksum=None,
            stats_json=None,
            created_by="t",
        )
        conn.commit()
    finally:
        conn.close()

    # Append a [references.pon] block with derived_from to the TOML.
    toml_path = proj / "casetrack.toml"
    toml_path.write_text(
        toml_path.read_text()
        + (
            '\n[references.pon]\n'
            'path = "/x/pon.vcf"\n'
            'version = "pon_v1"\n'
            'kind = "known_variants"\n'
            'derived_from = ["cohort:make_pon@v1"]\n'
        )
    )

    # Run schema apply and assert the edge was recorded.
    r = _run(["schema", "apply", "--project-dir", str(proj)])
    assert r.returncode == 0, r.stderr

    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        edges = ad.list_edges(conn)
        assert any(
            e["down_node"] == "reference:pon" and e["up_node"] == "cohort:make_pon@v1"
            for e in edges
        ), f"expected edge not found; edges={edges}"
    finally:
        conn.close()


def test_toml_derived_from_malformed_ref_rejected(tmp_path):
    """A malformed derived_from node-ref fails loudly at schema-load validation."""
    proj = _init_project(tmp_path)
    toml_path = proj / "casetrack.toml"
    toml_path.write_text(
        toml_path.read_text()
        + (
            '\n[references.pon]\n'
            'path = "/x/pon.vcf"\n'
            'version = "pon_v1"\n'
            'kind = "known_variants"\n'
            'derived_from = ["not-a-valid-node-ref"]\n'
        )
    )
    r = _run(["schema", "apply", "--project-dir", str(proj)])
    assert r.returncode != 0
    assert "derived_from" in (r.stderr + r.stdout)
