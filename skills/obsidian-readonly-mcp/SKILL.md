---
name: obsidian-readonly-mcp
description: Use when reading or exploring an Obsidian vault through the obsidian_readonly MCP server, especially when sandboxed agents need search, read, backlinks, links, outline, tags, properties, bases, history, or graph-neighborhood inspection without raw Obsidian CLI access.
---

# Obsidian Readonly MCP

Use the `obsidian_readonly` MCP tools for Obsidian vault inspection. Do not call raw `obsidian` CLI when these tools are available.

## Workflow

1. Start with `mcp__obsidian_readonly__search` or `mcp__obsidian_readonly__search_context`.
2. Read the strongest notes with `mcp__obsidian_readonly__read`.
3. For graph-aware exploration, run:
   - `mcp__obsidian_readonly__backlinks`
   - `mcp__obsidian_readonly__links`
   - `mcp__obsidian_readonly__outline`
4. Follow only useful graph neighbors. Prefer specific Tier 1/domain notes over broad hubs.
5. Return a compact graph artifact when the main answer depends on topology:

```text
Seed: Note A
Note A --outgoing--> Note B: specific reason
Note A --incoming--> Note C: repeated backlink
Skipped: Broad Hub Note: too general
Gap: Note A has backlinks but no outgoing links
```

## Tool Families

- Discovery: `search`, `search_context`, `tags`, `tag`, `aliases`, `unresolved`, `help`
- Notes and graph: `read`, `backlinks`, `links`, `outline`, `wordcount`
- Vault inventory: `files`, `file`, `folders`, `folder`, `deadends`, `orphans`, `recents`, `random_read`, `vault`, `vaults`
- Structured data: `bases`, `base_query`, `base_views`, `properties`, `property_read`, `tasks`, `templates`, `template_read`
- Metadata: `history`, `history_list`, `history_read`, `diff`, `plugins`, `plugin`, `theme`, `themes`, `commands`, `hotkey`, `hotkeys`, `snippets`, `snippets_enabled`, `tabs`, `workspace`, `workspaces`
- Developer inspection: `dev_console`, `dev_errors`, `dev_css`, `dev_dom`

## Safety

These tools are read-only. Do not attempt to create, edit, delete, move, rename, append, prepend, set properties, install plugins, reload the app, execute commands, open files, run arbitrary eval, or access the network.

If the MCP tools are unavailable, report that the `obsidian_readonly` MCP server is not loaded. Do not work around missing MCP tools with raw `obsidian` from a sandboxed agent.
