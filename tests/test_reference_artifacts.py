# tests/test_reference_artifacts.py
import sqlite3
import pytest
from casetrack_qc import reference_artifacts as ra


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE assays (assay_id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE cohort_artifacts (artifact_id INTEGER PRIMARY KEY AUTOINCREMENT)"
    )
    ra.ensure_reference_schema(conn)
    return conn


def test_sync_inserts_updates_and_reports_version_changes():
    conn = _conn()
    toml_refs = {
        "genome": {"path": "/db/hg38.fa", "version": "hg38_v0", "kind": "genome"},
        "gtf": {"path": "/db/g.v47.gtf", "version": "v47", "kind": "annotation"},
    }
    changes = ra.sync_references_from_toml(conn, toml_refs)
    assert {c["ref_key"] for c in changes} == {"genome", "gtf"}
    assert all(c["old_version"] is None for c in changes)  # all new

    # bump genome version, leave gtf
    toml_refs["genome"]["version"] = "hg38_v1"
    changes = ra.sync_references_from_toml(conn, toml_refs)
    assert changes == [
        {"ref_key": "genome", "old_version": "hg38_v0", "new_version": "hg38_v1"}
    ]
    assert ra.get_reference(conn, "genome").version == "hg38_v1"


def test_record_usage_is_idempotent_per_output_and_ref():
    conn = _conn()
    ra.sync_references_from_toml(
        conn, {"genome": {"path": "/p", "version": "v1"}}
    )
    ra.record_usage(conn, scope="analysis", entity_level="specimen",
                    entity_id="S1", analysis="clair3", ref_key="genome",
                    version_used="v1", transaction_id="t1")
    # same edge again with a newer version_used overwrites (re-append semantics)
    ra.record_usage(conn, scope="analysis", entity_level="specimen",
                    entity_id="S1", analysis="clair3", ref_key="genome",
                    version_used="v2", transaction_id="t2")
    rows = conn.execute(
        "SELECT version_used FROM reference_usage WHERE entity_id='S1'"
    ).fetchall()
    assert rows == [("v2",)]


def test_staleness_three_states_and_reason():
    conn = _conn()
    ra.sync_references_from_toml(conn, {
        "genome": {"path": "/p", "version": "hg38_v1"},
        "gtf": {"path": "/p", "version": "v47"},
    })
    # fresh: used current version
    ra.record_usage(conn, scope="analysis", entity_level="specimen",
                    entity_id="S_fresh", analysis="clair3", ref_key="genome",
                    version_used="hg38_v1", transaction_id="t")
    # stale: used an old version
    ra.record_usage(conn, scope="analysis", entity_level="specimen",
                    entity_id="S_stale", analysis="clair3", ref_key="genome",
                    version_used="hg38_v0", transaction_id="t")

    s_fresh = ra.output_staleness(conn, scope="analysis",
                                  entity_level="specimen", entity_id="S_fresh",
                                  analysis="clair3")
    assert s_fresh["state"] == "fresh" and s_fresh["reasons"] == []

    s_stale = ra.output_staleness(conn, scope="analysis",
                                  entity_level="specimen", entity_id="S_stale",
                                  analysis="clair3")
    assert s_stale["state"] == "STALE"
    assert s_stale["reasons"] == ["genome: hg38_v0 -> hg38_v1"]

    # untracked: an output with no usage rows
    s_unk = ra.output_staleness(conn, scope="analysis", entity_level="specimen",
                                entity_id="S_none", analysis="modkit")
    assert s_unk["state"] == "untracked"


def test_staleness_removed_ref_key_is_stale():
    conn = _conn()
    ra.sync_references_from_toml(conn, {"dbsnp": {"path": "/p", "version": "b156"}})
    ra.record_usage(conn, scope="analysis", entity_level="specimen",
                    entity_id="S1", analysis="clair3", ref_key="dbsnp",
                    version_used="b156", transaction_id="t")
    # remove dbsnp from the canonical set
    conn.execute("DELETE FROM reference_artifacts WHERE ref_key='dbsnp'")
    s = ra.output_staleness(conn, scope="analysis", entity_level="specimen",
                            entity_id="S1", analysis="clair3")
    assert s["state"] == "STALE"
    assert s["reasons"] == ["reference removed: dbsnp"]
