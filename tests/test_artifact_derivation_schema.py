"""Schema + LineageNode tests for proposal 0011 (artifact-to-artifact lineage)."""
import sqlite3
import pytest
from casetrack_qc import artifact_derivation as ad


def _conn():
    return sqlite3.connect(":memory:")


def test_ensure_schema_idempotent():
    conn = _conn()
    assert not ad.derivation_schema_exists(conn)
    first = ad.ensure_derivation_schema(conn)
    assert any("CREATE TABLE artifact_derivation" in s for s in first)
    assert ad.derivation_schema_exists(conn)
    # second call is a no-op
    assert ad.ensure_derivation_schema(conn) == []


def test_lineage_node_roundtrip():
    for s in (
        "cohort:joint_genotype@cohort147_v1",
        "reference:pon",
        "analysis:specimen/SPEC1/clair3",
    ):
        node = ad.LineageNode.parse(s)
        assert node.canonical() == s


def test_lineage_node_fields():
    c = ad.LineageNode.parse("cohort:joint_genotype@cohort147_v1")
    assert c.scope == "cohort" and c.analysis == "joint_genotype" and c.run_tag == "cohort147_v1"
    r = ad.LineageNode.parse("reference:pon")
    assert r.scope == "reference" and r.ref_key == "pon"
    a = ad.LineageNode.parse("analysis:specimen/SPEC1/clair3")
    assert a.scope == "analysis" and a.entity_level == "specimen" and a.entity_id == "SPEC1" and a.analysis == "clair3"


def test_lineage_node_rejects_malformed():
    for bad in ("", "bogus:x", "cohort:noatsign", "reference:", "analysis:only/two"):
        with pytest.raises(ad.DerivationError):
            ad.LineageNode.parse(bad)
