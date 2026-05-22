# register-cohort (proposal 0012) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `casetrack register-cohort --project-dir . --samplesheet cohort.tsv` command that explodes one schema-native wide sample sheet (one row per assay, full patient→specimen→assay chain) into the three normalized tables in a single transaction, deduping patients/specimens across rows.

**Architecture:** A schema-driven column router + an explode-to-three-frames step + a pre-write validator, feeding a `_upsert_level` engine **extracted from** `cmd_add_metadata_project` so both commands share one tested upsert path. Create-by-default; `--dry-run` previews; all-or-nothing transaction. Lives in `casetrack.py` next to `register`/`add-metadata`/`migrate`.

**Tech Stack:** Python 3.10–3.13, stdlib `sqlite3`, `pandas` (TSV parse + dedup), argparse, TOML schema. pytest.

**Reference reading before you start:**
- `docs/proposals/0012-register-cohort.md` — the design (§6 is the spec).
- `casetrack.py:6806-7032` — `cmd_add_metadata_project`, the engine to extract from. Read it fully.
- `casetrack.py:6793` `_MetadataRouting`; `casetrack.py:1219` `_quote_ident`; `casetrack.py:3443` `_coerce_for_sqlite`; `validate_hierarchy_id` / `check_id_case_unique` / `_preload_folded_ids` (grep for defs) — helpers `_upsert_level` uses.
- `casetrack.py:80` `LEVEL_ORDER = ("patient","specimen","assay")`; `_resolve_project`, `open_project_db`, `begin_immediate`, `log_project_provenance`, `_new_transaction_id`, `_checksum`, `PROJECT_DB_NAME`.
- `casetrack.py:8598` the `commands` dispatch dict; the `add-metadata` subparser at ~`casetrack.py:8311`.

**Conventions:** clean exit 2 on validation errors (never a traceback); module functions take an open `conn` and the CLI owns the transaction + provenance; commit messages end with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`. Branch `feature/register-cohort-0012` (already created). Test: `cd /data1/greenbab/users/ahunos/apps/casetrack && python3 -m pytest tests/ -q`.

---

## Task 1: Extract `_upsert_level` from `cmd_add_metadata_project` (behavior-preserving)

**Files:**
- Modify: `casetrack.py` (`cmd_add_metadata_project` ~6806-7032; add `_upsert_level` just above it)
- Test: `tests/test_add_metadata.py` (existing — must stay green), `tests/test_upsert_level.py` (new unit tests)

The engine extracted is the in-transaction validate+update+insert from `cmd_add_metadata_project`. **One behavior change required for register-cohort:** the "no columns besides key" hard-error must NOT live in `_upsert_level` (a patient frame may legitimately be key-only). That guard stays in `cmd_add_metadata_project`. `_upsert_level` must insert key-only / key+FK rows when `meta_cols` is empty.

- [ ] **Step 1: Write the failing unit test**

```python
# tests/test_upsert_level.py
"""Unit tests for the shared _upsert_level engine (proposal 0012 §6.6)."""
import sqlite3
import pandas as pd
import pytest
import casetrack


def _schema():
    return {
        "project": {"schema_v": 1},
        "levels": {
            "patient":  {"key": "patient_id",
                         "columns": {"patient_id": {"type": "TEXT"}, "cohort": {"type": "TEXT"}}},
            "specimen": {"key": "specimen_id", "parent": "patient", "parent_key": "patient_id",
                         "columns": {"specimen_id": {"type": "TEXT"}, "patient_id": {"type": "TEXT"},
                                     "tissue_site": {"type": "TEXT"}}},
            "assay":    {"key": "assay_id", "parent": "specimen", "parent_key": "specimen_id",
                         "columns": {"assay_id": {"type": "TEXT"}, "specimen_id": {"type": "TEXT"},
                                     "assay_type": {"type": "TEXT"}}},
        },
    }


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE patients(patient_id TEXT PRIMARY KEY, cohort TEXT);"
        "CREATE TABLE specimens(specimen_id TEXT PRIMARY KEY, patient_id TEXT, tissue_site TEXT);"
        "CREATE TABLE assays(assay_id TEXT PRIMARY KEY, specimen_id TEXT, assay_type TEXT);"
    )
    return conn


def test_upsert_inserts_new_rows():
    conn = _db()
    frame = pd.DataFrame({"patient_id": ["P1", "P2"], "cohort": ["c", "c"]})
    with casetrack.begin_immediate(conn):
        res = casetrack._upsert_level(conn, level="patient", frame=frame,
                                      schema=_schema(), allow_new=True, overwrite=False)
    assert res["inserted"] == 2 and res["updated"] == 0
    assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 2


def test_upsert_key_only_frame_inserts():
    """A key-only frame (no attribute columns) still inserts — register-cohort needs this."""
    conn = _db()
    frame = pd.DataFrame({"patient_id": ["P1"]})
    with casetrack.begin_immediate(conn):
        res = casetrack._upsert_level(conn, level="patient", frame=frame,
                                      schema=_schema(), allow_new=True, overwrite=False)
    assert res["inserted"] == 1
    assert conn.execute("SELECT patient_id FROM patients").fetchone()[0] == "P1"


def test_upsert_fill_only_vs_overwrite():
    conn = _db()
    conn.execute("INSERT INTO patients(patient_id, cohort) VALUES ('P1','old')")
    conn.commit()
    frame = pd.DataFrame({"patient_id": ["P1"], "cohort": ["new"]})
    with casetrack.begin_immediate(conn):
        casetrack._upsert_level(conn, level="patient", frame=frame, schema=_schema(),
                                allow_new=True, overwrite=False)  # fill-only: existing non-null kept
    assert conn.execute("SELECT cohort FROM patients").fetchone()[0] == "old"
    with casetrack.begin_immediate(conn):
        casetrack._upsert_level(conn, level="patient", frame=frame, schema=_schema(),
                                allow_new=True, overwrite=True)
    assert conn.execute("SELECT cohort FROM patients").fetchone()[0] == "new"


def test_upsert_missing_parent_raises_routing():
    conn = _db()
    frame = pd.DataFrame({"specimen_id": ["S1"], "patient_id": ["GHOST"], "tissue_site": ["t"]})
    with pytest.raises(casetrack._MetadataRouting):
        with casetrack.begin_immediate(conn):
            casetrack._upsert_level(conn, level="specimen", frame=frame, schema=_schema(),
                                    allow_new=True, overwrite=False)


def test_upsert_undeclared_column_raises():
    conn = _db()
    frame = pd.DataFrame({"patient_id": ["P1"], "bogus": ["x"]})
    with pytest.raises(ValueError):
        with casetrack.begin_immediate(conn):
            casetrack._upsert_level(conn, level="patient", frame=frame, schema=_schema(),
                                    allow_new=True, overwrite=False)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_upsert_level.py -q`
Expected: FAIL — `AttributeError: module 'casetrack' has no attribute '_upsert_level'`.

- [ ] **Step 3: Add `_upsert_level`** just above `cmd_add_metadata_project` (≈ line 6805)

```python
def _upsert_level(conn, *, level, frame, schema, allow_new, overwrite):
    """Validate `frame` against `level`'s schema and upsert it into the level table.

    Caller owns the transaction. Returns {'inserted': n, 'updated': m, 'skipped': 0,
    'meta_cols': [...], 'sql': [...]}. Raises _MetadataRouting (missing keys without
    allow_new, or missing parents), ValueError (undeclared column / malformed id),
    or sqlite3.IntegrityError. A frame with only the key column (no attribute/FK
    columns) inserts key-only rows — register-cohort relies on this (proposal 0012).
    """
    level_spec = schema["levels"][level]
    table = f"{level}s"
    key_col = level_spec["key"]
    parent_key_col = level_spec.get("parent_key")
    parent_level = level_spec.get("parent")

    if key_col not in frame.columns:
        raise ValueError(f"key column {key_col!r} not in sample sheet for level {level!r}")

    meta_cols = [c for c in frame.columns if c != key_col]
    declared_cols = set(level_spec["columns"])
    unknown = [c for c in meta_cols if c not in declared_cols]
    if unknown:
        raise ValueError(
            f"columns not declared in casetrack.toml under [levels.{level}.columns]: {unknown}")

    if allow_new and parent_key_col and parent_key_col not in frame.columns:
        raise ValueError(
            f"inserting new {level}(s) requires the parent FK column {parent_key_col!r}")

    executed_sql: list[str] = []
    n_updated = n_inserted = 0

    existing_keys = {
        str(r[0]) for r in conn.execute(
            f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)}").fetchall()
    }
    tsv_keys = frame[key_col].astype(str).tolist()
    missing_keys = set(tsv_keys) - existing_keys

    parents_needed: set = set()
    missing_parents: set = set()
    if missing_keys and allow_new and parent_level:
        new_rows = frame[frame[key_col].astype(str).isin(missing_keys)]
        parents_needed = set(new_rows[parent_key_col].astype(str))
        parent_table = f"{parent_level}s"
        parents_have = {
            str(r[0]) for r in conn.execute(
                f"SELECT {_quote_ident(parent_key_col)} FROM {_quote_ident(parent_table)}").fetchall()
        }
        missing_parents = parents_needed - parents_have

    if missing_keys and not allow_new:
        raise _MetadataRouting(missing_keys, set())
    if missing_parents:
        raise _MetadataRouting(set(), missing_parents)

    # UPDATE existing keys (fill-only unless overwrite). No-op when meta_cols empty.
    update_keys = [k for k in tsv_keys if k in existing_keys]
    if update_keys and meta_cols:
        if overwrite:
            set_clauses = ", ".join(f"{_quote_ident(c)} = ?" for c in meta_cols)
        else:
            set_clauses = ", ".join(
                f"{_quote_ident(c)} = COALESCE({_quote_ident(c)}, ?)" for c in meta_cols)
        update_sql = (f"UPDATE {_quote_ident(table)} SET {set_clauses} "
                      f"WHERE {_quote_ident(key_col)} = ?")
        executed_sql.append(update_sql)
        idx_by_key = {str(frame.iloc[i][key_col]): i for i in range(len(frame))}
        for k in update_keys:
            row = frame.iloc[idx_by_key[k]]
            conn.execute(update_sql, tuple(_coerce_for_sqlite(row[c]) for c in meta_cols) + (k,))
            n_updated += 1

    # INSERT new keys.
    if allow_new and missing_keys:
        for new_key in missing_keys:
            validate_hierarchy_id(new_key, schema, level)
        if parent_level:
            for parent_id in parents_needed:
                validate_hierarchy_id(parent_id, schema, parent_level)
        folded_existing = _preload_folded_ids(conn, schema, level)
        for new_key in missing_keys:
            check_id_case_unique(conn, schema, level, new_key, folded_existing)
        new_rows = frame[frame[key_col].astype(str).isin(missing_keys)]
        insert_cols = [key_col] + meta_cols
        quoted = ", ".join(_quote_ident(c) for c in insert_cols)
        placeholders = ", ".join("?" * len(insert_cols))
        insert_sql = f"INSERT INTO {_quote_ident(table)} ({quoted}) VALUES ({placeholders})"
        executed_sql.append(insert_sql)
        for _, row in new_rows.iterrows():
            conn.execute(insert_sql, tuple(_coerce_for_sqlite(row[c]) for c in insert_cols))
            n_inserted += 1

    return {"inserted": n_inserted, "updated": n_updated, "skipped": 0,
            "meta_cols": meta_cols, "sql": executed_sql}
```

- [ ] **Step 4: Rewrite `cmd_add_metadata_project` to call `_upsert_level`** — keep its arg checks, the `--allow-new`-requires-`--yes` gate, the `metadata` TSV read, the **"no columns besides key" guard** (this stays here, not in the engine), the parent-FK-present check for `--allow-new`, the exception→message mapping, and provenance. Replace the inline `with begin_immediate(...)` validate/update/insert block with:

```python
    metadata = pd.read_csv(metadata_path, sep="\t")
    # add-metadata-specific: a metadata sheet must carry at least one column
    # besides the key (register-cohort allows key-only frames; this command does not).
    if metadata.shape[1] <= 1:
        print(f"Error: {metadata_path} has no columns besides the key.", file=sys.stderr)
        sys.exit(1)
    conn = open_project_db(project_dir / PROJECT_DB_NAME)
    result = {"inserted": 0, "updated": 0, "meta_cols": [], "sql": []}
    try:
        with begin_immediate(conn):
            result = _upsert_level(conn, level=level, frame=metadata, schema=schema,
                                   allow_new=args.allow_new, overwrite=args.overwrite)
    except _MetadataRouting as e:
        conn.close()
        # ... keep the EXISTING _MetadataRouting message block verbatim (missing keys
        #     vs missing parents), using `table`, `parent_level`, `metadata_path` ...
        sys.exit(2)
    except ValueError as e:
        conn.close(); print(f"Error: {e}", file=sys.stderr); sys.exit(1)
    except sqlite3.IntegrityError as e:
        conn.close(); print(f"Error: add-metadata aborted — {type(e).__name__}: {e}", file=sys.stderr); sys.exit(1)
    finally:
        if conn:
            conn.close()
    # provenance: same dict as before, but read counts from `result`
    log_project_provenance(project_dir, { ... "rows_updated": result["updated"],
                                          "rows_inserted": result["inserted"],
                                          "columns": result["meta_cols"], "sql": result["sql"], ... })
    print(f"add-metadata → {level}: updated={result['updated']}, "
          f"inserted={result['inserted']}, columns={len(result['meta_cols'])}.")
```

Preserve the early-out arg validation (level check, overwrite/fill-only mutual exclusion, allow-new/yes gate, file-exists, key-col-present) exactly as it was — only the in-transaction body moves into `_upsert_level`. The `table` / `parent_level` / `metadata_path` names used by the `_MetadataRouting` message must still be in scope (they are: `table = f"{level}s"`, `parent_level = level_spec.get("parent")` — keep those local assignments).

- [ ] **Step 5: Run both test sets**

Run: `python3 -m pytest tests/test_upsert_level.py tests/test_add_metadata.py -q`
Expected: PASS — the 5 new unit tests AND every existing add-metadata test (behavior-preserving refactor).

- [ ] **Step 6: Commit**

```bash
git add casetrack.py tests/test_upsert_level.py
git commit -m "refactor(cohort): extract shared _upsert_level engine from add-metadata (0012 §6.6)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Column routing + explode (pure functions)

**Files:**
- Modify: `casetrack.py` (add `_route_samplesheet_columns` + `_explode_samplesheet` near `_upsert_level`)
- Test: `tests/test_register_cohort.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_register_cohort.py
"""Unit + CLI tests for register-cohort (proposal 0012)."""
import argparse, subprocess, sys
import pandas as pd
import pytest
import casetrack

SCHEMA = {
    "project": {"schema_v": 1},
    "levels": {
        "patient":  {"key": "patient_id",
                     "columns": {"patient_id": {"type": "TEXT"}, "cohort": {"type": "TEXT"}}},
        "specimen": {"key": "specimen_id", "parent": "patient", "parent_key": "patient_id",
                     "columns": {"specimen_id": {"type": "TEXT"}, "patient_id": {"type": "TEXT"},
                                 "tissue_site": {"type": "TEXT"}}},
        "assay":    {"key": "assay_id", "parent": "specimen", "parent_key": "specimen_id",
                     "columns": {"assay_id": {"type": "TEXT"}, "specimen_id": {"type": "TEXT"},
                                 "assay_type": {"type": "TEXT"}}},
    },
}


def test_route_columns_by_level():
    routed = casetrack._route_samplesheet_columns(
        ["patient_id", "cohort", "specimen_id", "tissue_site", "assay_id", "assay_type"], SCHEMA)
    assert routed["patient"] == ["patient_id", "cohort"]
    assert routed["specimen"] == ["specimen_id", "patient_id", "tissue_site"]
    assert routed["assay"] == ["assay_id", "specimen_id", "assay_type"]


def test_route_columns_undeclared_raises():
    with pytest.raises(ValueError):
        casetrack._route_samplesheet_columns(["patient_id", "bogus"], SCHEMA)


def test_explode_dedups_parents():
    df = pd.DataFrame({
        "patient_id": ["P1", "P1", "P2"],
        "cohort": ["c", "c", "c"],
        "specimen_id": ["P1_T", "P1_N", "P2_T"],
        "patient_id_fk_unused": ["", "", ""],  # ignored; FK comes from patient_id col
        "tissue_site": ["tumor", "normal", "tumor"],
        "assay_id": ["P1_T_A", "P1_N_A", "P2_T_A"],
        "assay_type": ["ONT", "ONT", "ONT"],
    }).drop(columns=["patient_id_fk_unused"])
    frames = casetrack._explode_samplesheet(df, SCHEMA)
    assert len(frames["patient"]) == 2     # P1, P2
    assert len(frames["specimen"]) == 3
    assert len(frames["assay"]) == 3
    assert set(frames["specimen"].columns) == {"specimen_id", "patient_id", "tissue_site"}
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_register_cohort.py -q`
Expected: FAIL — `_route_samplesheet_columns` not defined.

- [ ] **Step 3: Implement** (near `_upsert_level`)

```python
def _route_samplesheet_columns(columns, schema):
    """Map each sample-sheet column to the level(s) whose frame it belongs in.

    Key columns (patient_id/specimen_id/assay_id) and parent-FK columns route to
    every frame that needs them; a non-key attribute routes to the single level
    that declares it. Returns {level: [cols in sheet order]} for the 3 levels.
    Raises ValueError on a column declared at no level (undeclared) or a non-key
    attribute declared at >1 level (ambiguous).
    """
    levels = schema["levels"]
    keys = {levels[lvl]["key"] for lvl in LEVEL_ORDER}                      # PKs
    parent_keys = {levels[lvl].get("parent_key") for lvl in LEVEL_ORDER} - {None}
    cross_level = keys | parent_keys                                       # routed positionally

    # which level declares each non-key attribute (must be exactly one)
    attr_owner: dict = {}
    for lvl in LEVEL_ORDER:
        for col in levels[lvl]["columns"]:
            if col in cross_level:
                continue
            if col in attr_owner:
                raise ValueError(f"column {col!r} declared at both "
                                 f"{attr_owner[col]!r} and {lvl!r} levels (ambiguous)")
            attr_owner[col] = lvl

    routed = {lvl: [] for lvl in LEVEL_ORDER}
    for col in columns:
        if col in cross_level:
            # add the key/FK to whichever level(s) declare it
            for lvl in LEVEL_ORDER:
                if col in levels[lvl]["columns"]:
                    routed[lvl].append(col)
        elif col in attr_owner:
            routed[attr_owner[col]].append(col)
        else:
            raise ValueError(
                f"column {col!r} is not declared at any level in casetrack.toml; "
                f"declare it (and run `casetrack schema apply`) first")
    return routed


def _explode_samplesheet(df, schema):
    """Split a wide sample-sheet DataFrame into per-level frames, deduped by key.

    Returns {level: DataFrame}. Each frame keeps only that level's routed columns
    and is deduplicated on the level's key column (identical duplicate rows collapse).
    """
    routed = _route_samplesheet_columns(list(df.columns), schema)
    frames = {}
    for lvl in LEVEL_ORDER:
        key_col = schema["levels"][lvl]["key"]
        sub = df[routed[lvl]].drop_duplicates(subset=[key_col]).reset_index(drop=True)
        frames[lvl] = sub
    return frames
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_register_cohort.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add casetrack.py tests/test_register_cohort.py
git commit -m "feat(cohort): schema-driven column routing + explode for register-cohort (0012 §6.2-6.3)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Pre-write sample-sheet validation

**Files:**
- Modify: `casetrack.py` (add `_validate_samplesheet`)
- Test: `tests/test_register_cohort.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_register_cohort.py
def _full_sheet():
    return pd.DataFrame({
        "patient_id": ["P1", "P2"], "cohort": ["c", "c"],
        "specimen_id": ["P1_T", "P2_T"], "tissue_site": ["tumor", "tumor"],
        "assay_id": ["P1_T_A", "P2_T_A"], "assay_type": ["ONT", "ONT"],
    })


def test_validate_ok():
    casetrack._validate_samplesheet(_full_sheet(), SCHEMA)  # no raise


def test_validate_missing_required_column():
    df = _full_sheet().drop(columns=["assay_type"])  # required attr missing
    with pytest.raises(ValueError, match="assay_type"):
        casetrack._validate_samplesheet(df, SCHEMA)


def test_validate_blank_key_breaks_chain():
    df = _full_sheet(); df.loc[0, "assay_id"] = ""
    with pytest.raises(ValueError, match="chain|empty|assay_id"):
        casetrack._validate_samplesheet(df, SCHEMA)


def test_validate_specimen_two_patients():
    df = _full_sheet(); df.loc[1, "specimen_id"] = "P1_T"; df.loc[1, "patient_id"] = "P2"
    with pytest.raises(ValueError, match="specimen|parent"):
        casetrack._validate_samplesheet(df, SCHEMA)


def test_validate_duplicate_assay():
    df = pd.concat([_full_sheet(), _full_sheet().iloc[[0]].assign(assay_type="WGS")])
    with pytest.raises(ValueError, match="assay_id|duplicate"):
        casetrack._validate_samplesheet(df, SCHEMA)


def test_validate_conflicting_attribute():
    df = _full_sheet(); df.loc[1, "patient_id"] = "P1"; df.loc[1, "cohort"] = "other"
    with pytest.raises(ValueError, match="conflict|cohort"):
        casetrack._validate_samplesheet(df, SCHEMA)
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_register_cohort.py -k validate -q`
Expected: FAIL — `_validate_samplesheet` not defined.

- [ ] **Step 3: Implement**

```python
def _validate_samplesheet(df, schema):
    """Pre-write integrity checks for a register-cohort sample sheet (0012 §6.4).

    Raises ValueError (with a human message) on the first violation: a missing
    required column, a row with any blank key or required attr (full-chain), a
    specimen mapped to >1 patient, a duplicate assay_id, or an entity key with
    conflicting attribute values across rows. Routing/undeclared-column errors
    are surfaced by _route_samplesheet_columns (called first).
    """
    routed = _route_samplesheet_columns(list(df.columns), schema)  # undeclared/ambiguous → ValueError
    levels = schema["levels"]

    # required columns present in the header
    for lvl in LEVEL_ORDER:
        for col, spec in levels[lvl]["columns"].items():
            if spec.get("required") and col not in df.columns:
                raise ValueError(f"required column {col!r} (level {lvl}) missing from sample sheet")

    # full chain: every row has all three keys + every required attr non-empty
    def _blank(v):
        return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""
    required_cols = [c for lvl in LEVEL_ORDER for c, s in levels[lvl]["columns"].items()
                     if (s.get("required") or c == levels[lvl]["key"]) and c in df.columns]
    for i in range(len(df)):
        for col in required_cols:
            if _blank(df.iloc[i][col]):
                raise ValueError(f"row {i}: empty {col!r} breaks the patient→specimen→assay chain")

    # specimen → exactly one patient
    sp = df[["specimen_id", "patient_id"]].astype(str).drop_duplicates()
    dup_sp = sp[sp.duplicated(subset=["specimen_id"], keep=False)]
    if not dup_sp.empty:
        bad = sorted(dup_sp["specimen_id"].unique())[:3]
        raise ValueError(f"specimen(s) mapped to >1 patient (one parent only): {bad}")

    # duplicate assay_id (non-identical rows sharing a key)
    if df["assay_id"].astype(str).duplicated().any():
        # allow fully-identical duplicate rows; flag only conflicting ones
        assay_cols = routed["assay"]
        confl = df[assay_cols].astype(str).drop_duplicates()
        if confl["assay_id"].duplicated().any():
            bad = sorted(confl[confl["assay_id"].duplicated(keep=False)]["assay_id"].unique())[:3]
            raise ValueError(f"duplicate assay_id with differing values: {bad}")

    # conflicting attributes for the same entity key (patient, specimen)
    for lvl in ("patient", "specimen"):
        key_col = levels[lvl]["key"]
        cols = routed[lvl]
        distinct = df[cols].astype(str).drop_duplicates()
        if distinct[key_col].duplicated().any():
            bad = sorted(distinct[distinct[key_col].duplicated(keep=False)][key_col].unique())[:3]
            raise ValueError(f"{lvl} key(s) with conflicting attribute values across rows: {bad}")
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_register_cohort.py -k validate -q`
Expected: PASS (6 validation tests).

- [ ] **Step 5: Commit**

```bash
git add casetrack.py tests/test_register_cohort.py
git commit -m "feat(cohort): pre-write sample-sheet integrity validation (0012 §6.4)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: `cmd_register_cohort` command (transaction, dry-run, provenance)

**Files:**
- Modify: `casetrack.py` (add `cmd_register_cohort` after `cmd_add_metadata_project`)
- Test: `tests/test_register_cohort.py` (extend — CLI-level via subprocess, needs Task 5 wiring to run end-to-end; the function itself is tested directly here)

- [ ] **Step 1: Write the failing test (direct call + dry-run)**

```python
# append to tests/test_register_cohort.py
def _init_project(tmp_path):
    proj = tmp_path / "proj"
    ns = argparse.Namespace(manifest=None, project_dir=str(proj), samples=None,
                            key="sample_id", metadata=None, cols=None,
                            from_template="hgsoc", project_name="test", force=False)
    casetrack.cmd_init(ns)
    return proj


def _write_sheet(path):
    path.write_text(
        "patient_id\ttissue_site\tspecimen_id\tassay_type\tassay_id\n"
        "P1\ttumor\tP1_T\tONT\tP1_T_ONT\n"
        "P1\tnormal\tP1_N\tONT\tP1_N_ONT\n"
        "P2\ttumor\tP2_T\tONT\tP2_T_ONT\n"
    )


def _ns(proj, sheet, **kw):
    base = dict(project_dir=str(proj), project=None, samplesheet=str(sheet),
                overwrite=False, dry_run=False, force_archived=False, yes=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_register_cohort_loads_all_levels(tmp_path):
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"; _write_sheet(sheet)
    casetrack.cmd_register_cohort(_ns(proj, sheet))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM specimens").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM assays").fetchone()[0] == 3
    conn.close()


def test_register_cohort_dry_run_writes_nothing(tmp_path):
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"; _write_sheet(sheet)
    casetrack.cmd_register_cohort(_ns(proj, sheet, dry_run=True))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
    conn.close()


def test_register_cohort_rerun_idempotent(tmp_path):
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"; _write_sheet(sheet)
    casetrack.cmd_register_cohort(_ns(proj, sheet))
    casetrack.cmd_register_cohort(_ns(proj, sheet))  # second run inserts 0
    conn = casetrack.open_project_db(proj / "casetrack.db")
    assert conn.execute("SELECT COUNT(*) FROM assays").fetchone()[0] == 3
    conn.close()


def test_register_cohort_rolls_back_on_bad_chain(tmp_path):
    proj = _init_project(tmp_path)
    sheet = tmp_path / "bad.tsv"
    sheet.write_text(
        "patient_id\ttissue_site\tspecimen_id\tassay_type\tassay_id\n"
        "P1\ttumor\tP1_T\tONT\t\n"  # blank assay_id → validation error, nothing written
    )
    with pytest.raises(SystemExit):
        casetrack.cmd_register_cohort(_ns(proj, sheet))
    conn = casetrack.open_project_db(proj / "casetrack.db")
    assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
    conn.close()
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_register_cohort.py -k "loads_all or dry_run or rerun or rolls_back" -q`
Expected: FAIL — `cmd_register_cohort` not defined.

- [ ] **Step 3: Implement** `cmd_register_cohort`

```python
def cmd_register_cohort(args):
    """Explode one schema-native wide sample sheet into the three normalized
    tables in a single transaction (proposal 0012)."""
    project_dir, schema = _resolve_project(args.project_dir, project_id=getattr(args, "project", None))
    from casetrack_lifecycle.gate import assert_not_archived as _assert_not_archived
    _assert_not_archived(project_dir, force_archived=getattr(args, "force_archived", False),
                         yes=getattr(args, "yes", False))

    sheet_path = Path(args.samplesheet)
    if not sheet_path.exists():
        print(f"Error: sample sheet not found: {sheet_path}", file=sys.stderr); sys.exit(1)
    df = pd.read_csv(sheet_path, sep="\t", dtype=str).fillna("")

    # validation (pre-write) — clean exit 2, never a traceback
    try:
        _validate_samplesheet(df, schema)
        frames = _explode_samplesheet(df, schema)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr); sys.exit(2)

    if getattr(args, "dry_run", False):
        conn = open_project_db(project_dir / PROJECT_DB_NAME)
        try:
            print("[dry-run] register-cohort would load:")
            for lvl in LEVEL_ORDER:
                key_col = schema["levels"][lvl]["key"]
                table = f"{lvl}s"
                existing = {str(r[0]) for r in conn.execute(
                    f"SELECT {_quote_ident(key_col)} FROM {_quote_ident(table)}").fetchall()}
                keys = set(frames[lvl][key_col].astype(str))
                new = len(keys - existing)
                print(f"  {lvl:8s}: {new} new, {len(keys) - new} existing")
        finally:
            conn.close()
        return

    txn_id = _new_transaction_id()
    conn = open_project_db(project_dir / PROJECT_DB_NAME)
    counts: dict = {}
    try:
        with begin_immediate(conn):
            for lvl in LEVEL_ORDER:  # FK order: patient → specimen → assay
                counts[lvl] = _upsert_level(conn, level=lvl, frame=frames[lvl], schema=schema,
                                            allow_new=True, overwrite=getattr(args, "overwrite", False))
    except _MetadataRouting as e:
        conn.close()
        print(f"Error: register-cohort aborted — unresolved parents/keys: "
              f"{sorted(e.missing_keys | e.missing_parents)[:5]}", file=sys.stderr); sys.exit(2)
    except ValueError as e:
        conn.close(); print(f"Error: {e}", file=sys.stderr); sys.exit(2)
    except sqlite3.IntegrityError as e:
        conn.close(); print(f"Error: register-cohort aborted — {type(e).__name__}: {e}",
                            file=sys.stderr); sys.exit(1)
    finally:
        if conn:
            conn.close()

    log_project_provenance(project_dir, {
        "action": "register_cohort",
        "samplesheet": str(sheet_path),
        "samplesheet_checksum": _checksum(str(sheet_path)),
        "counts": {lvl: {"inserted": counts[lvl]["inserted"], "updated": counts[lvl]["updated"]}
                   for lvl in LEVEL_ORDER},
        "overwrite": bool(getattr(args, "overwrite", False)),
        "transaction_id": txn_id,
    })
    print("register-cohort: " + ", ".join(
        f"{lvl}s +{counts[lvl]['inserted']} (~{counts[lvl]['updated']})" for lvl in LEVEL_ORDER))
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_register_cohort.py -q`
Expected: PASS (all unit + the 4 command tests).

- [ ] **Step 5: Commit**

```bash
git add casetrack.py tests/test_register_cohort.py
git commit -m "feat(cohort): cmd_register_cohort — explode + transactional load + dry-run (0012 §6.5)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: argparse + dispatch wiring

**Files:**
- Modify: `casetrack.py` (subparser near the `add-metadata` parser; dispatch dict at ~8616)
- Test: `tests/test_register_cohort.py` (extend — subprocess end-to-end)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_register_cohort.py
def _run(args):
    return subprocess.run([sys.executable, "-m", "casetrack", *args], capture_output=True, text=True)


def test_register_cohort_cli_end_to_end(tmp_path):
    proj = _init_project(tmp_path)
    sheet = tmp_path / "cohort.tsv"; _write_sheet(sheet)
    r = _run(["register-cohort", "--project-dir", str(proj), "--samplesheet", str(sheet)])
    assert r.returncode == 0, r.stderr
    assert "register-cohort:" in r.stdout
    # dry-run via CLI
    r2 = _run(["register-cohort", "--project-dir", str(proj), "--samplesheet", str(sheet), "--dry-run"])
    assert r2.returncode == 0 and "[dry-run]" in r2.stdout


def test_register_cohort_cli_validation_exit2(tmp_path):
    proj = _init_project(tmp_path)
    bad = tmp_path / "bad.tsv"
    bad.write_text("patient_id\tbogus_col\nP1\tx\n")  # undeclared column + missing levels
    r = _run(["register-cohort", "--project-dir", str(proj), "--samplesheet", str(bad)])
    assert r.returncode == 2
    assert "Error" in r.stderr and "Traceback" not in r.stderr
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_register_cohort.py -k cli -q`
Expected: FAIL — `register-cohort` is not a known command.

- [ ] **Step 3: Register the subparser** (immediately after the `add-metadata` subparser block, ~`casetrack.py:8311`+)

```python
    p_regcohort = subparsers.add_parser(
        "register-cohort",
        help="[v0.10] Bulk-register patients+specimens+assays from one wide sample sheet",
    )
    g_rc = p_regcohort.add_mutually_exclusive_group(required=True)
    g_rc.add_argument("--project-dir", help="Casetrack project directory")
    g_rc.add_argument("--project", help="Registered project id (registry lookup)")
    p_regcohort.add_argument("--samplesheet", required=True,
                             help="Wide TSV: one row per assay, columns named to match the schema "
                                  "(patient_id, specimen_id, assay_id + declared level columns)")
    p_regcohort.add_argument("--overwrite", action="store_true",
                             help="Replace existing non-null attribute cells (default: fill-only)")
    p_regcohort.add_argument("--dry-run", dest="dry_run", action="store_true",
                             help="Print the per-level new/existing plan; write nothing")
    p_regcohort.add_argument("--force-archived", action="store_true",
                             help="[v0.7] Allow on an archived project (requires --yes)")
    p_regcohort.add_argument("--yes", action="store_true", help="Confirm --force-archived")
```

- [ ] **Step 4: Add to the dispatch dict** (`casetrack.py:8616`, inside `commands = {...}` after `"register": cmd_register,`)

```python
        "register-cohort": cmd_register_cohort,
```

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m pytest tests/test_register_cohort.py -q`
Expected: PASS (all, incl. the 2 CLI tests).

- [ ] **Step 6: Commit**

```bash
git add casetrack.py tests/test_register_cohort.py
git commit -m "feat(cohort): wire register-cohort subparser + dispatch (0012 §6.1)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Docs + version bump to v0.10.0

**Files:**
- Modify: `setup.py` (`version`), `casetrack.py` (`_CASETRACK_VERSION`), `CHANGELOG.md`, `README.md`, `CLAUDE.md`, `docs/proposals/0012-register-cohort.md` (Status), the casetrack skill if it documents registration.

- [ ] **Step 1: Bump version (grep first)**

Run: `grep -rn "0\.9\.0" setup.py casetrack.py` — set both authoritative declarations (`setup.py` `version=`, `casetrack.py` `_CASETRACK_VERSION`) to `0.10.0`. (Per the project's "version lives in multiple places" history.) Do NOT touch historical CHANGELOG entries.

- [ ] **Step 2: CHANGELOG** — add `## [0.10.0] — 2026-05-21` summarizing: new `register-cohort` command (one wide sample sheet → patients+specimens+assays in one transaction); schema-driven column routing; create-by-default + `--dry-run`; pre-write intra-sheet validation; shared `_upsert_level` engine extracted from `add-metadata` (no behavior change).

- [ ] **Step 3: README + CLAUDE.md** — add `register-cohort` to the command tables; add a short "Registering a whole cohort from one sheet" subsection to the README project-mode quick start showing the wide-sheet + `register-cohort` invocation; update CLAUDE.md current-release line to v0.10.0 and add a one-line `register-cohort` entry to the Commands table + a note that it's the successor to `migrate` post-v1.0.

- [ ] **Step 4: Proposal Status** — `docs/proposals/0012-register-cohort.md`: `accepted (design)` → `accepted (implemented) — 2026-05-21`.

- [ ] **Step 5: Verify + commit**

Run: `python3 -m pytest tests/test_register_cohort.py tests/test_add_metadata.py -q` (sanity) and `grep -n version setup.py` shows 0.10.0.
```bash
git add -A
git commit -m "docs(cohort): v0.10.0 — document register-cohort + bump version

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Full verification + PR

- [ ] **Step 1: Full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS — prior 943 + the new register-cohort/upsert-level tests; confirm count went up and nothing regressed (esp. `tests/test_add_metadata.py` — the refactor's regression guard).

- [ ] **Step 2: Targeted refactor-safety re-run**

Run: `python3 -m pytest tests/test_add_metadata.py tests/test_upsert_level.py -v`
Expected: PASS — confirms `_upsert_level` extraction preserved `add-metadata` behavior.

- [ ] **Step 3: Push + PR**

```bash
git push -u origin feature/register-cohort-0012
gh pr create --title "feat: register-cohort one-shot cohort loader (proposal 0012, v0.10.0)" --body "$(cat <<'EOF'
Implements proposal 0012 — `register-cohort`: load patients+specimens+assays from one
schema-native wide sample sheet (one row per assay, full chain) in a single transaction.
Schema-driven column routing, create-by-default with --dry-run, pre-write intra-sheet
integrity checks, and a shared `_upsert_level` engine extracted from `add-metadata`.

See docs/proposals/0012-register-cohort.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Watch CI** — `gh pr checks --watch`; confirm the 3.10–3.13 matrix passes before declaring done.

---

## Self-review notes (for the executor)

- **Task 1 is the risk task** — the `_upsert_level` extraction touches the shipped `add-metadata` path. The `tests/test_add_metadata.py` suite is the regression guard; it MUST stay green with zero changes. The one intentional behavior split: the "no columns besides key" guard stays in `cmd_add_metadata_project` (register-cohort needs key-only frames). Do not move it into the engine.
- **`dtype=str).fillna("")`** on the sheet read (Task 4) keeps keys as strings and makes blank cells empty-string (so the full-chain `_blank` check fires correctly and `_coerce_for_sqlite` handles them).
- **Routing of FK columns**: `patient_id` appears in both the patient frame (as PK) and specimen frame (as FK) — `_route_samplesheet_columns` adds it to every level that declares it. Confirm `[levels.specimen.columns]` actually declares `patient_id` (the blank/hgsoc templates do — verified this session).
- **Match-the-codebase**: in Task 1 Step 4, preserve the EXISTING `_MetadataRouting` message text and the provenance dict keys verbatim — only the counts source changes. Grep the original block before editing.
- Backward-compat: `register-cohort` is project-mode-only (no flat dispatcher) — it dispatches straight to `cmd_register_cohort`, unlike `add-metadata`/`init` which go through a flat/project dispatcher.
