"""Edge recording, resolution, cycle prevention, and the staleness walk (0011)."""
import sqlite3
import pytest

import casetrack
from casetrack_qc import artifact_derivation as ad
from casetrack_qc import cohort_artifacts as ca
from casetrack_qc import reference_artifacts as ra


def _project(tmp_path):
    """A real on-disk project DB with 0009 + 0010 + 0011 schemas + a few rows."""
    db = tmp_path / "casetrack.db"
    conn = casetrack.open_project_db(db)
    # minimal three-level rows so cohort_artifact_inputs FK + active cascade work
    conn.executescript(
        """
        CREATE TABLE patients(patient_id TEXT PRIMARY KEY, qc_status TEXT DEFAULT 'pass',
                              consent_status TEXT DEFAULT 'consented');
        CREATE TABLE specimens(specimen_id TEXT PRIMARY KEY, patient_id TEXT,
                               qc_status TEXT DEFAULT 'pass');
        CREATE TABLE assays(assay_id TEXT PRIMARY KEY, specimen_id TEXT,
                            qc_status TEXT DEFAULT 'pass');
        INSERT INTO patients(patient_id) VALUES ('P1');
        INSERT INTO specimens(specimen_id, patient_id) VALUES ('S1','P1');
        INSERT INTO assays(assay_id, specimen_id) VALUES ('A1','S1'),('A2','S1');
        """
    )
    ca.ensure_cohort_artifacts_schema(conn)
    ra.ensure_reference_schema(conn)
    ad.ensure_derivation_schema(conn)
    conn.commit()
    return conn


def test_record_edge_idempotent(tmp_path):
    conn = _project(tmp_path)
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t1")
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t2")
    rows = ad.list_edges(conn)
    assert len(rows) == 1
    assert rows[0]["down_node"] == "cohort:annot@v1"
    assert rows[0]["up_node"] == "cohort:joint@v1"
    assert rows[0]["transaction_id"] == "t1"  # first-write-wins; the second is IGNORE'd


def test_list_edges_empty(tmp_path):
    conn = _project(tmp_path)
    assert ad.list_edges(conn) == []


def test_record_edge_validates_node_refs(tmp_path):
    conn = _project(tmp_path)
    with pytest.raises(ad.DerivationError):
        ad.record_edge(conn, down="bogus:x", up="cohort:j@v1", transaction_id="t")
    # the up argument is validated symmetrically
    with pytest.raises(ad.DerivationError):
        ad.record_edge(conn, down="cohort:j@v1", up="bogus:x", transaction_id="t")


def test_cycle_refused_direct(tmp_path):
    conn = _project(tmp_path)
    with pytest.raises(ad.DerivationError):
        ad.record_edge(conn, down="cohort:a@v1", up="cohort:a@v1", transaction_id="t")


def test_cycle_refused_indirect(tmp_path):
    conn = _project(tmp_path)
    ad.record_edge(conn, down="cohort:b@v1", up="cohort:a@v1", transaction_id="t")
    ad.record_edge(conn, down="cohort:c@v1", up="cohort:b@v1", transaction_id="t")
    # c->b->a ; adding a->c would close the loop a->c->b->a
    with pytest.raises(ad.DerivationError):
        ad.record_edge(conn, down="cohort:a@v1", up="cohort:c@v1", transaction_id="t")


def test_upstream_of_node(tmp_path):
    conn = _project(tmp_path)
    ad.record_edge(conn, down="cohort:b@v1", up="cohort:a@v1", transaction_id="t")
    ad.record_edge(conn, down="cohort:b@v1", up="reference:pon", transaction_id="t")
    ups = sorted(ad.upstream_nodes(conn, "cohort:b@v1"))
    assert ups == ["cohort:a@v1", "reference:pon"]


# ── Task 3: transitive staleness walk ────────────────────────────────────────

from casetrack_qc import events as qc_events  # noqa: E402


def _add_cohort(conn, analysis, run_tag, inputs):
    aid = ca.insert_artifact(conn, analysis=analysis, run_tag=run_tag,
                             path=f"/x/{run_tag}.vcf", n_inputs=len(inputs),
                             transaction_id="t", checksum=None, stats_json=None,
                             created_by="test")
    ca.add_artifact_inputs(conn, aid, inputs)
    conn.commit()
    return aid


def _censor_assay(conn, assay_id):
    conn.execute("UPDATE assays SET qc_status='censored' WHERE assay_id=?", (assay_id,))
    conn.commit()


def test_cohort_to_cohort_chain(tmp_path):
    conn = _project(tmp_path)
    _add_cohort(conn, "joint", "v1", ["A1", "A2"])
    _add_cohort(conn, "annot", "v1", ["A1", "A2"])
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t")
    conn.commit()
    # fresh before any censor
    assert ad.derived_staleness(conn, "cohort:annot@v1")["state"] == "fresh"
    # censor an input to the ROOT (joint); annot must read derived_stale via the chain
    _censor_assay(conn, "A2")
    s = ad.derived_staleness(conn, "cohort:annot@v1")
    assert s["state"] == "STALE"
    assert any("joint@v1" in r for r in s["reasons"])


def test_pon_as_reference_cascade(tmp_path):
    """The load-bearing case: censoring a PoN input cascades to a VCF that
    `uses` the pon reference, with NO TOML version bump (0011 §6.3)."""
    conn = _project(tmp_path)
    # PoN built from A1,A2 as a cohort artifact
    _add_cohort(conn, "make_pon", "cohort147_v1", ["A1", "A2"])
    # declare the pon reference (current version) and the derived-from edge
    ra.sync_references_from_toml(conn, {"pon": {"path": "/x/pon.vcf", "version": "pon_v1", "kind": "known_variants"}})
    ad.record_edge(conn, down="reference:pon", up="cohort:make_pon@cohort147_v1", transaction_id="t")
    # a downstream cohort VCF that USES the pon reference (0010 reference_usage, cohort scope)
    vcf_id = _add_cohort(conn, "call", "v1", ["A1"])
    ra.record_usage(conn, scope="cohort", artifact_id=vcf_id, ref_key="pon",
                    version_used="pon_v1", transaction_id="t")
    conn.commit()
    # nothing censored yet
    assert ad.derived_staleness(conn, "reference:pon")["state"] == "fresh"
    assert ad.derived_staleness(conn, "cohort:call@v1")["state"] == "fresh"
    # censor a PoN input — NO version bump
    _censor_assay(conn, "A2")
    assert ad.derived_staleness(conn, "reference:pon")["state"] == "STALE"
    s = ad.derived_staleness(conn, "cohort:call@v1")
    assert s["state"] == "STALE"  # reached pon via reference_usage edge
    assert any("pon" in r for r in s["reasons"])


def test_orthogonality_derived_only(tmp_path):
    conn = _project(tmp_path)
    _add_cohort(conn, "joint", "v1", ["A1", "A2"])
    annot = _add_cohort(conn, "annot", "v1", ["A1"])  # annot has its own fresh inputs
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t")
    _censor_assay(conn, "A2")  # only joint's input
    # annot: input-fresh (its own A1 ok) but derived_stale (joint is input-stale)
    stale_map = ca.artifact_staleness(conn)
    assert stale_map.get(annot, []) == []          # 0009 input-stale: NO
    assert ad.derived_staleness(conn, "cohort:annot@v1")["state"] == "STALE"  # 0011: YES


def test_leaf_no_edges_not_stale(tmp_path):
    conn = _project(tmp_path)
    _add_cohort(conn, "joint", "v1", ["A1", "A2"])
    # no derivation edges at all -> derived_stale False, NOT 'untracked'
    assert ad.derived_staleness(conn, "cohort:joint@v1")["state"] == "fresh"


def test_all_derived_stale_listing(tmp_path):
    conn = _project(tmp_path)
    _add_cohort(conn, "joint", "v1", ["A1", "A2"])
    _add_cohort(conn, "annot", "v1", ["A1"])
    ad.record_edge(conn, down="cohort:annot@v1", up="cohort:joint@v1", transaction_id="t")
    _censor_assay(conn, "A2")
    stale = ad.all_derived_stale(conn)
    nodes = {r["node"] for r in stale if r["state"] == "STALE"}
    assert "cohort:annot@v1" in nodes
