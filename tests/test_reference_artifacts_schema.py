import sqlite3
import pytest
from casetrack_qc import reference_artifacts as ra
import casetrack


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    # minimal three-level + cohort_artifacts so FKs resolve
    conn.execute("CREATE TABLE assays (assay_id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE cohort_artifacts (artifact_id INTEGER PRIMARY KEY AUTOINCREMENT)"
    )
    return conn


def test_ensure_schema_is_idempotent_and_creates_both_tables():
    conn = _conn()
    first = ra.ensure_reference_schema(conn)
    assert ra.reference_schema_exists(conn) is True
    assert any("reference_artifacts" in s for s in first)
    assert any("reference_usage" in s for s in first)
    # second call is a no-op
    second = ra.ensure_reference_schema(conn)
    assert second == []


def test_reference_schema_exists_false_when_absent():
    conn = _conn()
    assert ra.reference_schema_exists(conn) is False


def test_validate_references_accepts_well_formed_block():
    refs = {
        "genome": {"path": "/db/hg38.fa", "version": "hg38_v0", "kind": "genome"},
        "dbsnp": {"path": "/db/dbsnp.vcf.gz", "version": "b156",
                  "kind": "known_variants"},
    }
    casetrack._validate_references(refs)  # no raise


def test_validate_references_requires_path_and_version():
    with pytest.raises(casetrack.SchemaError):
        casetrack._validate_references({"genome": {"version": "hg38_v0"}})
    with pytest.raises(casetrack.SchemaError):
        casetrack._validate_references({"genome": {"path": "/db/hg38.fa"}})


def test_validate_references_rejects_bad_kind_and_key():
    with pytest.raises(casetrack.SchemaError):
        casetrack._validate_references(
            {"genome": {"path": "/p", "version": "v", "kind": "nonsense"}}
        )
    with pytest.raises(casetrack.SchemaError):
        casetrack._validate_references(
            {"1bad": {"path": "/p", "version": "v"}}
        )


def test_validate_analyses_uses_must_be_known_refs():
    analyses = {"clair3": {"level": "specimen", "uses": ["genome", "dbsnp"]}}
    refs = {"genome": {"path": "/p", "version": "v"},
            "dbsnp": {"path": "/p", "version": "v"}}
    casetrack._validate_analyses(analyses, references=refs)  # no raise
    with pytest.raises(casetrack.SchemaError):
        casetrack._validate_analyses(
            {"clair3": {"level": "specimen", "uses": ["ghost"]}}, references=refs
        )
