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
