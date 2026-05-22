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

# proj-qualified active-assay subquery used inside DuckDB views (the tables live
# in the attached `proj` catalog). Mirrors build_active_assay_sql with default
# exclusions. Shared by install_cohort_artifact_view and _derived_stale_cte so
# the cohort input-staleness definition has exactly one source of truth.
_ACTIVE_ASSAY_SQL = """
        SELECT a.assay_id
        FROM proj.assays a
        JOIN proj.specimens s ON a.specimen_id = s.specimen_id
        JOIN proj.patients  p ON s.patient_id  = p.patient_id
        WHERE a.qc_status NOT IN ('fail', 'censored', 'consent_revoked')
          AND s.qc_status NOT IN ('fail', 'censored', 'consent_revoked')
          AND p.qc_status NOT IN ('fail', 'censored', 'consent_revoked')
          AND p.consent_status IN ('consented', 'consented_limited_use')
    """

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
    """Attach a ``_cohort_artifacts`` DuckDB view (proposal 0009 + 0010).

    Exposes each cohort artifact with derived columns:
      - ``n_censored_inputs`` — contributing assays currently excluded by the
        §4.4 cascade (QC fail/censored or consent-revoked).
      - ``stale`` — boolean, true when ``n_censored_inputs > 0`` (input-stale;
        distinct from ``ref_stale``).
      - ``ref_stale`` — boolean (proposal 0010), true when any cohort-scope
        reference-usage row for this artifact has a stale version. Present only
        on 0010+ projects; absent on pre-0010 projects (view still created
        without the column so ``query`` continues to work).

    Silent no-op on projects without the cohort-artifact tables (pre-0009).
    """
    # proj-qualified active-assay set (mirrors build_active_assay_sql, but the
    # tables live in the attached `proj` catalog inside DuckDB).
    active_sql = _ACTIVE_ASSAY_SQL
    censored_count = f"""
        (SELECT COUNT(*) FROM proj.cohort_artifact_inputs ci
          WHERE ci.artifact_id = ca.artifact_id
            AND ci.assay_id NOT IN ({active_sql}))
    """
    # Proposal 0010: ref_stale subquery. True when any cohort-scope usage row
    # for this artifact references a version that no longer matches
    # reference_artifacts.version. Distinct from input `stale`.
    ref_stale_subquery = """
        EXISTS (
            SELECT 1
            FROM proj.reference_usage ru
            LEFT JOIN proj.reference_artifacts rr ON rr.ref_key = ru.ref_key
            WHERE ru.scope = 'cohort'
              AND ru.artifact_id = ca.artifact_id
              AND (rr.version IS NULL OR rr.version <> ru.version_used)
        )
    """
    # Proposal 0013: region_scope (cohort_artifacts column) + derived
    # scope_ref_key (the ref_key it resolves to, NULL when label-only).
    # Column-presence-guarded so pre-0013 projects keep a working view.
    try:
        have_scope = duckdb_con.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_catalog = 'proj' "
            "  AND table_name = 'cohort_artifacts' "
            "  AND column_name = 'region_scope'"
        ).fetchone() is not None
    except Exception:
        have_scope = False
    scope_expr = "ca.region_scope" if have_scope else "CAST(NULL AS VARCHAR)"
    scope_ref_key_expr = (
        "(SELECT rr.ref_key FROM proj.reference_artifacts rr "
        f"WHERE rr.ref_key = {scope_expr})"
    )
    # Proposal 0011: derived_stale. Joins the recursive transitive closure
    # (derived) computed from BASE TABLES (self_safe=True) so it can be defined
    # inside the very view it would otherwise self-reference.
    # Requires: 0009 (cohort_artifacts) + 0010 (reference_usage) + 0011 (artifact_derivation).
    sql_with_ref_stale_and_derived = f"""
        CREATE VIEW "_cohort_artifacts" AS
        WITH RECURSIVE {_derived_stale_cte(self_safe=True)}
        SELECT ca.artifact_id, ca.analysis, ca.run_tag, ca.path, ca.checksum,
               ca.n_inputs, ca.stats_json, ca.created_at,
               {scope_expr} AS region_scope,
               {scope_ref_key_expr} AS scope_ref_key,
               {censored_count} AS n_censored_inputs,
               ({censored_count} > 0) AS stale,
               {ref_stale_subquery} AS ref_stale,
               COALESCE(d.derived_stale, FALSE) AS derived_stale
        FROM proj.cohort_artifacts ca
        LEFT JOIN derived d
               ON d.node = 'cohort:' || ca.analysis || '@' || ca.run_tag
    """
    # Proposal 0010 view without 0011 derived_stale. This is the exact body
    # that shipped in v0.8.0 before Task 8 added artifact_derivation support.
    # Required by 0010-era DBs that have reference_usage/reference_artifacts
    # but lack artifact_derivation (not yet migrated to 0011).
    sql_with_ref_stale_only = f"""
        CREATE VIEW "_cohort_artifacts" AS
        SELECT ca.artifact_id, ca.analysis, ca.run_tag, ca.path, ca.checksum,
               ca.n_inputs, ca.stats_json, ca.created_at,
               {scope_expr} AS region_scope,
               {scope_ref_key_expr} AS scope_ref_key,
               {censored_count} AS n_censored_inputs,
               ({censored_count} > 0) AS stale,
               {ref_stale_subquery} AS ref_stale
        FROM proj.cohort_artifacts ca
    """
    # Pre-0010 fallback: no ref_stale column, no derived_stale column.
    # Note: reference_artifacts is absent in this tier, so scope_ref_key cannot
    # be resolved — emit a typed-NULL placeholder to keep the column shape
    # consistent with the 0010+ tiers above.
    sql_without_ref_stale = f"""
        CREATE VIEW "_cohort_artifacts" AS
        SELECT ca.artifact_id, ca.analysis, ca.run_tag, ca.path, ca.checksum,
               ca.n_inputs, ca.stats_json, ca.created_at,
               {scope_expr} AS region_scope,
               CAST(NULL AS VARCHAR) AS scope_ref_key,
               {censored_count} AS n_censored_inputs,
               ({censored_count} > 0) AS stale
        FROM proj.cohort_artifacts ca
    """
    try:
        # Tier 1: full 0011 view (artifact_derivation + reference tables present).
        duckdb_con.execute(sql_with_ref_stale_and_derived)
    except Exception:
        # Tier 2: 0010-era view — reference tables present but artifact_derivation
        # is absent (pre-0011 project not yet migrated). Preserves ref_stale so
        # shipped 0010 functionality is not silently regressed.
        try:
            duckdb_con.execute(sql_with_ref_stale_only)
        except Exception:
            # Tier 3: pre-0010 projects lack reference_usage/reference_artifacts —
            # fall back to the original view without ref_stale.
            try:
                duckdb_con.execute(sql_without_ref_stale)
            except Exception:
                # Pre-0009 DBs lack the cohort-artifact tables — skip so `query`
                # still works.
                pass


def install_reference_usage_view(duckdb_con) -> None:
    """Attach a ``_reference_usage`` DuckDB view (proposal 0010).

    Exposes every usage edge with two derived columns:
      - ``current_version`` — the current version in ``reference_artifacts``
        (NULL when the ref_key has been removed from the TOML).
      - ``is_stale`` — boolean, true when ``current_version`` is NULL or
        differs from ``version_used``.

    Silent no-op on projects without the reference tables (pre-0010).
    """
    sql = """
        CREATE VIEW "_reference_usage" AS
        SELECT u.usage_id, u.scope, u.entity_level, u.entity_id,
               u.analysis, u.artifact_id, u.ref_key,
               u.version_used, u.recorded_at,
               r.version AS current_version,
               CASE
                   WHEN r.version IS NULL THEN TRUE
                   WHEN r.version <> u.version_used THEN TRUE
                   ELSE FALSE
               END AS is_stale
        FROM proj.reference_usage u
        LEFT JOIN proj.reference_artifacts r ON r.ref_key = u.ref_key
    """
    try:
        duckdb_con.execute(sql)
    except Exception:
        # Pre-0010 projects lack reference_usage/reference_artifacts — skip so
        # `query` still works on pre-0010 projects.
        pass


def _derived_stale_cte(self_safe: bool = False) -> str:
    """Reusable ``WITH RECURSIVE`` body computing derived_stale per node (0011).

    Produces three CTEs plus a final ``derived(node, derived_stale)`` relation.
    The caller must prefix this with ``WITH RECURSIVE`` and append a SELECT.

    Semantics (must match casetrack_qc.artifact_derivation.derived_staleness):
      - ``edges``  : up-edges = artifact_derivation (down->up) UNION the 0010
        reference_usage consumer->``reference:<ref_key>`` edge.
      - ``direct`` : each node's OWN single-hop staleness. cohort = (>=1 censored
        input) OR (cohort ref-version mismatch); analysis = (ref mismatch);
        reference = FALSE (references have no intrinsic direct staleness).
      - ``reach``  : transitive closure (>=1 hop) of edges, seeded by every edge
        (so a node's own direct staleness is NEVER folded into its derived_stale).
        ``UNION`` (not UNION ALL) dedupes the working set so cycles terminate.
      - ``derived``: node is derived_stale iff any node it reaches is direct-stale.

    ``self_safe=True`` computes cohort/analysis direct staleness from BASE TABLES
    (proj.cohort_artifacts / proj.reference_usage / proj.reference_artifacts)
    instead of the ``_cohort_artifacts`` / ``_reference_usage`` DuckDB views.
    This variant is REQUIRED when the CTE is embedded in the definition of the
    ``_cohort_artifacts`` view itself (and in ``_artifact_derivation``, which is
    installed in the same pass and must not depend on view-install order) —
    reading the views there would be a self-reference / forward-reference.
    """
    if self_safe:
        cohort_direct = """
        SELECT 'cohort:' || ca.analysis || '@' || ca.run_tag AS node,
               ((SELECT COUNT(*) FROM proj.cohort_artifact_inputs ci
                  WHERE ci.artifact_id = ca.artifact_id
                    AND ci.assay_id NOT IN (%(active)s)) > 0
                OR EXISTS (
                    SELECT 1 FROM proj.reference_usage ru2
                    LEFT JOIN proj.reference_artifacts rr2 ON rr2.ref_key = ru2.ref_key
                    WHERE ru2.scope = 'cohort'
                      AND ru2.artifact_id = ca.artifact_id
                      AND (rr2.version IS NULL OR rr2.version <> ru2.version_used))
               ) AS d
        FROM proj.cohort_artifacts ca
        """ % {"active": _ACTIVE_ASSAY_SQL}
        analysis_direct = """
        SELECT 'analysis:' || ru.entity_level || '/' || ru.entity_id || '/' || ru.analysis AS node,
               BOOL_OR(rr.version IS NULL OR rr.version <> ru.version_used) AS d
        FROM proj.reference_usage ru
        LEFT JOIN proj.reference_artifacts rr ON rr.ref_key = ru.ref_key
        WHERE ru.scope = 'analysis'
        GROUP BY 1
        """
    else:
        cohort_direct = (
            "SELECT 'cohort:' || analysis || '@' || run_tag AS node, "
            "(stale OR ref_stale) AS d FROM \"_cohort_artifacts\""
        )
        analysis_direct = (
            "SELECT 'analysis:' || entity_level || '/' || entity_id || '/' || analysis, "
            "BOOL_OR(is_stale) FROM \"_reference_usage\" WHERE scope='analysis' GROUP BY 1"
        )
    return f"""
    edges AS (
        SELECT down_node, up_node FROM proj.artifact_derivation
        UNION
        SELECT CASE ru.scope
                 WHEN 'cohort'   THEN 'cohort:' || ca.analysis || '@' || ca.run_tag
                 WHEN 'analysis' THEN 'analysis:' || ru.entity_level || '/' || ru.entity_id || '/' || ru.analysis
               END AS down_node,
               'reference:' || ru.ref_key AS up_node
        FROM proj.reference_usage ru
        LEFT JOIN proj.cohort_artifacts ca ON ca.artifact_id = ru.artifact_id
        WHERE ru.scope IN ('cohort', 'analysis')
    ),
    direct AS (
        {cohort_direct}
        UNION ALL
        SELECT 'reference:' || ref_key, FALSE FROM proj.reference_artifacts
        UNION ALL
        {analysis_direct}
    ),
    reach(start, cur) AS (
        SELECT down_node, up_node FROM edges
        UNION
        SELECT r.start, e.up_node FROM reach r JOIN edges e ON e.down_node = r.cur
    ),
    derived AS (
        SELECT reach.start AS node, BOOL_OR(COALESCE(direct.d, FALSE)) AS derived_stale
        FROM reach LEFT JOIN direct ON direct.node = reach.cur
        GROUP BY reach.start
    )
    """


def install_artifact_derivation_view(duckdb_con) -> None:
    """Attach ``_artifact_derivation`` (edges + per-down_node derived_stale). 0011.

    Each row is one ``artifact_derivation`` edge annotated with
    ``down_derived_stale`` — the transitive derived-staleness of the edge's
    downstream node (same closure as the ``derived_stale`` column on
    ``_cohort_artifacts``). Uses ``_derived_stale_cte(self_safe=True)`` so it
    reads only base tables and is independent of the other view installers.

    Silent no-op on pre-0011 projects (no artifact_derivation table) or pre-0010
    projects (the CTE references reference_usage / reference_artifacts).
    """
    sql = f"""
        CREATE VIEW "_artifact_derivation" AS
        WITH RECURSIVE {_derived_stale_cte(self_safe=True)}
        SELECT e.down_node, e.up_node,
               COALESCE(d.derived_stale, FALSE) AS down_derived_stale
        FROM proj.artifact_derivation e
        LEFT JOIN derived d ON d.node = e.down_node
    """
    try:
        duckdb_con.execute(sql)
    except Exception:
        # Pre-0011 (no artifact_derivation) or pre-0010 (no reference tables) —
        # skip so `query` still works on older projects.
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
    "install_artifact_derivation_view",
    "install_cohort_artifact_view",
    "install_reference_usage_view",
]
