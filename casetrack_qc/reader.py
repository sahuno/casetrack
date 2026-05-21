"""The `_active` read-model cascade (§4.4) + DuckDB view helper.

An assay is **usable** iff:

- Its patient has ``consent_status = 'consented'`` or
  ``consented_limited_use``
- All three levels (patient / specimen / assay) have
  ``qc_status NOT IN ('fail', 'censored', 'consent_revoked')``.

``warn`` propagates but doesn't exclude. These helpers are SQL-only so they
work on both the raw sqlite3 connection (used by status / rerun / export)
and on DuckDB's attached SQLite view (used by ``query``).

Author: Samuel Ahuno (ekwame001&#x0040;gmail.com)
"""
from __future__ import annotations

import sqlite3
from typing import Iterable


# QC statuses that exclude by default (proposal §4.4).
DEFAULT_QC_EXCLUDE: tuple[str, ...] = ("fail", "censored", "consent_revoked")

# Consent statuses excluded by default (§4.3). `consented_limited_use` is
# treated as usable.
DEFAULT_CONSENT_INCLUDE: tuple[str, ...] = ("consented", "consented_limited_use")


def _qc_exclusion_clause(alias: str, include_censored: bool) -> str:
    """Return a SQL fragment like ``alias.qc_status NOT IN (...)`` — or empty
    if ``include_censored`` says don't filter."""
    if include_censored:
        # Still filter consent_revoked on patient alias when cascade is in force.
        return f"{alias}.qc_status != 'consent_revoked'"
    # Exclude everything that's not 'pass' / 'warn'.
    return (
        f"{alias}.qc_status NOT IN ('fail', 'censored', 'consent_revoked')"
    )


def _consent_clause(alias: str, include_consent_revoked: bool) -> str:
    if include_consent_revoked:
        return "1=1"
    # Include both 'consented' and 'consented_limited_use'; anything else is
    # excluded by default.
    return f"{alias}.consent_status IN ('consented', 'consented_limited_use')"


def build_active_assay_sql(
    *,
    include_censored: bool = False,
    include_consent_revoked: bool = False,
) -> str:
    """The SQL that returns every active ``assay_id`` per §4.4.

    Intentionally a single self-contained query — callers can either
    ``SELECT assay_id`` as-is, or wrap it as a subquery / CTE for joins.
    """
    qc_a = _qc_exclusion_clause("a", include_censored)
    qc_s = _qc_exclusion_clause("s", include_censored)
    qc_p = _qc_exclusion_clause("p", include_censored)
    consent_p = _consent_clause("p", include_consent_revoked)
    return f"""
        SELECT a.assay_id
        FROM assays  a
        JOIN specimens s ON a.specimen_id = s.specimen_id
        JOIN patients  p ON s.patient_id  = p.patient_id
        WHERE {qc_a}
          AND {qc_s}
          AND {qc_p}
          AND {consent_p}
    """


def active_assay_ids(
    conn: sqlite3.Connection,
    *,
    include_censored: bool = False,
    include_consent_revoked: bool = False,
) -> set[str]:
    sql = build_active_assay_sql(
        include_censored=include_censored,
        include_consent_revoked=include_consent_revoked,
    )
    return {
        r[0] for r in conn.execute(sql).fetchall()
    }


def active_specimen_ids(
    conn: sqlite3.Connection,
    *,
    include_censored: bool = False,
    include_consent_revoked: bool = False,
) -> set[str]:
    qc_s = _qc_exclusion_clause("s", include_censored)
    qc_p = _qc_exclusion_clause("p", include_censored)
    consent_p = _consent_clause("p", include_consent_revoked)
    sql = f"""
        SELECT s.specimen_id
        FROM specimens s
        JOIN patients  p ON s.patient_id = p.patient_id
        WHERE {qc_s} AND {qc_p} AND {consent_p}
    """
    return {r[0] for r in conn.execute(sql).fetchall()}


def active_patient_ids(
    conn: sqlite3.Connection,
    *,
    include_censored: bool = False,
    include_consent_revoked: bool = False,
) -> set[str]:
    qc_p = _qc_exclusion_clause("p", include_censored)
    consent_p = _consent_clause("p", include_consent_revoked)
    sql = f"""
        SELECT p.patient_id
        FROM patients p
        WHERE {qc_p} AND {consent_p}
    """
    return {r[0] for r in conn.execute(sql).fetchall()}


def exclusion_breakdown(conn: sqlite3.Connection) -> dict:
    """Return counts + IDs of entities excluded by the cascade, by reason.

    Used by ``status --usable`` (§8.1) and the export audit line (§5.2).
    """
    out = {
        "qc_failed_assays": [],
        "censored_assays": [],
        "consent_revoked_patients": [],
        "consent_revoked_assays": [],
    }
    for (assay_id, status) in conn.execute(
        "SELECT assay_id, qc_status FROM assays "
        "WHERE qc_status IN ('fail','censored')"
    ).fetchall():
        if status == "fail":
            out["qc_failed_assays"].append(assay_id)
        else:
            out["censored_assays"].append(assay_id)

    consent_revoked_pids = [
        r[0] for r in conn.execute(
            "SELECT patient_id FROM patients "
            "WHERE consent_status NOT IN ('consented', 'consented_limited_use')"
        ).fetchall()
    ]
    out["consent_revoked_patients"] = consent_revoked_pids
    if consent_revoked_pids:
        placeholders = ", ".join("?" * len(consent_revoked_pids))
        assay_rows = conn.execute(
            f"SELECT a.assay_id FROM assays a "
            f"JOIN specimens s ON a.specimen_id = s.specimen_id "
            f"WHERE s.patient_id IN ({placeholders})",
            consent_revoked_pids,
        ).fetchall()
        out["consent_revoked_assays"] = [r[0] for r in assay_rows]
    return out


def install_active_views(duckdb_con) -> None:
    """Attach DuckDB views ``_active`` (cascaded) alongside the existing ``_``.

    The ``_`` view is raw (all rows, no QC filter); ``_active`` applies the
    §4.4 cascade with default exclusions.
    """
    # `_active` mirrors the shape of `_` (USING (...) joins remove the join
    # columns from the SELECT list so the outputs line up with flat-mode
    # expectations) but applies the §4.4 cascade.
    sql_active = """
        CREATE VIEW "_active" AS
        SELECT *
        FROM proj.assays
        JOIN proj.specimens USING (specimen_id)
        JOIN proj.patients USING (patient_id)
        WHERE assays.qc_status NOT IN ('fail', 'censored', 'consent_revoked')
          AND specimens.qc_status NOT IN ('fail', 'censored', 'consent_revoked')
          AND patients.qc_status NOT IN ('fail', 'censored', 'consent_revoked')
          AND patients.consent_status IN ('consented', 'consented_limited_use')
    """
    try:
        duckdb_con.execute(sql_active)
    except Exception:
        # Older v0.3 DBs that haven't been through migrate-qc may not have
        # the required columns — skip silently so `query` still works.
        pass


def install_cohort_artifact_view(duckdb_con) -> None:
    """Attach a ``_cohort_artifacts`` DuckDB view (proposal 0009).

    Exposes each cohort artifact with two derived columns:
      - ``n_censored_inputs`` — contributing assays currently excluded by the
        §4.4 cascade (QC fail/censored or consent-revoked).
      - ``stale`` — boolean, true when ``n_censored_inputs > 0``.

    Silent no-op on projects without the cohort-artifact tables (pre-0009).
    """
    # proj-qualified active-assay set (mirrors build_active_assay_sql, but the
    # tables live in the attached `proj` catalog inside DuckDB).
    active_sql = """
        SELECT a.assay_id
        FROM proj.assays a
        JOIN proj.specimens s ON a.specimen_id = s.specimen_id
        JOIN proj.patients  p ON s.patient_id  = p.patient_id
        WHERE a.qc_status NOT IN ('fail', 'censored', 'consent_revoked')
          AND s.qc_status NOT IN ('fail', 'censored', 'consent_revoked')
          AND p.qc_status NOT IN ('fail', 'censored', 'consent_revoked')
          AND p.consent_status IN ('consented', 'consented_limited_use')
    """
    censored_count = f"""
        (SELECT COUNT(*) FROM proj.cohort_artifact_inputs ci
          WHERE ci.artifact_id = ca.artifact_id
            AND ci.assay_id NOT IN ({active_sql}))
    """
    sql = f"""
        CREATE VIEW "_cohort_artifacts" AS
        SELECT ca.artifact_id, ca.analysis, ca.run_tag, ca.path, ca.checksum,
               ca.n_inputs, ca.stats_json, ca.created_at,
               {censored_count} AS n_censored_inputs,
               ({censored_count} > 0) AS stale
        FROM proj.cohort_artifacts ca
    """
    try:
        duckdb_con.execute(sql)
    except Exception:
        # Pre-0009 DBs lack the tables — skip so `query` still works.
        pass


__all__ = [
    "DEFAULT_CONSENT_INCLUDE",
    "DEFAULT_QC_EXCLUDE",
    "active_assay_ids",
    "active_patient_ids",
    "active_specimen_ids",
    "build_active_assay_sql",
    "exclusion_breakdown",
    "install_active_views",
    "install_cohort_artifact_view",
]
