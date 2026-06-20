# Obsidian Readonly MCP

Read-only MCP tools for querying a running Obsidian vault from Codex or another agentic runtime.

## Why this exists

The Obsidian CLI is not just a filesystem reader; it is connected to your main Obsidian app. Commands such as `backlinks`, `links`, `outline`, and indexed `search` need to talk to the running Obsidian app. In agentic runtimes, the agent's filesystem sandbox can block or destabilize that app/IPC boundary. For example, running `obsidian help` inside an agent sandbox can cause the Obsidian app to crash.

Giving the whole agent unsandboxed shell access fixes the IPC problem, but it gives the agent too much permission. This MCP server is the narrower solution:

```text
sandboxed agent
  -> read-only MCP tool call
  -> host-side MCP server
  -> obsidian CLI
  -> running Obsidian app
```

The agent can stay sandboxed. The host-side MCP server gets permission to reach Obsidian, but it exposes only a fixed allowlist of read-only Obsidian commands.

## Safety model

This server:

- exposes only read-only Obsidian operations
- rejects unknown tools
- validates vault-relative paths and rejects `..` path traversal
- builds `obsidian` argv directly without shell interpolation
- caps output size
- times out long calls

It does **not** expose:

- note creation or editing
- delete, move, rename, append, prepend
- property mutation
- plugin/theme install, enable, disable, reload, uninstall
- `obsidian command`
- `obsidian eval`
- app reload/restart/open/tab/workspace mutation
- browser/web/open actions

Feel free to modify it to add these features if you need them.

## Tools

Core vault reads:

- `search`
- `search_context`
- `read`
- `daily_read`
- `daily_path`
- `backlinks`
- `links`
- `outline`
- `aliases`
- `tags`
- `tag`
- `unresolved`
- `properties`
- `property_read`
- `help`

Filesystem/index reads through Obsidian:

- `file`
- `files`
- `folder`
- `folders`
- `wordcount`
- `deadends`
- `orphans`
- `recents`
- `random_read`
- `vault`
- `vaults`
- `version`

Structured features:

- `bases`
- `base_query`
- `base_views`
- `bookmarks`
- `tasks`
- `templates`
- `template_read`
- `snippets`
- `snippets_enabled`

History/config metadata reads:

- `diff`
- `history`
- `history_list`
- `history_read`
- `hotkey`
- `hotkeys`
- `plugins`
- `plugin`
- `plugins_enabled`
- `theme`
- `themes`
- `tabs`
- `workspace`
- `workspaces`
- `commands`

Developer read-only inspection:

- `dev_console`
- `dev_errors`
- `dev_css`
- `dev_dom`

## Install

Clone this repo somewhere stable, then add the MCP server to your Codex config:

```toml
[mcp_servers.obsidian_readonly]
command = "python3"
args = ["/absolute/path/to/obsidian-readonly-mcp/src/obsidian_readonly_mcp.py"]

[mcp_servers.obsidian_readonly.env]
OBSIDIAN_READONLY_VAULT = "Your Vault Name"
```

Use the Obsidian vault name, not the filesystem path. It should be the same value you would pass to `obsidian vault="Your Vault Name" ...`.

Restart Codex or start a new thread so the MCP server is discovered.

## Usage

Once loaded, tools are exposed as names such as:

```text
mcp__obsidian_readonly__search
mcp__obsidian_readonly__read
mcp__obsidian_readonly__backlinks
mcp__obsidian_readonly__links
mcp__obsidian_readonly__outline
```

Example tool arguments:

```json
{"query": "agent harness memory", "limit": 10}
```

```json
{"file": "Agent harness memory", "counts": true, "format": "json"}
```

## Local smoke test

With Obsidian open:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python3 src/obsidian_readonly_mcp.py
```

End-to-end read:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search","arguments":{"query":"agent harness","limit":3}}}' \
  | python3 src/obsidian_readonly_mcp.py
```

## Companion Skill

This repo includes a Codex skill in `skills/obsidian-readonly-mcp/`. Install or copy that skill into your Codex skills directory if you want agents to automatically prefer these tools for Obsidian vault exploration.
