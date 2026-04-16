"""Tests for the v0.3 SQLite engine helpers (proposal 0001 §9.1).

Covers `open_project_db` pragma defaults, `begin_immediate` transaction
semantics (commit + rollback), and DDL rendering from a schema dict.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import casetrack


# ── open_project_db ───────────────────────────────────────────────────────────


def test_open_sets_wal_foreign_keys_and_busy_timeout(tmp_path: Path):
    conn = casetrack.open_project_db(tmp_path / "x.db")
    try:
        (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert journal_mode.lower() == "wal"

        (foreign_keys,) = conn.execute("PRAGMA foreign_keys").fetchone()
        assert foreign_keys == 1

        (busy_timeout,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert busy_timeout == casetrack.SQLITE_BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_foreign_keys_actually_enforce(tmp_path: Path):
    conn = casetrack.open_project_db(tmp_path / "x.db")
    try:
        conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child ("
            "  id TEXT PRIMARY KEY, "
            "  parent_id TEXT NOT NULL, "
            "  FOREIGN KEY (parent_id) REFERENCES parent(id)"
            ")"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO child VALUES ('c1', 'missing_parent')")
    finally:
        conn.close()


# ── begin_immediate ───────────────────────────────────────────────────────────


def test_begin_immediate_commits_on_success(tmp_path: Path):
    conn = casetrack.open_project_db(tmp_path / "x.db")
    try:
        conn.execute("CREATE TABLE t (id INTEGER)")
        with casetrack.begin_immediate(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            conn.execute("INSERT INTO t VALUES (2)")
        rows = conn.execute("SELECT id FROM t ORDER BY id").fetchall()
        assert rows == [(1,), (2,)]
    finally:
        conn.close()


def test_begin_immediate_rolls_back_on_exception(tmp_path: Path):
    conn = casetrack.open_project_db(tmp_path / "x.db")
    try:
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (100)")
        conn.commit()

        with pytest.raises(RuntimeError, match="fail on purpose"):
            with casetrack.begin_immediate(conn):
                conn.execute("INSERT INTO t VALUES (200)")
                raise RuntimeError("fail on purpose")

        rows = conn.execute("SELECT id FROM t ORDER BY id").fetchall()
        assert rows == [(100,)]  # the 200 INSERT was rolled back
    finally:
        conn.close()


# ── schema_to_ddl ─────────────────────────────────────────────────────────────


def _load_blank(tmp_path: Path) -> dict:
    p = tmp_path / "schema.toml"
    p.write_text(casetrack.TEMPLATES["blank"]("t"))
    return casetrack.load_schema(p)


def test_ddl_orders_parents_before_children(tmp_path: Path):
    schema = _load_blank(tmp_path)
    ddls = casetrack.schema_to_ddl(schema)
    joined = " ".join(ddls)
    assert joined.index('"patients"') < joined.index('"specimens"') < joined.index('"assays"')


def test_ddl_declares_primary_keys(tmp_path: Path):
    schema = _load_blank(tmp_path)
    ddls = casetrack.schema_to_ddl(schema)
    assert any('PRIMARY KEY ("patient_id")' in d for d in ddls)
    assert any('PRIMARY KEY ("specimen_id")' in d for d in ddls)
    assert any('PRIMARY KEY ("assay_id")' in d for d in ddls)


def test_ddl_declares_foreign_keys(tmp_path: Path):
    schema = _load_blank(tmp_path)
    ddls = casetrack.schema_to_ddl(schema)
    joined = "\n".join(ddls)
    assert 'FOREIGN KEY ("patient_id") REFERENCES "patients"("patient_id")' in joined
    assert 'FOREIGN KEY ("specimen_id") REFERENCES "specimens"("specimen_id")' in joined
    assert "ON DELETE RESTRICT" in joined


def test_ddl_renders_enums_as_check(tmp_path: Path):
    p = tmp_path / "schema.toml"
    p.write_text(casetrack.TEMPLATES["hgsoc"]("t"))
    schema = casetrack.load_schema(p)
    ddls = casetrack.schema_to_ddl(schema)
    joined = "\n".join(ddls)
    assert """CHECK ("assay_type" IN ('scRNA', 'ATAC', 'WGS', 'WES', 'ONT', 'Visium'))""" in joined


def test_apply_schema_creates_all_tables(tmp_path: Path):
    schema = _load_blank(tmp_path)
    conn = casetrack.open_project_db(tmp_path / "x.db")
    try:
        casetrack.apply_schema(conn, schema)
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        assert names == ["assays", "patients", "specimens"]
    finally:
        conn.close()


def test_apply_schema_rolls_back_on_bad_ddl(tmp_path: Path, monkeypatch):
    """If any CREATE TABLE fails, none of the tables should be left behind."""
    schema = _load_blank(tmp_path)
    # Corrupt the last table's DDL so CREATE fails after patients/specimens are created.
    def _broken_ddl(s):
        return ["CREATE TABLE x (id INT)", "CREATE TABLE y (id INT)", "CREATE bogus_syntax"]
    monkeypatch.setattr(casetrack, "schema_to_ddl", _broken_ddl)

    conn = casetrack.open_project_db(tmp_path / "x.db")
    try:
        with pytest.raises(sqlite3.Error):
            casetrack.apply_schema(conn, schema)
        # Both earlier tables should have been rolled back.
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert names == []
    finally:
        conn.close()


# ── identifier / literal quoting ──────────────────────────────────────────────


def test_quote_ident_rejects_embedded_double_quote():
    with pytest.raises(casetrack.SchemaError, match="double-quote"):
        casetrack._quote_ident('bad"name')


def test_quote_literal_escapes_single_quotes():
    assert casetrack._quote_literal("o'brien") == "'o''brien'"


def test_quote_literal_bool_to_int():
    assert casetrack._quote_literal(True) == "1"
    assert casetrack._quote_literal(False) == "0"
