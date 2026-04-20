"""MCP stdio server for casetrack.

Thin adapter over `casetrack_mcp.tools` — tool logic lives there, this
file is just the MCP-protocol wiring (schemas, stdio transport, error
conversion). Keeping them separate lets us unit-test tool behavior
without the `mcp` SDK installed.

Entry point registered via setup.py `console_scripts`:
    casetrack-mcp = casetrack_mcp.server:main

Wire into Claude Desktop in `~/Library/Application Support/Claude/
claude_desktop_config.json` (macOS) or the equivalent on Linux/Windows:

    {
      "mcpServers": {
        "casetrack": {
          "command": "casetrack-mcp"
        }
      }
    }

If the casetrack project you want to inspect is a pre-v0.6 legacy
project, set CASETRACK_ALLOW_LEGACY=1 in the env block so the MCP
server can read past the hard gate.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from casetrack_mcp.tools import (
    MCPToolError,
    list_projects_tool,
    query_tool,
)

# The mcp SDK is optional (install via `pip install casetrack[mcp]`).
# Import here so the module can still be imported for testing the
# tool helpers, even in environments without the SDK.
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover — import-time branch
    Server = None  # type: ignore[assignment]
    stdio_server = None  # type: ignore[assignment]
    TextContent = None  # type: ignore[assignment]
    Tool = None  # type: ignore[assignment]
    _MCP_AVAILABLE = False


_LIST_PROJECTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_id": {
            "type": "string",
            "description": (
                "DNS-label slug identifying a casetrack project in the local "
                "registry. Call casetrack_list_projects first if you don't "
                "know the id."
            ),
        },
        "sql": {
            "type": "string",
            "description": (
                "A single SELECT or WITH statement. Non-SELECT SQL is "
                "rejected — use the casetrack CLI to mutate data. "
                "The raw join view is `_`; the QC/consent-cascaded view "
                "is `_active`."
            ),
        },
    },
    "required": ["project_id", "sql"],
    "additionalProperties": False,
}


def _build_server() -> "Server":
    """Register the two tool handlers against a fresh Server instance."""
    if not _MCP_AVAILABLE:
        raise RuntimeError(
            "The `mcp` package is not installed. Run "
            "`pip install casetrack[mcp]` to enable the MCP server."
        )
    app = Server("casetrack")

    @app.list_tools()
    async def _list_tools() -> list["Tool"]:
        return [
            Tool(
                name="casetrack_list_projects",
                description=(
                    "List the casetrack projects registered on this machine. "
                    "Returns project_id, name, path, and last_seen for each "
                    "entry. Use the returned project_id with "
                    "casetrack_query."
                ),
                inputSchema=_LIST_PROJECTS_SCHEMA,
            ),
            Tool(
                name="casetrack_query",
                description=(
                    "Run a read-only SQL SELECT against a casetrack project. "
                    "project_id must be one of the slugs returned by "
                    "casetrack_list_projects — unknown ids fail fast with "
                    "the valid set listed. Tables: patients, specimens, "
                    "assays, qc_events. Views: `_` (raw join), `_active` "
                    "(QC + consent cascade applied)."
                ),
                inputSchema=_QUERY_SCHEMA,
            ),
        ]

    @app.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list["TextContent"]:
        try:
            if name == "casetrack_list_projects":
                payload = list_projects_tool()
            elif name == "casetrack_query":
                project_id = arguments.get("project_id")
                sql = arguments.get("sql")
                payload = query_tool(project_id, sql)
            else:
                raise MCPToolError(f"unknown tool: {name!r}")
        except MCPToolError as e:
            # Surface expected errors as readable tool output, not as
            # protocol-level exceptions.
            return [TextContent(type="text", text=f"Error: {e}")]
        return [TextContent(
            type="text",
            text=json.dumps(payload, indent=2, default=str),
        )]

    return app


async def _run() -> None:
    app = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def main() -> None:
    """Console-script entry point."""
    if not _MCP_AVAILABLE:
        print(
            "Error: the `mcp` package is not installed. "
            "Run `pip install casetrack[mcp]` and retry.",
            file=sys.stderr,
        )
        sys.exit(1)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
