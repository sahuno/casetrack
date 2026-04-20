"""casetrack_mcp — MCP server exposing casetrack to AI agents.

Two tools:
  - casetrack_list_projects() -> list of known projects (registry-backed).
  - casetrack_query(project_id, sql) -> rows from a registered project.

Per proposal 0005 §5.6, the closed-world lookup (project_id must be a
known registry entry) is the hallucination-reduction lever — agents
cannot invent paths that "might work" because paths are never in the
tool signature.

Layout:
  - tools.py  — pure-Python implementations, unit-testable without mcp.
  - server.py — thin MCP stdio adapter (imports the `mcp` SDK).

Author: Samuel Ahuno (ekwame001@gmail.com)
"""

from casetrack_mcp.tools import (
    MCPToolError,
    list_projects_tool,
    query_tool,
)

__all__ = [
    "MCPToolError",
    "list_projects_tool",
    "query_tool",
]
