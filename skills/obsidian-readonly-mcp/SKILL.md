---
name: obsidian-readonly-mcp
description: Use when reading or exploring an Obsidian vault through the obsidian_readonly MCP server, especially when sandboxed agents need search, read, backlinks, links, outline, tags, properties, bases, history, or graph-neighborhood inspection without raw Obsidian CLI access.
---

# Obsidian Readonly MCP

Use the `obsidian_readonly` MCP tools for Obsidian vault inspection. Do not call raw `obsidian` CLI when these tools are available.

## Configuration

The MCP server needs access to the running Obsidian app through the Obsidian CLI. If no default vault is configured, pass a `vault` argument to tool calls or ask the user to set `OBSIDIAN_READONLY_VAULT` to their Obsidian vault name.

## Workflow

1. Start with `mcp__obsidian_readonly__search` or `mcp__obsidian_readonly__search_context`.
2. Read the strongest notes with `mcp__obsidian_readonly__read`.
3. For graph-aware exploration, run:
   - `mcp__obsidian_readonly__backlinks`
   - `mcp__obsidian_readonly__links`
   - `mcp__obsidian_readonly__outline`
4. Follow only useful graph neighbors.
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

## Bounded retrieval and failures

- Group up to 8 independent lookups with `batch`; put arguments on each child.
  Inspect every child result. An output/time-budget error is missing evidence,
  not an empty result. Narrow the query or request only the missing portion.
- For long notes, use `outline` then `read` with an exact ATX (`#`) `heading`,
  including its subsections; or use inclusive 1-based `start_line`/`end_line`.
  Use line ranges for duplicate or Setext headings. Retrieve properties separately
  if a selected section omits frontmatter needed to interpret the note.
- After two infrastructure failures, the MCP returns `status: cooldown` with
  `retry_after_seconds` (60 seconds by default), without launching more CLI calls.
  Stop retrying the same backend, including through batches or other tool names.
  Continue independent work. After the connection is fixed, use one `health`
  probe; a successful probe clears the cooldown. Do not repeatedly poll health.
- Missing notes and invalid arguments do not trigger the infrastructure cooldown.
  Distinguish them from unavailable tools, a timeout, and budget exhaustion.
- Do not replace failed/unavailable MCP reads with Bash, raw Obsidian CLI, `cat`,
  `rg`, or direct-file reads. Report the limitation; no automatic disk fallback
  or note cache is used. This restriction concerns vault retrieval, not inspection
  of MCP source/configuration when explicitly troubleshooting it.
