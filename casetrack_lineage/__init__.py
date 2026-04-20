"""casetrack_lineage — assay lineage + batch tracking (proposal 0006).

Adds two tables (``batches``, ``assay_sources``) and a ``batch_id`` column to
``assays``.  Exposes ``migrate-lineage``, ``add-batch``, and ``link-sources``
subcommands plus a batch-level censor cascade.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
