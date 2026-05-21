import sqlite3
import pytest
from casetrack_qc import reference_artifacts as ra


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
