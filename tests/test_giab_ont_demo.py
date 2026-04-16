"""Tests for the giab_ont template + the mock demo pieces in examples/giab_chr21/.

Keeps the demo exercised by CI so nothing bit-rots silently.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-16
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import casetrack


REPO_ROOT = Path(casetrack.__file__).parent
DEMO_DIR = REPO_ROOT / "examples" / "giab_chr21"


# ── giab_ont template ─────────────────────────────────────────────────────────


def test_giab_ont_template_parses(tmp_path: Path):
    toml_path = tmp_path / "schema.toml"
    toml_path.write_text(casetrack.TEMPLATES["giab_ont"]("giab"))
    schema = casetrack.load_schema(toml_path)
    assert set(schema["levels"]) == set(casetrack.LEVEL_ORDER)

    # patient level has trio_role enum.
    patient_cols = schema["levels"]["patient"]["columns"]
    assert "trio_role" in patient_cols
    assert "proband" in patient_cols["trio_role"]["enum"]

    # assay enum contains ONT-specific values.
    assay_cols = schema["levels"]["assay"]["columns"]
    assert assay_cols["assay_type"]["enum"] == [
        "ONT_WGS", "ONT_target", "ONT_cDNA", "ONT_direct_RNA"
    ]
    assert "chemistry" in assay_cols
    assert "bam_path" in assay_cols


def test_giab_ont_template_via_init(tmp_path: Path):
    """init --from-template giab_ont creates a DB with the expected tables."""
    proj = tmp_path / "proj"
    casetrack.cmd_init(argparse.Namespace(
        manifest=None, project_dir=str(proj), samples=None, key="sample_id",
        metadata=None, cols=None, from_template="giab_ont",
        project_name=None, force=False,
    ))
    import sqlite3
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        # assays table has bam_path
        cols = {r[1] for r in conn.execute("PRAGMA table_info(assays)").fetchall()}
    finally:
        conn.close()
    assert names == {"patients", "specimens", "assays"}
    assert "bam_path" in cols
    assert "chemistry" in cols


# ── bootstrap.py ──────────────────────────────────────────────────────────────


def _fake_sheet(path: Path) -> Path:
    """A sample sheet matching the real giab_chr21 format — 2 patients × 2 assays."""
    pd.DataFrame([
        {"patient_id": "HG002", "sample_id": "F1",
         "condition": "reference", "assay_type": "ONT_WGS",
         "bam_path": "/tmp/nonexistent/F1.bam"},
        {"patient_id": "HG002", "sample_id": "F2",
         "condition": "reference", "assay_type": "ONT_WGS",
         "bam_path": "/tmp/nonexistent/F2.bam"},
        {"patient_id": "HG006", "sample_id": "F3",
         "condition": "reference", "assay_type": "ONT_WGS",
         "bam_path": "/tmp/nonexistent/F3.bam"},
        {"patient_id": "HG006", "sample_id": "F4",
         "condition": "reference", "assay_type": "ONT_WGS",
         "bam_path": "/tmp/nonexistent/F4.bam"},
    ]).to_csv(path, sep="\t", index=False)
    return path


def test_bootstrap_registers_patients_specimens_assays(tmp_path: Path):
    sheet = _fake_sheet(tmp_path / "sheet.tsv")
    proj = tmp_path / "proj"
    res = subprocess.run(
        [sys.executable, str(DEMO_DIR / "bootstrap.py"),
         "--sample-sheet", str(sheet), "--project-dir", str(proj)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, f"stderr:\n{res.stderr}"
    assert "Registered 2 patient(s), 2 specimen(s), 4 assay(s)" in res.stdout

    import sqlite3
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        patients = [r[0] for r in conn.execute(
            "SELECT patient_id FROM patients ORDER BY patient_id"
        ).fetchall()]
        assays = dict(conn.execute(
            "SELECT assay_id, bam_path FROM assays"
        ).fetchall())
    finally:
        conn.close()
    assert patients == ["HG002", "HG006"]
    assert set(assays) == {"F1", "F2", "F3", "F4"}
    assert assays["F1"].endswith("F1.bam")


def test_bootstrap_is_idempotent(tmp_path: Path):
    """Re-running bootstrap against the same project is a no-op."""
    sheet = _fake_sheet(tmp_path / "sheet.tsv")
    proj = tmp_path / "proj"

    for _ in range(2):
        res = subprocess.run(
            [sys.executable, str(DEMO_DIR / "bootstrap.py"),
             "--sample-sheet", str(sheet), "--project-dir", str(proj)],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, f"stderr:\n{res.stderr}"

    import sqlite3
    conn = sqlite3.connect(str(proj / "casetrack.db"))
    try:
        (n_assays,) = conn.execute("SELECT COUNT(*) FROM assays").fetchone()
    finally:
        conn.close()
    assert n_assays == 4  # no duplicates from the second run


# ── mock summarizers ──────────────────────────────────────────────────────────


def test_mock_modkit_summary_is_deterministic(tmp_path: Path):
    out1 = tmp_path / "m1.tsv"
    out2 = tmp_path / "m2.tsv"
    for path in (out1, out2):
        subprocess.run(
            [sys.executable,
             str(DEMO_DIR / "scripts" / "mock_modkit_summary.py"),
             "--assay-ids", "F1,F2",
             "--output", str(path)],
            check=True,
        )
    assert out1.read_text() == out2.read_text()

    df = pd.read_csv(out1, sep="\t")
    assert list(df.columns) == ["assay_id", "n_cpg_sites", "mean_meth",
                                "median_meth", "pct_high_conf"]
    assert len(df) == 2
    # Mean methylation is a valid fraction.
    assert df["mean_meth"].between(0, 1).all()


def test_mock_sniffles_summary_is_deterministic(tmp_path: Path):
    out = tmp_path / "s.tsv"
    subprocess.run(
        [sys.executable,
         str(DEMO_DIR / "scripts" / "mock_sniffles_summary.py"),
         "--assay-ids", "F1,F2,F3",
         "--output", str(out)],
        check=True,
    )
    df = pd.read_csv(out, sep="\t")
    assert len(df) == 3
    # Subtype counts sum to total.
    for _, row in df.iterrows():
        parts = row["n_ins"] + row["n_del"] + row["n_inv"] + row["n_bnd"]
        assert parts == row["n_svs_total"], row.to_dict()


# ── summarize_flagstat.py (real parser, canned input) ─────────────────────────


FLAGSTAT_FIXTURE = """\
2000000 + 0 in total (QC-passed reads + QC-failed reads)
100 + 0 secondary
80000 + 0 supplementary
10000 + 0 duplicates
1950000 + 0 mapped (97.50% : N/A)
0 + 0 paired in sequencing
0 + 0 read1
0 + 0 read2
5 + 0 properly paired (0.00% : N/A)
0 + 0 with itself and mate mapped
0 + 0 singletons (0.00% : N/A)
0 + 0 with mate mapped to a different chr
0 + 0 with mate mapped to a different chr (mapQ>=5)
"""


def test_summarize_flagstat_parses_canonical_output(tmp_path: Path):
    fs = tmp_path / "sample.flagstat"
    fs.write_text(FLAGSTAT_FIXTURE)

    out = tmp_path / "out.tsv"
    subprocess.run(
        [sys.executable,
         str(DEMO_DIR / "scripts" / "summarize_flagstat.py"),
         "--assay-id", "A1",
         "--input", str(fs),
         "--output", str(out)],
        check=True,
    )
    df = pd.read_csv(out, sep="\t")
    assert len(df) == 1
    row = df.iloc[0]
    assert row["assay_id"] == "A1"
    assert row["total_reads"] == 2_000_000
    assert row["mapped_reads"] == 1_950_000
    assert row["mapped_pct"] == pytest.approx(97.50)
    assert row["properly_paired_reads"] == 5
    assert row["duplicates_reads"] == 10000
    assert row["supplementary_reads"] == 80000


def test_summarize_flagstat_empty_input_errors(tmp_path: Path):
    fs = tmp_path / "empty.flagstat"
    fs.write_text("")
    out = tmp_path / "out.tsv"
    res = subprocess.run(
        [sys.executable,
         str(DEMO_DIR / "scripts" / "summarize_flagstat.py"),
         "--assay-id", "A1", "--input", str(fs), "--output", str(out)],
        capture_output=True, text=True,
    )
    assert res.returncode != 0


# ── summarize_modkit.py (real bedMethyl parser, canned input) ─────────────────


BEDMETHYL_FIXTURE = """\
#chrom\tstart\tend\tmod_code\tscore\tstrand\tstart\tend\tcolor\tN_valid\tfrac_mod\tN_mod\tN_canonical\tN_other_mod\tN_delete\tN_fail\tN_diff\tN_no_call
chr21\t100\t101\tm\t.\t+\t100\t101\t0,0,0\t10\t80.0\t8\t2\t0\t0\t0\t0\t0
chr21\t200\t201\tm\t.\t+\t200\t201\t0,0,0\t2\t50.0\t1\t1\t0\t0\t0\t0\t0
chr21\t300\t301\tm\t.\t+\t300\t301\t0,0,0\t20\t60.0\t12\t8\t0\t0\t0\t0\t0
chr21\t400\t401\th\t.\t+\t400\t401\t0,0,0\t15\t10.0\t1\t14\t0\t0\t0\t0\t0
"""


# ── summarize_sniffles.py (real VCF parser, canned input) ─────────────────────


SNIFFLES_VCF_FIXTURE = """\
##fileformat=VCFv4.2
##source=Sniffles2_2.4
##INFO=<ID=SVTYPE,Number=1,Type=String,Description="Type">
##INFO=<ID=SVLEN,Number=1,Type=Integer,Description="Length">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE
chr21\t1000\ts1\tN\t<INS>\t30\tPASS\tSVTYPE=INS;SVLEN=500\tGT\t0/1
chr21\t2000\ts2\tN\t<DEL>\t30\tPASS\tSVTYPE=DEL;SVLEN=-120\tGT\t0/1
chr21\t3000\ts3\tN\t<INV>\t30\tPASS\tSVTYPE=INV;SVLEN=800\tGT\t0/1
chr21\t4000\ts4\tN\t<DUP>\t30\tPASS\tSVTYPE=DUP;SVLEN=200\tGT\t0/1
chr21\t5000\ts5\tN\tN]chr22:1000]\t30\tPASS\tSVTYPE=BND\tGT\t0/1
chr21\t6000\ts6\tN\t<DEL>\t30\tPASS\tSVTYPE=DEL;SVLEN=-80\tGT\t0/1
"""


def test_summarize_sniffles_parses_vcf(tmp_path: Path):
    vcf = tmp_path / "sample.vcf"
    vcf.write_text(SNIFFLES_VCF_FIXTURE)

    out = tmp_path / "out.tsv"
    subprocess.run(
        [sys.executable,
         str(DEMO_DIR / "scripts" / "summarize_sniffles.py"),
         "--assay-id", "A1",
         "--input", str(vcf),
         "--output", str(out)],
        check=True,
    )
    df = pd.read_csv(out, sep="\t")
    row = df.iloc[0]
    assert row["assay_id"] == "A1"
    assert row["n_svs_total"] == 6
    assert row["n_ins"] == 1
    assert row["n_del"] == 2
    assert row["n_inv"] == 1
    # DUP merges into BND in the summary (so INS+DEL+INV+BND sums to total).
    assert row["n_bnd"] == 2
    assert row["n_ins"] + row["n_del"] + row["n_inv"] + row["n_bnd"] == row["n_svs_total"]
    # SVLEN values present: 500, 120, 800, 200, 80 → median = 200
    assert row["sv_size_median"] == 200


def test_summarize_sniffles_gzipped_input(tmp_path: Path):
    import gzip
    vcf = tmp_path / "sample.vcf.gz"
    with gzip.open(vcf, "wt") as f:
        f.write(SNIFFLES_VCF_FIXTURE)

    out = tmp_path / "out.tsv"
    subprocess.run(
        [sys.executable,
         str(DEMO_DIR / "scripts" / "summarize_sniffles.py"),
         "--assay-id", "A1",
         "--input", str(vcf),
         "--output", str(out)],
        check=True,
    )
    df = pd.read_csv(out, sep="\t")
    assert df.iloc[0]["n_svs_total"] == 6


# ── summarize_modkit.py (real bedMethyl parser, canned input) ─────────────────


def test_summarize_modkit_parses_bedmethyl(tmp_path: Path):
    bed = tmp_path / "sample.bedMethyl"
    bed.write_text(BEDMETHYL_FIXTURE)

    out = tmp_path / "out.tsv"
    subprocess.run(
        [sys.executable,
         str(DEMO_DIR / "scripts" / "summarize_modkit.py"),
         "--assay-id", "A1",
         "--input", str(bed),
         "--output", str(out),
         "--high-conf-cov", "5"],
        check=True,
    )
    df = pd.read_csv(out, sep="\t")
    assert df.iloc[0]["n_cpg_sites"] == 3  # 'h' row excluded
    # mean of (80, 50, 60) / 100 = 0.633
    assert df.iloc[0]["mean_meth"] == pytest.approx(0.633, abs=1e-3)
    # 2 of 3 rows have N_valid >= 5 → 66.67%
    assert df.iloc[0]["pct_high_conf"] == pytest.approx(66.67, abs=0.01)
