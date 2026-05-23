from setuptools import setup, find_packages

setup(
    name="casetrack",
    version="0.11.0",
    description="Lifecycle data management for bioinformatics pipelines — manifest-centric "
                "on flat TSVs (v0.2) or SQLite-backed with normalized patient/specimen/assay "
                "hierarchy and QC / consent tracking (v0.3 / v0.4).",
    author="Samuel Ahuno",
    author_email="sahuno@mskcc.org",
    py_modules=["casetrack"],
    packages=["casetrack_qc", "casetrack_mcp", "casetrack_lineage", "casetrack_lifecycle", "casetrack_gui"],
    package_data={"casetrack_gui": ["templates/*.html", "static/*"]},
    install_requires=[
        "pandas>=1.5.0",
        "duckdb>=0.9",
        # tomllib is stdlib on 3.11+; tomli is the backport for 3.10 and below.
        'tomli>=2.0; python_version < "3.11"',
    ],
    extras_require={
        "excel": ["openpyxl>=3.0"],
        "parquet": ["pyarrow>=10.0"],
        # v0.6 Part B: optional MCP server for AI-agent integration.
        "mcp": ["mcp>=1.0"],
        # v0.8: optional operator GUI (FastAPI + Jinja2).
        "gui": ["fastapi>=0.110", "jinja2>=3.1", "uvicorn>=0.27", "starlette>=0.36"],
        "all": [
            "openpyxl>=3.0", "pyarrow>=10.0", "mcp>=1.0",
            "fastapi>=0.110", "jinja2>=3.1", "uvicorn>=0.27", "starlette>=0.36",
        ],
    },
    entry_points={
        "console_scripts": [
            "casetrack=casetrack:main",
            # v0.6 Part B: `casetrack-mcp` stdio server (requires `mcp` extra).
            "casetrack-mcp=casetrack_mcp.server:main",
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
