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

## Install With `uvx`

You can run the MCP server directly from GitHub without cloning the repo:

```toml
[mcp_servers.obsidian_readonly]
command = "uvx"
args = [
  "--from",
  "git+https://github.com/eothL/obsidian-readonly-mcp",
  "obsidian-readonly-mcp"
]

[mcp_servers.obsidian_readonly.env]
OBSIDIAN_READONLY_VAULT = "Your Vault Name"
```

Use the Obsidian vault name, not the filesystem path. It should be the same value you would pass to `obsidian vault="Your Vault Name" ...`.

If this package is later published to PyPI, the config can become:

```toml
[mcp_servers.obsidian_readonly]
command = "uvx"
args = ["obsidian-readonly-mcp"]

[mcp_servers.obsidian_readonly.env]
OBSIDIAN_READONLY_VAULT = "Your Vault Name"
```

Restart Codex or start a new thread so the MCP server is discovered.

## Install From A Local Clone

Clone this repo somewhere stable, then add the MCP server to your Codex config:

```toml
[mcp_servers.obsidian_readonly]
command = "python3"
args = ["/absolute/path/to/obsidian-readonly-mcp/src/obsidian_readonly_mcp.py"]

[mcp_servers.obsidian_readonly.env]
OBSIDIAN_READONLY_VAULT = "Your Vault Name"
```

Restart Codex or start a new thread so the MCP server is discovered.

## Codex Plugin

This repository also includes Codex plugin metadata:

- `.codex-plugin/plugin.json`
- `.mcp.json`
- `skills/obsidian-readonly-mcp/SKILL.md`

The plugin bundles the MCP server and the companion skill so Codex can learn to prefer the read-only Obsidian tools for vault exploration. The plugin MCP config does not hard-code a vault name; set `OBSIDIAN_READONLY_VAULT` in your environment or pass the `vault` argument in tool calls.

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

## Read-only diagnostics and timeouts

The read-only server writes local JSON-line diagnostic logs. MCP stdout remains
reserved for protocol responses. Events record request receipt, command start,
command completion or timeout, cleanup, and response delivery, linked by an
internally generated trace ID. Logs include timestamps, server PID, tool name,
exit code, elapsed milliseconds, and output byte counts. They exclude request IDs,
arguments, vault/note paths, queries, note contents, and raw stdout/stderr. Raw
stderr remains available in the tool response as before; it is not persisted to logs.
This protects diagnostic logs but does not anonymize tool results.

| Setting | Default | Purpose |
| --- | --- | --- |
| `OBSIDIAN_READONLY_TIMEOUT_SECONDS` | `30` | Positive command timeout in seconds. |
| `OBSIDIAN_READONLY_LOG_DIR` | Platform default below | Local log directory; set to `off` to disable. |

On macOS, logs are in `~/Library/Logs/obsidian-readonly-mcp/server.log`.
Elsewhere they are in `$XDG_STATE_HOME/obsidian-readonly-mcp/server.log`, falling
back to `~/.local/state/obsidian-readonly-mcp/server.log`. Logs rotate at 5 MiB,
keeping the current file and two backups (about 15 MiB total). New directories
are private and log files use mode `0600`. Use a separate log directory for each
concurrently running server instance; rotation is intended for one writer.
A log setup failure reports a generic warning on stderr and does not prevent
serving requests. Logging adds no dependencies.

Call `health` with `{}` or `{"vault":"Your Vault Name"}` to check both the server
and an actual vault command (`vault info=name`). It returns responsiveness,
elapsed time and the configured timeout, with `isError` when the probe fails.
This checks the app connection, not just whether the CLI executable exists.
A failed probe does not by itself distinguish a closed app from an IPC failure.
MCP `ping` checks only the server.

On POSIX, timed-out CLI commands and helpers in their dedicated process group
are killed. Pipe cleanup has its own one-second limit, avoiding an unbounded
wait when a helper retains output pipes. The running Obsidian app is not targeted.
On other platforms, only the direct CLI process is killed. Requests are handled
serially, so concurrent client calls can queue behind an active command; the
command timeout begins when that command is launched.

To investigate a delay, inspect the last event for a trace: `command_started`
without completion points to the CLI/app boundary; `command_timeout` confirms
the server timeout fired; `response_sent` means the response was flushed to the
client transport. No `request_received` event suggests checking startup or the
client connection (also verify logging is enabled and writable).

After modifying this repository, restart the MCP process using this checkout.
A server configured to run a separate copied script does not pick up these changes
automatically. The changes do not explain past hangs without diagnostic evidence.

Diagnostics and workflow regression tests run without Obsidian:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Bounded retrieval and batches

`read` accepts either `heading` (exact ATX Markdown heading text without the
leading `#`, including subsections), or `start_line` / `end_line` (1-based,
inclusive). Full reads are unchanged. Use `outline` to discover headings and
`search_context` to find line numbers. Duplicate headings require a line range.
Setext headings are not matched by `heading`; use a line range for those.
Fenced code and YAML frontmatter are excluded from heading matching.
The MCP still fetches the note through Obsidian and filters it before returning
it; it does not cache notes or fall back to disk. A truncated source is rejected
for partial reads rather than silently returning an incomplete section.

```json
{"path":"Notes/GPU.md","heading":"Memory hierarchy"}
```

```json
{"path":"Notes/GPU.md","start_line":20,"end_line":50}
```

`batch` accepts 1–8 independent read-only calls. Results retain input order and
have individual `isError` flags. It runs commands sequentially under one shared
command timeout and output budget; nesting and unknown tools are rejected.
Arguments (including `vault` when supported) belong on each child call. Budget
exhaustion returns explicit errors for omitted results. Prefer narrow searches
and selected sections over batching many full documents. Batching reduces MCP
call overhead, not the number of underlying CLI operations by itself.

```json
{"calls":[{"tool":"outline","arguments":{"path":"Notes/GPU.md"}},{"tool":"links","arguments":{"path":"Notes/GPU.md"}}]}
```

The runtime exposes `run_command` and `main(request_handler)` so a project adapter
can retain stricter schemas and a pinned vault while reusing logging and timeout
fixes. Updating a repository checkout affects new server processes; already
running servers must restart. Detached script copies do not update automatically.

## Protocol compatibility

This server uses local stdio and the legacy `initialize` handshake. It does not
store conversation history or note caches, but it has not implemented the modern
2026-07-28 stateless wire protocol (`server/discover` and per-request metadata).
Initialization negotiates a supported legacy version instead of echoing an
unsupported modern version. No HTTP endpoint or session service is introduced.

## Evaluating real tool use

Unit tests verify correctness; they cannot establish whether an agent chooses a
tool or produces a better answer. Use a fresh, read-only agent session with a
fixed task/model and `codex exec --json`, keeping the prompt independent of the
feature being evaluated. Save traces outside the repo: they contain private note
contents. Run the same prompt with baseline and candidate tool catalogs, then:

```bash
python3 scripts/summarize_trace.py /path/to/run.jsonl --server-log /path/to/server.log
```

The summary excludes note text and arguments. Compare logical operations as well
as MCP calls, partial-read use, output size, budget/error events, cached versus
uncached input tokens, and a manually scored answer-quality rubric. Repeat tasks
before claiming stable savings. Suggested fixtures: a short note, a long lecture
with relevant subsections, ambiguous headings, a missing note, a timeout, and a
large search response. Check that conclusions remain supported, contradictory
notes are noticed, and missing/error results are not treated as evidence.

## Failure cooldown

After two consecutive infrastructure failures for the same vault, commands return
an immediate tool error with `status: cooldown` and `retry_after_seconds`. The
process does not sleep or launch more CLI subprocesses during the pause, including
for children of a batch. `OBSIDIAN_READONLY_COOLDOWN_SECONDS` defaults to `60`;
set it to `0` to disable. Counters are process-local, per vault, and bounded to 64
vault entries. Expiry permits another attempt; successful execution clears the
failure streak. A deliberate `health` call bypasses the pause and clears it if
successful. Repeated health polling defeats this protection.

Launch failures, command timeouts and infrastructure command errors count. Input
validation failures and recognized missing-file/folder/property/heading responses
do not. No missing note is treated as proof that the Obsidian app is broken.
Cooldown state contains no note data or conversation history; it is operational
health state, compatible with a future stateless protocol implementation.

The MCP always executes the Obsidian CLI directly (argv, no shell). It does not
replace failures with Bash commands, direct `.md` reads, or cached notes. The
companion skills likewise instruct agents to report unavailability and respect
the retry interval instead of falling back to direct filesystem retrieval.
