"""Path inference for the tool-first results directory convention.

Given a path inside ``<project>/results/<tool>/<run_tag>/.../<leaf>``, this
module resolves:

* the project root (nearest ancestor containing ``casetrack.toml``),
* the analysis tool, run_tag, and level-specific entity ids,
* the matching ``[analyses.<tool>]`` declaration (column_prefix, summary_tsv).

Used by ``casetrack append --infer-from-path`` so pipeline authors can
``cd`` into any leaf of the results tree and register outputs without
passing every flag by hand. See the ``[layout]`` block in the shipped
TOML templates for the path-template syntax.

Author: Samuel Ahuno <ekwame001@gmail.com>
"""
from __future__ import annotations

import re
from pathlib import Path


PROJECT_TOML_NAME = "casetrack.toml"

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class InferenceError(Exception):
    """Raised when a path cannot be unambiguously mapped to a casetrack row."""


def find_project_root(start: Path) -> Path:
    """Walk up from ``start`` until a ``casetrack.toml`` is found.

    Mirrors ``git``'s strategy for locating the repo root. Raises
    :class:`InferenceError` when no ancestor contains a ``casetrack.toml``.
    """
    start = Path(start).resolve()
    # If start is a file, begin the walk from its parent.
    cur = start if start.is_dir() else start.parent
    while True:
        if (cur / PROJECT_TOML_NAME).is_file():
            return cur
        if cur.parent == cur:
            raise InferenceError(
                f"no {PROJECT_TOML_NAME} found in any ancestor of {start}"
            )
        cur = cur.parent


def _template_to_regex(template: str) -> re.Pattern[str]:
    """Turn ``{tool}/{run_tag}/{patient_id}`` into a named-group regex."""
    parts: list[str] = []
    last_end = 0
    for m in _PLACEHOLDER_RE.finditer(template):
        parts.append(re.escape(template[last_end:m.start()]))
        parts.append(f"(?P<{m.group(1)}>[^/]+)")
        last_end = m.end()
    parts.append(re.escape(template[last_end:]))
    # fullmatch when rel path exactly equals the template, but tolerate
    # deeper paths (a summary TSV or subdir below the leaf).
    return re.compile("".join(parts) + r"(?:/.*)?")


def infer_from_path(project_dir: Path, start: Path, schema: dict) -> dict:
    """Resolve path context for ``casetrack append --infer-from-path``.

    Parameters
    ----------
    project_dir :
        Absolute path to the project root (the directory containing
        ``casetrack.toml``).
    start :
        Path somewhere under ``<project_dir>/<results_dir>/`` — typically
        ``$PWD``.
    schema :
        The parsed schema dict returned by :func:`casetrack.load_schema`.

    Returns
    -------
    dict
        ``{"tool", "level", "run_tag", "patient_id", "specimen_id"?,
        "assay_id"?, "column_prefix", "summary_tsv", "leaf_dir"}``.

    Raises
    ------
    InferenceError
        If ``start`` is outside the project results tree, no path_template
        matches, the matched tool is not declared in ``[analyses]``, or the
        inferred level disagrees with the tool's declared level.
    """
    layout = schema.get("layout")
    if not layout:
        raise InferenceError(
            "no [layout] section in casetrack.toml — add [layout.path_templates]"
            " to enable --infer-from-path"
        )

    project_dir = Path(project_dir).resolve()
    start = Path(start).resolve()

    results_root = (project_dir / layout.get("results_dir", "results")).resolve()
    try:
        rel = start.relative_to(results_root)
    except ValueError as e:
        raise InferenceError(
            f"path {start} is not under results root {results_root}"
        ) from e

    templates = layout["path_templates"]
    # Try most-specific templates first (assay > specimen > patient) so a
    # deep path doesn't accidentally match the patient-level template.
    by_depth = sorted(
        templates.items(), key=lambda kv: kv[1].count("/"), reverse=True
    )

    rel_str = str(rel).rstrip("/")
    match = None
    matched_level = None
    for level, tmpl in by_depth:
        pattern = _template_to_regex(tmpl)
        m = pattern.fullmatch(rel_str)
        if m:
            match = m
            matched_level = level
            break

    if not match:
        raise InferenceError(
            f"no [layout.path_templates.*] matched {rel_str!r}; "
            f"available templates: {list(templates)}"
        )

    fields = match.groupdict()
    tool = fields.pop("tool", None)
    run_tag = fields.pop("run_tag", None)
    if not tool:
        raise InferenceError(
            f"matched template {matched_level!r} did not capture a tool name"
        )
    if not run_tag:
        raise InferenceError(
            f"matched template {matched_level!r} did not capture a run_tag"
        )

    analyses = schema.get("analyses") or {}
    if tool not in analyses:
        raise InferenceError(
            f"unknown tool {tool!r} — add [analyses.{tool}] to casetrack.toml "
            f"with at least `level = \"{matched_level}\"`"
        )
    tool_cfg = analyses[tool]
    declared_level = tool_cfg.get("level")
    if declared_level != matched_level:
        raise InferenceError(
            f"path implies level={matched_level!r} but [analyses.{tool}] "
            f"declares level={declared_level!r}"
        )

    # Determine the leaf directory: the deepest directory that still matches
    # the template exactly (without the optional trailing "/…" capture).
    exact_re = _template_to_regex(templates[matched_level])
    parts = rel_str.split("/")
    leaf_depth = templates[matched_level].count("/") + 1
    leaf_parts = parts[:leaf_depth]
    leaf_dir = results_root.joinpath(*leaf_parts)

    result = {
        "tool": tool,
        "level": matched_level,
        "run_tag": run_tag,
        "column_prefix": tool_cfg.get("column_prefix"),
        "summary_tsv": tool_cfg.get("summary_tsv"),
        "leaf_dir": leaf_dir,
        "project_dir": project_dir,
        "results_root": results_root,
    }
    # Copy level-key ids through verbatim (patient_id, specimen_id, assay_id).
    for k, v in fields.items():
        result[k] = v
    return result
