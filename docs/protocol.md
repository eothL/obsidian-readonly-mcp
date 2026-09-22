# Protocol migration: 2026-07-28

This server supports modern MCP `2026-07-28` and the existing legacy revisions
through one tool implementation. It uses **stdio**, not HTTP. No network listener,
new runtime dependency, arbitrary Python/shell tool, or direct-file fallback is
introduced. Every vault read still runs an allowlisted Obsidian CLI command.
The Python implementation processes CLI output; it does not load Markdown from
disk when Obsidian is unavailable. Section selection happens on CLI output.

## Before: initialization-based MCP

Each object below is one message (one line on the actual stdio wire).
The client opens with:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"example","version":"1.0"}}}
```

The server replies:

```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"obsidian-readonly","version":"0.1.0"}}}
```

The client confirms readiness, then calls a tool:

```json
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read","arguments":{"path":"Example.md"}}}
```

The tool result is:

```json
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"Example note content"}],"isError":false}}
```

## After: self-contained requests

A modern client can call the tool immediately, without initialization:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {},
      "io.modelcontextprotocol/clientInfo": {"name": "example", "version": "1.0"}
    },
    "name": "read",
    "arguments": {"path": "Example.md"}
  }
}
```

```json
{"jsonrpc":"2.0","id":2,"result":{"resultType":"complete","content":[{"type":"text","text":"Example note content"}],"isError":false}}
```

Every modern request includes the version and capabilities; client identity is
optional. Capabilities are not inherited from earlier requests and do not grant
access. `{}` is sufficient for this server. Capabilities describe optional client
facilities, such as sampling an LLM or asking the user for structured input. They
are not the server's list of tools, which remains available through `tools/list`.

`resultType: "complete"` means the operation has finished, even if `isError` is
true. The protocol also supports `input_required` for a server that needs client
input before completion. This server does not request such input and never emits
that result type. It does not advertise optional resources, prompts or subscriptions.

## Discovery and version mismatch

`server/discover` uses the same `_meta` as the tool request, without `name` or
`arguments`. It returns `supportedVersions`, server capabilities and identity,
`resultType: "complete"`, `ttlMs: 0`, and `cacheScope: "private"`.
Modern `tools/list` also includes these cache-policy fields. They do not create
a note cache; this implementation does not retain note contents.

Discovery is required on the server but optional for the client. A client can use
it to populate its interface or determine whether it should use modern requests
or a legacy initialization handshake. It creates no session and is not a mandatory
first step. With an unsupported modern version, the server returns:

```json
{"jsonrpc":"2.0","id":2,"error":{"code":-32022,"message":"Unsupported protocol version for per-request metadata","data":{"requested":"2099-01-01","supported":["2026-07-28","2025-11-25","2025-06-18","2025-03-26","2024-11-05"]}}}
```

The client can retry using a mutually supported version. Legacy revisions require
their initialization handshake. If there is no common version, the client reports
incompatibility. The rejected request does not invoke Obsidian CLI.

## Cancellation, failures and state

The stdio reader remains available while one worker runs CLI operations. A
`notifications/cancelled` notification with `params.requestId` cancels queued or
running work. Running CLI processes are checked at 100 ms intervals and stopped
with bounded cleanup. No response is sent after cancellation is processed.
Cancellation does not count toward the infrastructure-failure cooldown.
At most 64 requests may be pending. Responses are correlated by ID, not order.

The 60-second cooldown, logs and in-flight cancellation bookkeeping are local
operational state. “Stateless protocol” does not prohibit this state; it means
modern requests do not depend on an earlier version/capability negotiation.

For HTTP deployments, self-contained requests let a load balancer send successive
requests to different workers without copying initialization state or keeping the
client pinned to one worker. Application data, authentication and any shared
limits still need appropriate handling. Our server is local and depends on a
running Obsidian desktop app: this migration does not make that backend distributed
or remove CLI hangs.

## Validation and rollout

Run the dependency-free suite:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

For additional independent schema validation, use a development interpreter with
`jsonschema` already installed and set `MCP_PROTOCOL_SCHEMA` to the official
2026-07-28 `schema.json`. This is not a server dependency. Tests cover modern
requests without initialization, legacy behavior, rejected versions/metadata,
restricted tool catalogs, cancellation/recovery, batches, selections and cooldowns.
Synthetic subprocess fixtures never expose Python execution as an MCP tool.

Migration uses the temporary `feat/stateless-protocol` branch, then one maintained
`main`. Tags `protocol-legacy-2026-09-22` and `protocol-2026-07-28-r1` identify the
rollback and migrated source revisions. Restart a locally configured server after
updating its source. Existing processes keep the code they loaded at startup.
Installed/downloaded package copies require a separate update; they do not follow
Git automatically. A modern-capable client chooses the new wire format; older
clients continue with the legacy handshake.

The vault-pinned local adapter shares this protocol handler while retaining its
own restricted tool catalog and vault pin. If rolling back the shared source, also
restore the adapter revision that accompanied it.

## Specification references

- [Versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
- [stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)
- [Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
- [Schema](https://modelcontextprotocol.io/specification/2026-07-28/schema)
- [Transport design rationale](https://blog.modelcontextprotocol.io/posts/2025-12-19-mcp-transport-future/)
