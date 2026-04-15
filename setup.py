from setuptools import setup, find_packages

setup(
    name="casetrack",
    version="0.1.0",
    description="Manifest-centric case management for bioinformatics pipelines",
    author="Samuel Ahuno",
    author_email="sahuno@mskcc.org",
    py_modules=["casetrack"],
    install_requires=[
        "pandas>=1.5.0",
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
    python_requires=">=3.8",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Topic :: Scientific/Engineering :: Bio-Informatics",
        "Environment :: Console",
    ],
)
