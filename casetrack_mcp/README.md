# casetrack-mcp

MCP (Model Context Protocol) server that exposes casetrack to AI agents like Claude Desktop.

## What it does

Two tools — designed to answer "what casetrack projects do you have?" and "what's in this one?" without the agent ever seeing a filesystem path:

| Tool | Arguments | Returns |
|---|---|---|
| `casetrack_list_projects` | (none) | JSON with each registered project's `project_id`, `name`, `path`, `last_seen` |
| `casetrack_query` | `project_id` (slug), `sql` (SELECT / WITH only) | JSON with `columns`, `rows`, `row_count`, `truncated` |

Path input never appears in either tool signature — that's the **hallucination-reduction** design from [proposal 0005 §5.6](../docs/proposals/0005-id-format-and-project-identity.md). Unknown `project_id` returns a fail-fast error listing the valid set, so the agent can't invent paths that "might work."

## Install

Ship the `mcp` SDK alongside casetrack's core deps:

```bash
pip install casetrack[mcp]
```

(Or `pip install -e ".[mcp]" --user` for a local checkout.)

## Wire into Claude Desktop

Add this block to the MCP config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "casetrack": {
      "command": "casetrack-mcp"
    }
  }
}
```

Restart Claude Desktop. The `casetrack_list_projects` and `casetrack_query` tools appear in the "/" menu.

### Reading legacy projects

v0.6 refuses to operate on projects that haven't been migrated to the identity scheme. If you have a pre-v0.6 cohort you want to inspect without migrating yet, opt into the bypass in the MCP server's env:

```json
{
  "mcpServers": {
    "casetrack": {
      "command": "casetrack-mcp",
      "env": {
        "CASETRACK_ALLOW_LEGACY": "1"
      }
    }
  }
}
```

## Safety rails

- **Read-only SQL.** `casetrack_query` rejects anything that's not `SELECT` or `WITH`. Mutating data goes through the CLI, which logs to `provenance.jsonl`.
- **Row cap.** Results are truncated at 10 000 rows with `truncated=true` in the payload — enough for the vast majority of queries, small enough to keep LLM context windows responsive.
- **Closed-world project lookup.** The tool only accepts `project_id`s that exist in `~/.casetrack/registry.json`. Agents can't address arbitrary paths.

## Layout

```
casetrack_mcp/
├── __init__.py   — re-exports tools.list_projects_tool, tools.query_tool
├── tools.py      — pure Python, unit-testable without the mcp SDK
├── server.py     — MCP stdio adapter (imports mcp)
└── README.md     — this file
```

Unit tests live in the top-level `tests/test_mcp_tools.py`; they run against `tools.py` directly and don't require the `mcp` SDK to be installed.
