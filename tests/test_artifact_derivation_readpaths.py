"""DuckDB view + read-path tests for proposal 0011 (artifact-to-artifact lineage).

Exercises the ``_artifact_derivation`` view and the ``derived_stale`` column
added to ``_cohort_artifacts`` via ``casetrack query``. Also asserts the SQL
recursive closure agrees with the authoritative Python walk
(``artifact_derivation.derived_staleness``) so the two cannot drift.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
import argparse
import json
import subprocess
import sys

import casetrack
from casetrack_qc import cohort_artifacts as ca
from casetrack_qc import artifact_derivation as ad
from casetrack_qc import reference_artifacts as ra


def _run(args):
    return subprocess.run(
        [sys.executable, "-m", "casetrack", *args],
        capture_output=True,
        text=True,
    )


def _init_project(tmp_path):
    """Scaffold a minimal casetrack project via cmd_init (hgsoc template)."""
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


def _entity_rows(conn):
    """Insert P1/S1 + A1,A2 honouring the hgsoc template's NOT NULL columns."""
    conn.execute("INSERT OR IGNORE INTO patients(patient_id) VALUES ('P1')")
    conn.execute(
        "INSERT OR IGNORE INTO specimens(specimen_id, patient_id, tissue_site)"
        " VALUES ('S1','P1','tumor')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO assays(assay_id, specimen_id, assay_type)"
        " VALUES ('A1','S1','ONT'),('A2','S1','ONT')"
    )


def _add_cohort(conn, analysis, run_tag, inputs):
    aid = ca.insert_artifact(
        conn, analysis=analysis, run_tag=run_tag, path=f"/x/{run_tag}",
        n_inputs=len(inputs), transaction_id="t", checksum=None,
        stats_json=None, created_by="test",
    )
    ca.add_artifact_inputs(conn, aid, inputs)
    return aid


def _proj(tmp_path):
    """Project with joint@v1 + annot@v1 cohort artifacts and edge annot<-joint."""
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        _entity_rows(conn)
        conn.commit()
        ca.ensure_cohort_artifacts_schema(conn)
        ad.ensure_derivation_schema(conn)
        for analysis, run_tag in (("joint", "v1"), ("annot", "v1")):
            _add_cohort(conn, analysis, run_tag, ["A1", "A2"])
        ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1",
                       transaction_id="t")
        conn.commit()
    finally:
        conn.close()
    return proj


def test_query_artifact_derivation_view(tmp_path):
    """``_artifact_derivation`` exposes the edge rows via query."""
    p = _proj(tmp_path)
    r = _run(["query", "--project-dir", str(p), "--fmt", "json",
              'SELECT * FROM "_artifact_derivation"'])
    assert r.returncode == 0, r.stderr
    assert "cohort:annot@v1" in r.stdout
    assert "cohort:joint@v1" in r.stdout


def test_cohort_artifacts_view_has_derived_stale(tmp_path):
    """Censor an input to joint: annot is derived_stale but NOT input-stale (orthogonality)."""
    p = _proj(tmp_path)
    conn = casetrack.open_project_db(p / "casetrack.db")
    conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id='A2'")
    conn.commit()
    conn.close()
    r = _run(["query", "--project-dir", str(p), "--fmt", "json",
              'SELECT analysis, run_tag, stale, ref_stale, derived_stale '
              'FROM "_cohort_artifacts" ORDER BY analysis'])
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    annot = next(x for x in rows if x["analysis"] == "annot")
    joint = next(x for x in rows if x["analysis"] == "joint")
    # annot: input-fresh (its own A1 still active is irrelevant — annot's inputs
    # ARE A1,A2 and A2 is censored, so annot IS input-stale here). We instead
    # use a dedicated orthogonality case below; here just assert the chain.
    assert annot["derived_stale"] in (True, 1)
    # joint itself is input-stale (A2 censored) but has no upstream edges, so
    # its derived_stale is False — direct causes are NOT rolled into derived.
    assert joint["derived_stale"] in (False, 0)


def test_cohort_view_orthogonality_derived_not_input_stale(tmp_path):
    """annot derives from joint but has its OWN fresh input (A1); censoring A2
    (only joint's input) makes annot derived_stale=TRUE while stale=FALSE."""
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        _entity_rows(conn)
        conn.commit()
        ca.ensure_cohort_artifacts_schema(conn)
        ad.ensure_derivation_schema(conn)
        _add_cohort(conn, "joint", "v1", ["A1", "A2"])
        _add_cohort(conn, "annot", "v1", ["A1"])  # annot's only input is A1
        ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1",
                       transaction_id="t")
        conn.commit()
        conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id='A2'")
        conn.commit()
    finally:
        conn.close()
    r = _run(["query", "--project-dir", str(proj), "--fmt", "json",
              'SELECT analysis, stale, ref_stale, derived_stale '
              'FROM "_cohort_artifacts" WHERE analysis=\'annot\''])
    assert r.returncode == 0, r.stderr
    annot = json.loads(r.stdout)[0]
    assert annot["stale"] in (False, 0)            # 0009 input-stale: NO
    assert annot["derived_stale"] in (True, 1)     # 0011 derived-stale: YES


def test_view_matches_python_walk_cohort_chain(tmp_path):
    """Parity guard: the SQL derived_stale equals the Python derived_staleness
    state for the cohort->cohort chain after censoring joint's input."""
    p = _proj(tmp_path)
    conn = casetrack.open_project_db(p / "casetrack.db")
    conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id='A2'")
    conn.commit()
    # Python ground truth for both nodes
    py_annot = ad.derived_staleness(conn, "cohort:annot@v1")["state"] == "STALE"
    py_joint = ad.derived_staleness(conn, "cohort:joint@v1")["state"] == "STALE"
    conn.close()
    r = _run(["query", "--project-dir", str(p), "--fmt", "json",
              'SELECT analysis, derived_stale FROM "_cohort_artifacts" ORDER BY analysis'])
    assert r.returncode == 0, r.stderr
    rows = {x["analysis"]: bool(x["derived_stale"]) for x in json.loads(r.stdout)}
    assert rows["cohort:annot@v1".split(":")[1].split("@")[0]] == py_annot
    assert rows["joint"] == py_joint
    # Concretely: annot STALE via chain, joint NOT (no upstream edge)
    assert rows["annot"] is True
    assert rows["joint"] is False


def test_view_matches_python_walk_reference_cascade(tmp_path):
    """Parity guard for the load-bearing PoN case: a cohort VCF that USES a
    reference whose own upstream input got censored (NO version bump). The
    SQL closure must follow the reference_usage edge just like the Python walk."""
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        _entity_rows(conn)
        conn.commit()
        ca.ensure_cohort_artifacts_schema(conn)
        ra.ensure_reference_schema(conn)
        ad.ensure_derivation_schema(conn)
        # PoN built from A1,A2 as a cohort artifact
        _add_cohort(conn, "make_pon", "cohort147_v1", ["A1", "A2"])
        # declare the pon reference + the derived-from edge reference<-cohort
        ra.sync_references_from_toml(conn, {
            "pon": {"path": "/x/pon.vcf", "version": "pon_v1",
                    "kind": "known_variants"}})
        ad.record_edge(conn, down="reference:pon",
                       up="cohort:make_pon@cohort147_v1", transaction_id="t")
        # downstream cohort VCF that USES the pon reference (reference_usage)
        vcf_id = _add_cohort(conn, "call", "v1", ["A1"])
        ra.record_usage(conn, scope="cohort", artifact_id=vcf_id, ref_key="pon",
                        version_used="pon_v1", transaction_id="t")
        conn.commit()
        # censor a PoN input — NO version bump
        conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id='A2'")
        conn.commit()
        py_call = ad.derived_staleness(conn, "cohort:call@v1")["state"] == "STALE"
    finally:
        conn.close()
    assert py_call is True  # sanity: Python says STALE
    r = _run(["query", "--project-dir", str(proj), "--fmt", "json",
              'SELECT analysis, stale, derived_stale FROM "_cohort_artifacts" '
              "WHERE analysis='call'"])
    assert r.returncode == 0, r.stderr
    call = json.loads(r.stdout)[0]
    # call's own input A1 is fresh -> stale=False; derived via pon-cascade -> True
    assert call["stale"] in (False, 0)
    assert bool(call["derived_stale"]) == py_call
    assert call["derived_stale"] in (True, 1)


def test_artifact_derivation_view_cycle_safe(tmp_path):
    """A raw-inserted cycle must not hang or error the recursive view."""
    p = _proj(tmp_path)
    conn = casetrack.open_project_db(p / "casetrack.db")
    # Bypass record_edge's cycle guard: insert a back-edge directly so the graph
    # has a real loop joint<-annot<-joint. The WITH RECURSIVE ... UNION must
    # dedupe the working set and terminate.
    conn.execute(
        "INSERT OR IGNORE INTO artifact_derivation"
        "(down_node, up_node, recorded_at, transaction_id)"
        " VALUES ('cohort:joint@v1','cohort:annot@v1','t','t')")
    conn.commit()
    conn.close()
    r = _run(["query", "--project-dir", str(p), "--fmt", "json",
              'SELECT down_node, up_node, down_derived_stale '
              'FROM "_artifact_derivation" ORDER BY down_node'])
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert len(rows) == 2  # both edges returned, no hang/error
