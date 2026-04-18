from setuptools import setup, find_packages

setup(
    name="casetrack",
    version="0.4.2",
    description="Lifecycle data management for bioinformatics pipelines — manifest-centric "
                "on flat TSVs (v0.2) or SQLite-backed with normalized patient/specimen/assay "
                "hierarchy and QC / consent tracking (v0.3 / v0.4).",
    author="Samuel Ahuno",
    author_email="sahuno@mskcc.org",
    py_modules=["casetrack"],
    packages=["casetrack_qc"],
    install_requires=[
        "pandas>=1.5.0",
        "duckdb>=0.9",
        # tomllib is stdlib on 3.11+; tomli is the backport for 3.10 and below.
        'tomli>=2.0; python_version < "3.11"',
    ],
    extras_require={
        "excel": ["openpyxl>=3.0"],
        "parquet": ["pyarrow>=10.0"],
        "all": ["openpyxl>=3.0", "pyarrow>=10.0"],
    },
    entry_points={
        "console_scripts": [
            "casetrack=casetrack:main",
        ],
    },
    python_requires=">=3.10",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Topic :: Scientific/Engineering :: Bio-Informatics",
        "Environment :: Console",
    ],
)
