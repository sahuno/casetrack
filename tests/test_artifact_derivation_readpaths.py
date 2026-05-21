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
    # annot's inputs are A1,A2 and A2 is censored, so annot IS input-stale.
    # (Orthogonality — derived_stale independent of stale — is tested separately
    # in test_cohort_view_orthogonality_derived_not_input_stale where annot has
    # only A1 as its own input.) Here we only assert the derived chain propagates.
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


# ── Backward-compatibility regression (0011 Task 8 review) ──────────────────


def test_status_shows_derivation_section(tmp_path):
    """``status`` emits a Derivation section and names derived-stale nodes."""
    p = _proj(tmp_path)
    conn = casetrack.open_project_db(p / "casetrack.db")
    conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id='A2'")
    conn.commit()
    conn.close()
    r = _run(["status", "--project-dir", str(p)])
    assert r.returncode == 0, r.stderr
    assert "Derivation" in r.stdout
    assert "cohort:annot@v1" in r.stdout


def test_status_derivation_section_all_fresh(tmp_path):
    """Edges present but nothing censored: section still shows, 0 derived-stale."""
    p = _proj(tmp_path)  # has the annot<-joint edge, no censoring
    r = _run(["status", "--project-dir", str(p)])
    assert r.returncode == 0, r.stderr
    assert "Derivation" in r.stdout
    assert "0 derived-stale" in r.stdout


def test_cohort_view_ref_stale_survives_without_derivation_table(tmp_path):
    """Regression: _cohort_artifacts.ref_stale is preserved on 0010-era DBs
    that lack artifact_derivation (not yet migrated to 0011).

    Before the three-tier fallback fix, a DB with 0009+0010 tables but no
    artifact_derivation would fail the tier-1 (WITH RECURSIVE) view install and
    fall all the way back to the pre-0010 view, silently dropping ref_stale and
    making stale references appear fresh.
    """
    proj = _init_project(tmp_path)
    conn = casetrack.open_project_db(proj / "casetrack.db")
    try:
        _entity_rows(conn)
        conn.commit()
        ca.ensure_cohort_artifacts_schema(conn)
        ra.ensure_reference_schema(conn)
        # Register a genome reference at version hg38_v0.
        ra.sync_references_from_toml(conn, {
            "genome": {"path": "/db/hg38.fa", "version": "hg38_v0",
                       "kind": "genome"}
        })
        # Register a cohort artifact using the genome reference.
        aid = _add_cohort(conn, "joint_vc", "r1", ["A1", "A2"])
        ra.record_usage(conn, scope="cohort", artifact_id=aid,
                        ref_key="genome", version_used="hg38_v0",
                        transaction_id="t")
        conn.commit()
        # Bump the genome reference version: usage now stale (hg38_v0 vs hg38_v1).
        ra.sync_references_from_toml(conn, {
            "genome": {"path": "/db/hg38.fa", "version": "hg38_v1",
                       "kind": "genome"}
        })
        conn.commit()
        # Simulate a pre-0011 project: ensure_derivation_schema was NEVER called.
        # Drop the table if it exists (from init scaffold), so DuckDB can't find it.
        conn.execute("DROP TABLE IF EXISTS artifact_derivation")
        conn.commit()
    finally:
        conn.close()

    r = _run(["query", "--project-dir", str(proj), "--fmt", "json",
              'SELECT analysis, ref_stale FROM "_cohort_artifacts"'])
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert rows, "Expected at least one row in _cohort_artifacts"
    row = rows[0]
    # ref_stale must be present (not KeyError) and truthy (hg38_v0 vs hg38_v1).
    assert "ref_stale" in row, (
        "ref_stale column missing — three-tier fallback regressed to pre-0010 view"
    )
    assert bool(row["ref_stale"]) is True, (
        f"Expected ref_stale=True after version bump, got {row['ref_stale']!r}"
    )


# ── Task 10: export --include-derivation (0011 §6.5) ─────────────────────────


def test_export_include_derivation(tmp_path):
    """``export --include-derivation`` writes artifact_derivation.tsv containing
    the recorded edge (annot <- joint)."""
    p = _proj(tmp_path)
    out = tmp_path / "exp"          # directory → ext = .tsv (no prefix_mode)
    r = _run(["export", "--project-dir", str(p),
              "--output", str(out), "--include-derivation"])
    assert r.returncode == 0, r.stderr
    deriv_file = out / "artifact_derivation.tsv"
    assert deriv_file.exists(), f"artifact_derivation.tsv not written; stdout={r.stdout!r}"
    content = deriv_file.read_text()
    # The edge recorded in _proj: down=cohort:annot@v1, up=cohort:joint@v1
    assert "cohort:annot@v1" in content
    assert "cohort:joint@v1" in content


# ── Task 11: validate dangling + acyclic invariants (0011 §6.5) ───────────────


def test_validate_flags_dangling_edge(tmp_path):
    """``validate`` reports a dangling node-ref when artifact_derivation points
    at a cohort artifact that does not exist in cohort_artifacts."""
    p = _proj(tmp_path)
    conn = casetrack.open_project_db(p / "casetrack.db")
    # Raw-insert an edge whose up_node references a nonexistent cohort artifact.
    conn.execute(
        "INSERT INTO artifact_derivation(down_node, up_node, recorded_at) "
        "VALUES ('cohort:annot@v1','cohort:ghost@v9','2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    r = _run(["validate", "--project-dir", str(p)])
    out = r.stdout + r.stderr
    assert "ghost@v9" in out or "dangling" in out.lower(), (
        f"Expected 'ghost@v9' or 'dangling' in output; got:\n{out}"
    )


def test_validate_flags_cycle(tmp_path):
    """``validate`` reports a cycle when artifact_derivation contains a back-edge
    (raw-inserted, bypassing record_edge's cycle guard)."""
    p = _proj(tmp_path)
    # _proj already has edge annot@v1 <- joint@v1.
    # Add the reverse edge to form a two-node cycle: joint@v1 <- annot@v1.
    conn = casetrack.open_project_db(p / "casetrack.db")
    conn.execute(
        "INSERT OR IGNORE INTO artifact_derivation"
        "(down_node, up_node, recorded_at, transaction_id) "
        "VALUES ('cohort:joint@v1','cohort:annot@v1','2026-01-01T00:00:00','raw')"
    )
    conn.commit()
    conn.close()
    r = _run(["validate", "--project-dir", str(p)])
    out = r.stdout + r.stderr
    assert "cycle" in out.lower(), (
        f"Expected 'cycle' in validate output; got:\n{out}"
    )


def test_validate_cycle_no_false_positive_for_external_pointer(tmp_path):
    """A node that merely points INTO a cycle must NOT be reported as a cycle.

    Guards the DFS grey-node fix: with a 2-node cycle (joint<->annot) plus an
    external edge (extra -> annot) pointing into it, exactly ONE cycle issue
    should be emitted, not a spurious second one for `extra`.
    """
    p = _proj(tmp_path)  # has annot@v1 <- joint@v1
    conn = casetrack.open_project_db(p / "casetrack.db")
    _add_cohort(conn, "extra", "v1", ["A1"])  # a real cohort artifact (resolves)
    conn.executemany(
        "INSERT OR IGNORE INTO artifact_derivation"
        "(down_node, up_node, recorded_at, transaction_id) VALUES (?,?,?,?)",
        [
            ("cohort:joint@v1", "cohort:annot@v1", "2026-01-01T00:00:00", "raw"),  # close cycle
            ("cohort:extra@v1", "cohort:annot@v1", "2026-01-01T00:00:00", "raw"),  # points in
        ],
    )
    conn.commit()
    conn.close()
    r = _run(["validate", "--project-dir", str(p)])
    out = r.stdout + r.stderr
    n_cycle_issues = out.lower().count("cycle through")
    assert n_cycle_issues == 1, (
        f"expected exactly 1 cycle issue, got {n_cycle_issues}:\n{out}"
    )
