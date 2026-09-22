#!/usr/bin/env python3
"""Read-only MCP server for Obsidian CLI inspection commands."""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath
from typing import Any, Callable


DEFAULT_VAULT = os.environ.get("OBSIDIAN_READONLY_VAULT", "")
MAX_LIMIT = int(os.environ.get("OBSIDIAN_READONLY_MAX_LIMIT", "100"))
MAX_OUTPUT_BYTES = int(os.environ.get("OBSIDIAN_READONLY_MAX_OUTPUT_BYTES", "300000"))
TIMEOUT_SECONDS = int(os.environ.get("OBSIDIAN_READONLY_TIMEOUT_SECONDS", "30"))

if TIMEOUT_SECONDS <= 0:
    raise ValueError("OBSIDIAN_READONLY_TIMEOUT_SECONDS must be positive")

COOLDOWN_SECONDS = int(os.environ.get("OBSIDIAN_READONLY_COOLDOWN_SECONDS", "60"))
if COOLDOWN_SECONDS < 0:
    raise ValueError("OBSIDIAN_READONLY_COOLDOWN_SECONDS must be non-negative")
# Bounded per-vault infrastructure health, not conversation or note state.
FAILURES: dict[str, tuple[int, float]] = {}


def record_failure(key: str, *, trace: str | None = None) -> None:
    if not COOLDOWN_SECONDS:
        return
    count = FAILURES.get(key, (0, 0.0))[0] + 1
    until = time.monotonic() + COOLDOWN_SECONDS if count >= 2 else 0.0
    FAILURES[key] = (count, until)
    if len(FAILURES) > 64:
        del FAILURES[next(iter(FAILURES))]
    if until:
        diagnostic("cooldown_opened", trace=trace, retry_after_seconds=COOLDOWN_SECONDS)


def cooldown_remaining(key: str) -> int:
    if not COOLDOWN_SECONDS:
        return 0
    count, until = FAILURES.get(key, (0, 0.0))
    if until and until <= time.monotonic():
        FAILURES.pop(key, None)
        return 0
    return max(0, math.ceil(until - time.monotonic()))


LOGGER = logging.getLogger("obsidian-readonly-mcp")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


def configure_logging() -> None:
    """Local metadata only; never send diagnostics to MCP stdout."""
    setting = os.environ.get("OBSIDIAN_READONLY_LOG_DIR")
    if setting == "off":
        return
    directory = Path(setting) if setting else (
        Path.home() / "Library" / "Logs" / "obsidian-readonly-mcp"
        if sys.platform == "darwin" else
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "obsidian-readonly-mcp"
    )
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = directory / "server.log"
        handler = PrivateRotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        logging.raiseExceptions = False
    except OSError:
        # Avoid leaking paths or exception text and keep the protocol usable.
        sys.stderr.write("obsidian-readonly-mcp: diagnostic log unavailable\n")


def diagnostic(event: str, **fields: Any) -> None:
    LOGGER.info(json.dumps({"timestamp": time.time(), "pid": os.getpid(),
                            "event": event, **fields}))


LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = (PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS)
META_PREFIX = "io.modelcontextprotocol/"
SERVER_INFO = {"name": "obsidian-readonly", "version": "0.1.0+protocol.2026-07-28"}
REQUEST_CONTEXT = threading.local()


class RequestCancelled(Exception):
    pass


def check_cancelled() -> None:
    event = getattr(REQUEST_CONTEXT, "cancelled", None)
    if event is not None and event.is_set():
        raise RequestCancelled()


def legacy_protocol_version(requested: str) -> str:
    # Do not claim modern server/discover support by echoing an unknown revision.
    return requested if requested in LEGACY_PROTOCOL_VERSIONS else LEGACY_PROTOCOL_VERSIONS[0]


class McpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def s(args: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = args.get(key)
    if value is None:
        if required:
            raise McpError(-32602, f"{key} is required")
        return None
    if not isinstance(value, str) or value == "":
        raise McpError(-32602, f"{key} must be a non-empty string")
    return value


def b(args: dict[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise McpError(-32602, f"{key} must be a boolean")
    return value


def i(args: dict[str, Any], key: str, default: int, *, minimum: int = 1, maximum: int = MAX_LIMIT) -> int:
    value = args.get(key, default)
    if not isinstance(value, int) or value < minimum or value > maximum:
        raise McpError(-32602, f"{key} must be an integer between {minimum} and {maximum}")
    return value


def choice(args: dict[str, Any], key: str, values: set[str], default: str) -> str:
    value = args.get(key, default)
    if not isinstance(value, str) or value not in values:
        raise McpError(-32602, f"{key} must be one of {sorted(values)}")
    return value


def vault_relative(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise McpError(-32602, "path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise McpError(-32602, "path must be vault-relative and cannot contain '..'")
    return value


def target(args: dict[str, Any], *, required: bool = True) -> list[str]:
    file_name = s(args, "file")
    path = args.get("path")
    if file_name and path is not None:
        raise McpError(-32602, "provide only one of file or path")
    if file_name:
        return [f"file={file_name}"]
    if path is not None:
        return [f"path={vault_relative(path)}"]
    if required:
        raise McpError(-32602, "one of file or path is required")
    return []


def with_vault(argv: list[str], args: dict[str, Any]) -> list[str]:
    vault = s(args, "vault") or DEFAULT_VAULT
    if vault:
        return ["obsidian", f"vault={vault}", *argv]
    return ["obsidian", *argv]


def append_bool(argv: list[str], args: dict[str, Any], *names: str) -> None:
    for name in names:
        if b(args, name):
            argv.append(name.replace("_", "-") if name == "newtab" else name)


def build_argv(tool: str, args: dict[str, Any]) -> list[str]:
    if tool == "search":
        argv = ["search", f"query={s(args, 'query', required=True)}", f"limit={i(args, 'limit', 10)}", f"format={choice(args, 'format', {'text', 'json'}, 'text')}"]
        if args.get("path") is not None:
            argv.append(f"path={vault_relative(args['path'])}")
        if b(args, "case_sensitive"):
            argv.append("case")
        append_bool(argv, args, "total")
        return with_vault(argv, args)

    if tool == "search_context":
        argv = ["search:context", f"query={s(args, 'query', required=True)}", f"limit={i(args, 'limit', 5)}", f"format={choice(args, 'format', {'text', 'json'}, 'text')}"]
        if args.get("path") is not None:
            argv.append(f"path={vault_relative(args['path'])}")
        if b(args, "case_sensitive"):
            argv.append("case")
        return with_vault(argv, args)

    if tool in {"read", "file"}:
        return with_vault([tool, *target(args)], args)

    if tool == "daily_read":
        return with_vault(["daily:read"], args)

    if tool == "daily_path":
        return with_vault(["daily:path"], args)

    if tool == "backlinks":
        argv = ["backlinks", *target(args), f"format={choice(args, 'format', {'json', 'tsv', 'csv'}, 'json')}"]
        append_bool(argv, args, "counts", "total")
        return with_vault(argv, args)

    if tool == "links":
        argv = ["links", *target(args)]
        append_bool(argv, args, "total")
        return with_vault(argv, args)

    if tool == "outline":
        argv = ["outline", *target(args), f"format={choice(args, 'format', {'tree', 'md', 'json'}, 'json')}"]
        append_bool(argv, args, "total")
        return with_vault(argv, args)

    if tool == "aliases":
        argv = ["aliases", *target(args, required=False), f"format={choice(args, 'format', {'json', 'tsv', 'csv'}, 'tsv')}"]
        append_bool(argv, args, "total", "verbose", "active")
        return with_vault(argv, args)

    if tool == "tags":
        argv = ["tags", *target(args, required=False), f"format={choice(args, 'format', {'json', 'tsv', 'csv'}, 'tsv')}"]
        append_bool(argv, args, "total", "counts", "active")
        sort = s(args, "sort")
        if sort:
            if sort != "count":
                raise McpError(-32602, "sort must be count")
            argv.append("sort=count")
        return with_vault(argv, args)

    if tool == "tag":
        argv = ["tag", f"name={s(args, 'name', required=True)}"]
        append_bool(argv, args, "total", "verbose")
        return with_vault(argv, args)

    if tool == "unresolved":
        argv = ["unresolved", f"format={choice(args, 'format', {'json', 'tsv', 'csv'}, 'tsv')}"]
        append_bool(argv, args, "total", "counts", "verbose")
        return with_vault(argv, args)

    if tool == "properties":
        argv = ["properties", *target(args, required=False), f"format={choice(args, 'format', {'yaml', 'json', 'tsv'}, 'yaml')}"]
        name = s(args, "name")
        if name:
            argv.append(f"name={name}")
        sort = s(args, "sort")
        if sort:
            if sort != "count":
                raise McpError(-32602, "sort must be count")
            argv.append("sort=count")
        append_bool(argv, args, "total", "counts", "active")
        return with_vault(argv, args)

    if tool == "property_read":
        return with_vault(["property:read", f"name={s(args, 'name', required=True)}", *target(args)], args)

    if tool == "files":
        argv = ["files"]
        folder = args.get("folder")
        if folder is not None:
            argv.append(f"folder={vault_relative(folder)}")
        ext = s(args, "ext")
        if ext:
            argv.append(f"ext={ext}")
        append_bool(argv, args, "total")
        return with_vault(argv, args)

    if tool == "folders":
        argv = ["folders"]
        folder = args.get("folder")
        if folder is not None:
            argv.append(f"folder={vault_relative(folder)}")
        append_bool(argv, args, "total")
        return with_vault(argv, args)

    if tool == "folder":
        argv = ["folder", f"path={vault_relative(args.get('path'))}"]
        info = s(args, "info")
        if info:
            if info not in {"files", "folders", "size"}:
                raise McpError(-32602, "info must be files, folders, or size")
            argv.append(f"info={info}")
        return with_vault(argv, args)

    if tool == "wordcount":
        argv = ["wordcount", *target(args)]
        append_bool(argv, args, "words", "characters")
        return with_vault(argv, args)

    if tool in {"deadends", "orphans"}:
        argv = [tool]
        append_bool(argv, args, "total", "all")
        return with_vault(argv, args)

    if tool in {"bases", "base_views", "bookmarks", "history_list", "hotkeys", "plugins", "plugins_enabled", "templates", "themes", "vaults", "workspaces"}:
        command = {
            "base_views": "base:views",
            "history_list": "history:list",
            "plugins_enabled": "plugins:enabled",
        }.get(tool, tool)
        argv = [command]
        if tool == "bookmarks":
            argv.append(f"format={choice(args, 'format', {'json', 'tsv', 'csv'}, 'tsv')}")
            append_bool(argv, args, "total", "verbose")
        elif tool == "hotkeys":
            argv.append(f"format={choice(args, 'format', {'json', 'tsv', 'csv'}, 'tsv')}")
            append_bool(argv, args, "total", "verbose", "all")
        elif tool in {"plugins", "plugins_enabled"}:
            argv.append(f"format={choice(args, 'format', {'json', 'tsv', 'csv'}, 'tsv')}")
            filter_value = s(args, "filter")
            if filter_value:
                if filter_value not in {"core", "community"}:
                    raise McpError(-32602, "filter must be core or community")
                argv.append(f"filter={filter_value}")
            append_bool(argv, args, "versions")
        elif tool in {"vaults", "workspaces", "templates", "bases"}:
            append_bool(argv, args, "total")
            if tool == "vaults":
                append_bool(argv, args, "verbose")
        elif tool == "themes":
            append_bool(argv, args, "versions")
        return with_vault(argv, args)

    if tool == "base_query":
        argv = ["base:query", *target(args), f"format={choice(args, 'format', {'json', 'csv', 'tsv', 'md', 'paths'}, 'json')}"]
        view = s(args, "view")
        if view:
            argv.append(f"view={view}")
        return with_vault(argv, args)

    if tool in {"diff", "history"}:
        argv = [tool, *target(args)]
        if tool == "diff":
            if args.get("from_version") is not None:
                argv.append(f"from={i(args, 'from_version', 1, minimum=1, maximum=1000000)}")
            if args.get("to_version") is not None:
                argv.append(f"to={i(args, 'to_version', 1, minimum=1, maximum=1000000)}")
            filter_value = s(args, "filter")
            if filter_value:
                if filter_value not in {"local", "sync"}:
                    raise McpError(-32602, "filter must be local or sync")
                argv.append(f"filter={filter_value}")
        return with_vault(argv, args)

    if tool == "history_read":
        return with_vault(["history:read", *target(args), f"version={i(args, 'version', 1, minimum=1, maximum=1000000)}"], args)

    if tool == "hotkey":
        argv = ["hotkey", f"id={s(args, 'id', required=True)}"]
        append_bool(argv, args, "verbose")
        return with_vault(argv, args)

    if tool == "plugin":
        return with_vault(["plugin", f"id={s(args, 'id', required=True)}"], args)

    if tool == "theme":
        name = s(args, "name")
        argv = ["theme"]
        if name:
            argv.append(f"name={name}")
        return with_vault(argv, args)

    if tool == "template_read":
        argv = ["template:read", f"name={s(args, 'name', required=True)}"]
        if b(args, "resolve"):
            argv.append("resolve")
        title = s(args, "title")
        if title:
            argv.append(f"title={title}")
        return with_vault(argv, args)

    if tool == "random_read":
        argv = ["random:read"]
        folder = args.get("folder")
        if folder is not None:
            argv.append(f"folder={vault_relative(folder)}")
        return with_vault(argv, args)

    if tool == "tasks":
        argv = ["tasks", *target(args, required=False), f"format={choice(args, 'format', {'json', 'tsv', 'csv'}, 'tsv')}"]
        status = s(args, "status")
        if status:
            argv.append(f"status={status}")
        append_bool(argv, args, "total", "done", "todo", "verbose", "active", "daily")
        return with_vault(argv, args)

    if tool == "vault":
        argv = ["vault"]
        info = s(args, "info")
        if info:
            if info not in {"name", "path", "files", "folders", "size"}:
                raise McpError(-32602, "info must be name, path, files, folders, or size")
            argv.append(f"info={info}")
        return with_vault(argv, args)

    if tool == "version":
        return with_vault(["version"], args)

    if tool == "recents":
        argv = ["recents"]
        append_bool(argv, args, "total")
        return with_vault(argv, args)

    if tool in {"snippets", "snippets_enabled"}:
        command = "snippets:enabled" if tool == "snippets_enabled" else "snippets"
        return with_vault([command], args)

    if tool == "tabs":
        argv = ["tabs"]
        append_bool(argv, args, "ids")
        return with_vault(argv, args)

    if tool == "workspace":
        argv = ["workspace"]
        append_bool(argv, args, "ids")
        return with_vault(argv, args)

    if tool == "commands":
        argv = ["commands"]
        prefix = s(args, "filter")
        if prefix:
            argv.append(f"filter={prefix}")
        return with_vault(argv, args)

    if tool == "help":
        command = s(args, "command")
        return with_vault(["help", *([command] if command else [])], args)

    if tool == "dev_console":
        argv = ["dev:console", f"limit={i(args, 'limit', 50, minimum=1, maximum=500)}"]
        level = s(args, "level")
        if level:
            if level not in {"log", "warn", "error", "info", "debug"}:
                raise McpError(-32602, "level must be log, warn, error, info, or debug")
            argv.append(f"level={level}")
        return with_vault(argv, args)

    if tool == "dev_errors":
        return with_vault(["dev:errors"], args)

    if tool == "dev_css":
        argv = ["dev:css", f"selector={s(args, 'selector', required=True)}"]
        prop = s(args, "prop")
        if prop:
            argv.append(f"prop={prop}")
        return with_vault(argv, args)

    if tool == "dev_dom":
        argv = ["dev:dom", f"selector={s(args, 'selector', required=True)}"]
        attr = s(args, "attr")
        css = s(args, "css")
        if attr and css:
            raise McpError(-32602, "provide only one of attr or css")
        if attr:
            argv.append(f"attr={attr}")
        if css:
            argv.append(f"css={css}")
        append_bool(argv, args, "total", "text", "inner", "all")
        return with_vault(argv, args)

    raise McpError(-32601, f"unknown tool: {tool}")


READ_SELECTION = {
    "heading": {"type": "string", "description": "Exact ATX (#) Markdown heading text; includes subsections. Duplicates require a line range."},
    "start_line": {"type": "integer", "minimum": 1, "description": "First line, 1-based inclusive."},
    "end_line": {"type": "integer", "minimum": 1, "description": "Last line, 1-based inclusive; clipped to EOF."},
}


def validate_selection(args: dict[str, Any]) -> None:
    if "heading" in args:
        if not isinstance(args["heading"], str) or not args["heading"] or any(k in args for k in ("start_line", "end_line")):
            raise McpError(-32602, "Provide heading OR a line range")
    for key in ("start_line", "end_line"):
        if key in args and (type(args[key]) is not int or args[key] < 1):
            raise McpError(-32602, "Line numbers must be positive integers")
    if "end_line" in args and args["end_line"] < args.get("start_line", 1):
        raise McpError(-32602, "end_line precedes start_line")


def select_text(text: str, args: dict[str, Any]) -> str:
    validate_selection(args)
    lines = text.splitlines(keepends=True)
    heading = args.get('heading')
    start, end = args.get('start_line', 1), args.get('end_line', len(lines))
    if heading is not None:
        headings = []
        fence = None
        frontmatter = bool(lines and lines[0].strip() == "---")
        for index, line in enumerate(lines):
            if frontmatter:
                if index > 0 and line.strip() in {"---", "..."}:
                    frontmatter = False
                continue
            mark = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
            if mark:
                token = mark[1]
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence) and not line[mark.end():].strip():
                    fence = None
                continue
            if fence is not None:
                continue
            match = re.match(r'^ {0,3}(#{1,6})[ \t]+(.+?)\s*$', line)
            if match:
                title = re.sub(r'[ \t]+#+[ \t]*$', '', match[2])
                headings.append((index, len(match[1]), title))
        found = [h for h in headings if h[2] == heading]
        if len(found) != 1:
            raise McpError(-32602, 'Heading missing or ambiguous; use outline or line range')
        index, level, _ = found[0]
        start = index + 1
        end = next((pos for pos, depth, _ in headings if pos > index and depth <= level), len(lines))
    if type(start) is not int or type(end) is not int or start < 1 or end < start or start > len(lines):
        raise McpError(-32602, 'Invalid 1-based inclusive line range')
    end = min(end, len(lines))
    return f'[lines {start}-{end} of {len(lines)}]\n' + ''.join(lines[start-1:end])


def run_batch(args: dict[str, Any], *, invoke: Callable[..., Any], allowed: set[str],
              trace: str | None = None) -> tuple[str, bool]:
    if set(args) - {"calls"}:
        raise McpError(-32602, "Set arguments on each batch call, not on the batch")
    calls = args.get("calls")
    if not isinstance(calls, list) or not 1 <= len(calls) <= 8:
        raise McpError(-32602, "batch requires 1-8 calls")
    for call in calls:
        if (not isinstance(call, dict) or not isinstance(call.get("tool"), str)
                or call["tool"] not in allowed - {"batch"}
                or not isinstance(call.get("arguments", {}), dict)):
            raise McpError(-32602, "Invalid or nested batch call")
    deadline = time.monotonic() + TIMEOUT_SECONDS
    results = []
    output_exhausted = False
    for index, call in enumerate(calls):
        remaining = deadline - time.monotonic()
        if output_exhausted:
            text, failed = "Batch output budget exhausted; call separately", True
        elif remaining <= 0:
            text, failed = "Batch time budget exhausted; call separately", True
        else:
            try:
                text, failed = invoke(call["tool"], call.get("arguments", {}),
                                      trace=f"{trace}:{index}", timeout_seconds=remaining)
            except McpError as exc:
                text, failed = exc.message, True
        entry = {"tool": call["tool"], "text": text, "isError": failed}
        # Measure the serialized envelope too; never silently drop a requested result.
        if len(json.dumps(results + [entry]).encode()) > MAX_OUTPUT_BYTES - 2048:
            entry.update(text="Batch output budget exhausted; call separately", isError=True)
            output_exhausted = True
        results.append(entry)
    return json.dumps(results), any(item["isError"] for item in results)


def call_tool(name: str, args: dict[str, Any], *, trace: str | None = None,
              timeout_seconds: float | None = None) -> tuple[str, bool]:
    if name == "batch":
        return run_batch(args, invoke=call_tool, allowed={tool["name"] for tool in TOOLS}, trace=trace)
    if name == "health":
        return health(args, trace=trace, timeout_seconds=timeout_seconds)
    return run_obsidian(name, args, trace=trace, timeout_seconds=timeout_seconds)


def run_obsidian(tool: str, args: dict[str, Any], *, trace: str | None = None, timeout_seconds: float | None = None, probe: bool = False) -> tuple[str, bool]:
    if tool == "read":
        validate_selection(args)
    text, failed = run_command(build_argv(tool, args), tool=tool, trace=trace,
                               timeout_seconds=timeout_seconds, probe=probe)
    if tool == "read" and not failed and any(key in args for key in READ_SELECTION):
        if "[obsidian-readonly-mcp: output truncated]" in text:
            return "Cannot select from a truncated source; narrow the query or adjust the configured output limit", True
        text = select_text(text, args)
    return text, failed


def run_command(argv: list[str], *, tool: str, trace: str | None = None, timeout_seconds: float | None = None, probe: bool = False) -> tuple[str, bool]:
    """Execute validated argv; shared with vault-specific read-only adapters."""
    check_cancelled()
    key = next((arg for arg in argv[1:] if arg.startswith("vault=")), "default-vault")
    remaining = cooldown_remaining(key)
    if remaining and not probe:
        diagnostic("cooldown_rejected", trace=trace, tool=tool, retry_after_seconds=remaining)
        return json.dumps({"status": "cooldown", "retry_after_seconds": remaining,
                           "message": "Obsidian is temporarily unavailable. Stop retries until the cooldown expires; after fixing the connection, use one health check. No fallback was run."}), True
    timeout = TIMEOUT_SECONDS if timeout_seconds is None else min(TIMEOUT_SECONDS, timeout_seconds)
    started = time.monotonic()
    diagnostic("command_started", trace=trace, tool=tool, timeout_seconds=timeout)
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=(os.name == "posix"))
    except OSError as exc:
        record_failure(key, trace=trace)
        diagnostic("command_failed", trace=trace, tool=tool, error_type=type(exc).__name__)
        return ("obsidian CLI not found on PATH" if isinstance(exc, FileNotFoundError)
                else "obsidian CLI could not be started"), True
    try:
        if getattr(REQUEST_CONTEXT, "cancelled", None) is None:
            stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
        else:
            deadline = time.monotonic() + timeout
            while True:
                check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    stdout_bytes, stderr_bytes = process.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            check_cancelled()
    except (subprocess.TimeoutExpired, RequestCancelled) as exc:
        cancelled = isinstance(exc, RequestCancelled)
        if not cancelled:
            record_failure(key, trace=trace)
        diagnostic("command_cancelled" if cancelled else "command_timeout", trace=trace, tool=tool,
                   elapsed_ms=round((time.monotonic() - started) * 1000))
        # Kill only the CLI's new process group, including helpers holding its pipes.
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        # Do not call communicate() without a timeout: inherited pipes can stay open.
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        diagnostic("command_cleanup", trace=trace, tool=tool, exit_code=process.poll(),
                   elapsed_ms=round((time.monotonic() - started) * 1000))
        if cancelled:
            raise
        return f"obsidian command timed out after {timeout:g}s; check Obsidian and run health", True

    diagnostic("command_finished", trace=trace, tool=tool, exit_code=process.returncode,
               elapsed_ms=round((time.monotonic() - started) * 1000),
               stdout_bytes=len(stdout_bytes), stderr_bytes=len(stderr_bytes))
    stdout = stdout_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr = stderr_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    text = stdout
    if stderr:
        text = f"{text}\n[stderr]\n{stderr}" if text else stderr
    if len(stdout_bytes) > MAX_OUTPUT_BYTES or len(stderr_bytes) > MAX_OUTPUT_BYTES:
        text += "\n[obsidian-readonly-mcp: output truncated]\n"
    failed = process.returncode != 0 or stdout.lstrip().startswith("Error:")
    lookup_error = stdout.lstrip().startswith(("Error: File ", "Error: Folder ", "Error: Property ", "Error: Heading "))
    if (failed and not lookup_error) or (probe and not stdout.strip()):
        record_failure(key, trace=trace)
    else:
        FAILURES.pop(key, None)
    return text, failed


def health(args: dict[str, Any], *, trace: str | None = None, timeout_seconds: float | None = None) -> tuple[str, bool]:
    """Probe an actual app/vault command, not merely the CLI executable version."""
    started = time.monotonic()
    text, failed = run_obsidian("vault", {**args, "info": "name"}, trace=trace, timeout_seconds=timeout_seconds, probe=True)
    # Some CLI failures can be printed with exit status zero.
    responsive = not failed and bool(text.strip()) and not text.lstrip().lower().startswith("error")
    result = {"server": "ok", "obsidian_vault": "responsive" if responsive else "unavailable",
              "timeout_seconds": TIMEOUT_SECONDS,
              "elapsed_ms": round((time.monotonic() - started) * 1000)}
    if not responsive:
        result["detail"] = text or "Obsidian returned an empty response"
    return json.dumps(result), not responsive


def input_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    props = {"vault": {"type": "string", "description": "Optional Obsidian vault name; defaults to OBSIDIAN_READONLY_VAULT."}}
    props.update(properties)
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


STRING = {"type": "string"}
TARGET = {"file": STRING, "path": STRING}
FORMAT_TEXT_JSON = {"format": {"type": "string", "enum": ["text", "json"], "default": "text"}}
FORMAT_TRIPLE = {"format": {"type": "string", "enum": ["json", "tsv", "csv"], "default": "tsv"}}
BOOLS = {
    "total": {"type": "boolean", "default": False},
    "counts": {"type": "boolean", "default": False},
    "verbose": {"type": "boolean", "default": False},
}


def tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": input_schema(properties, required),
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}}


TOOLS = [
    tool("health", "Check the MCP server and Obsidian vault connection within the command timeout.", {}),
    tool("search", "Read-only Obsidian keyword search.", {"query": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 10}, "path": STRING, **FORMAT_TEXT_JSON, "case_sensitive": {"type": "boolean", "default": False}, "total": BOOLS["total"]}, ["query"]),
    tool("search_context", "Read-only Obsidian search with surrounding line context.", {"query": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 5}, "path": STRING, **FORMAT_TEXT_JSON, "case_sensitive": {"type": "boolean", "default": False}}, ["query"]),
    tool("read", "Read a note. For long notes, use outline then heading, or start_line/end_line, to return only the needed portion.", {**TARGET, **READ_SELECTION}),
    tool("daily_read", "Read the daily note.", {}),
    tool("daily_path", "Return the daily note path.", {}),
    tool("backlinks", "List backlinks to a note.", {**TARGET, "counts": {"type": "boolean", "default": True}, "total": BOOLS["total"], "format": {"type": "string", "enum": ["json", "tsv", "csv"], "default": "json"}}),
    tool("links", "List outgoing links from a note.", {**TARGET, "total": BOOLS["total"]}),
    tool("outline", "Return note headings / outline.", {**TARGET, "format": {"type": "string", "enum": ["tree", "md", "json"], "default": "json"}, "total": BOOLS["total"]}),
    tool("aliases", "List aliases.", {**TARGET, **FORMAT_TRIPLE, **BOOLS, "active": {"type": "boolean", "default": False}}),
    tool("tags", "List tags.", {**TARGET, **FORMAT_TRIPLE, "sort": {"type": "string", "enum": ["count"]}, **BOOLS, "active": {"type": "boolean", "default": False}}),
    tool("tag", "Get tag info.", {"name": STRING, "total": BOOLS["total"], "verbose": BOOLS["verbose"]}, ["name"]),
    tool("unresolved", "List unresolved links.", {**FORMAT_TRIPLE, **BOOLS}),
    tool("properties", "List properties.", {**TARGET, "name": STRING, "sort": {"type": "string", "enum": ["count"]}, "format": {"type": "string", "enum": ["yaml", "json", "tsv"], "default": "yaml"}, **BOOLS, "active": {"type": "boolean", "default": False}}),
    tool("property_read", "Read a property value from a note.", {**TARGET, "name": STRING}, ["name"]),
    tool("file", "Show file info.", TARGET),
    tool("files", "List files.", {"folder": STRING, "ext": STRING, "total": BOOLS["total"]}),
    tool("folder", "Show folder info.", {"path": STRING, "info": {"type": "string", "enum": ["files", "folders", "size"]}}, ["path"]),
    tool("folders", "List folders.", {"folder": STRING, "total": BOOLS["total"]}),
    tool("wordcount", "Count words and characters.", {**TARGET, "words": {"type": "boolean", "default": False}, "characters": {"type": "boolean", "default": False}}),
    tool("deadends", "List files with no outgoing links.", {"total": BOOLS["total"], "all": {"type": "boolean", "default": False}}),
    tool("orphans", "List files with no incoming links.", {"total": BOOLS["total"], "all": {"type": "boolean", "default": False}}),
    tool("bases", "List base files.", {"total": BOOLS["total"]}),
    tool("base_query", "Query a base view.", {**TARGET, "view": STRING, "format": {"type": "string", "enum": ["json", "csv", "tsv", "md", "paths"], "default": "json"}}),
    tool("base_views", "List views in the current base file.", {}),
    tool("bookmarks", "List bookmarks.", {**FORMAT_TRIPLE, **BOOLS}),
    tool("templates", "List templates.", {"total": BOOLS["total"]}),
    tool("template_read", "Read a template.", {"name": STRING, "resolve": {"type": "boolean", "default": False}, "title": STRING}, ["name"]),
    tool("random_read", "Read a random note without opening it.", {"folder": STRING}),
    tool("tasks", "List tasks.", {**TARGET, "format": {"type": "string", "enum": ["json", "tsv", "csv"], "default": "tsv"}, "status": STRING, "total": BOOLS["total"], "done": {"type": "boolean", "default": False}, "todo": {"type": "boolean", "default": False}, "verbose": BOOLS["verbose"], "active": {"type": "boolean", "default": False}, "daily": {"type": "boolean", "default": False}}),
    tool("diff", "List or diff local/sync versions.", {**TARGET, "from_version": {"type": "integer", "minimum": 1}, "to_version": {"type": "integer", "minimum": 1}, "filter": {"type": "string", "enum": ["local", "sync"]}}),
    tool("history", "List file history versions.", TARGET),
    tool("history_list", "List files with history.", {}),
    tool("history_read", "Read a file history version.", {**TARGET, "version": {"type": "integer", "minimum": 1, "default": 1}}),
    tool("hotkey", "Get hotkey for a command.", {"id": STRING, "verbose": BOOLS["verbose"]}, ["id"]),
    tool("hotkeys", "List hotkeys.", {**FORMAT_TRIPLE, "total": BOOLS["total"], "verbose": BOOLS["verbose"], "all": {"type": "boolean", "default": False}}),
    tool("plugins", "List installed plugins.", {**FORMAT_TRIPLE, "filter": {"type": "string", "enum": ["core", "community"]}, "versions": {"type": "boolean", "default": False}}),
    tool("plugin", "Get plugin info.", {"id": STRING}, ["id"]),
    tool("plugins_enabled", "List enabled plugins.", {**FORMAT_TRIPLE, "filter": {"type": "string", "enum": ["core", "community"]}, "versions": {"type": "boolean", "default": False}}),
    tool("theme", "Show active theme or theme info.", {"name": STRING}),
    tool("themes", "List installed themes.", {"versions": {"type": "boolean", "default": False}}),
    tool("vault", "Show vault info.", {"info": {"type": "string", "enum": ["name", "path", "files", "folders", "size"]}}),
    tool("vaults", "List known vaults.", {"total": BOOLS["total"], "verbose": BOOLS["verbose"]}),
    tool("version", "Show Obsidian version.", {}),
    tool("recents", "List recently opened files.", {"total": BOOLS["total"]}),
    tool("snippets", "List installed CSS snippets.", {}),
    tool("snippets_enabled", "List enabled CSS snippets.", {}),
    tool("tabs", "List open tabs.", {"ids": {"type": "boolean", "default": False}}),
    tool("workspace", "Show workspace tree.", {"ids": {"type": "boolean", "default": False}}),
    tool("workspaces", "List saved workspaces.", {"total": BOOLS["total"]}),
    tool("commands", "List available commands.", {"filter": STRING}),
    tool("help", "Show Obsidian CLI help.", {"command": STRING}),
    tool("dev_console", "Show captured console messages.", {"limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}, "level": {"type": "string", "enum": ["log", "warn", "error", "info", "debug"]}}),
    tool("dev_errors", "Show captured errors.", {}),
    tool("dev_css", "Inspect CSS with source locations.", {"selector": STRING, "prop": STRING}, ["selector"]),
    tool("dev_dom", "Query DOM elements.", {"selector": STRING, "total": BOOLS["total"], "text": {"type": "boolean", "default": False}, "inner": {"type": "boolean", "default": False}, "all": {"type": "boolean", "default": False}, "attr": STRING, "css": STRING}, ["selector"]),
]


TOOLS.append(tool("batch", "Run 1-8 independent read-only calls in one round trip, with separate results and a shared command timeout. No nested batches.", {
    "calls": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
        "type": "object", "properties": {
            "tool": {"type": "string", "enum": [item["name"] for item in TOOLS]},
            "arguments": {"type": "object"}}, "required": ["tool", "arguments"], "additionalProperties": False}}}, ["calls"]))

TOOLS[-1]["inputSchema"]["properties"].pop("vault")


def write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def modern_request(method: str, params: dict[str, Any]) -> bool:
    """Validate each modern request independently; retain legacy wire support."""
    meta = params.get("_meta", {})
    if not isinstance(meta, dict):
        raise McpError(-32602, "_meta must be an object")
    modern = method == "server/discover" or any(
        META_PREFIX + key in meta for key in ("protocolVersion", "clientCapabilities", "clientInfo", "logLevel"))
    if not modern:
        return False
    version = meta.get(META_PREFIX + "protocolVersion")
    if not isinstance(version, str):
        raise McpError(-32602, "Modern requests require protocolVersion in _meta")
    if version != PROTOCOL_VERSION:
        raise McpError(-32022, "Unsupported protocol version for per-request metadata",
                       {"supported": list(SUPPORTED_VERSIONS), "requested": version})
    capabilities = meta.get(META_PREFIX + "clientCapabilities")
    if not isinstance(capabilities, dict):
        raise McpError(-32602, "Modern requests require object clientCapabilities in _meta")
    # Unknown capabilities are permitted; known ones must have their specified shape.
    for key in ("roots", "sampling", "elicitation", "experimental", "extensions"):
        if key not in capabilities:
            continue
        value = capabilities[key]
        if not isinstance(value, dict):
            raise McpError(-32602, f"clientCapabilities.{key} must be an object")
        children = {"sampling": ("context", "tools"), "elicitation": ("form", "url")}.get(key, ())
        if key in {"experimental", "extensions"}:
            children = value.keys()
        if any(not isinstance(value[child], dict) for child in children if child in value):
            raise McpError(-32602, f"Invalid clientCapabilities.{key} settings")
    info = meta.get(META_PREFIX + "clientInfo")
    if META_PREFIX + "clientInfo" in meta and (
            not isinstance(info, dict) or any(not isinstance(info.get(k), str) for k in ("name", "version"))):
        raise McpError(-32602, "clientInfo requires string name and version")
    return True


def handle_request(message: dict[str, Any], *, trace: str | None = None,
                   tools: list[dict[str, Any]] | None = None,
                   invoke: Callable[..., Any] | None = None) -> dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    try:
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise McpError(-32602, "params must be an object")
        modern = modern_request(method, params)
        catalog = TOOLS if tools is None else tools
        if method == "initialize" and not modern:
            version = legacy_protocol_version(params.get("protocolVersion", "2024-11-05"))
            result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
        elif method == "ping" and not modern:
            result = {}
        elif method == "server/discover":
            result = {"supportedVersions": list(SUPPORTED_VERSIONS), "capabilities": {"tools": {}},
                      "_meta": {META_PREFIX + "serverInfo": SERVER_INFO}, "ttlMs": 0, "cacheScope": "private"}
        elif method == "tools/list":
            result = {"tools": catalog}
            if modern:
                result.update(ttlMs=0, cacheScope="private")
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(args, dict):
                raise McpError(-32602, "tools/call requires name and object arguments")
            if name not in {item["name"] for item in catalog}:
                raise McpError(-32602, f"unknown tool: {name}")
            text, is_error = (invoke or call_tool)(name, args, trace=trace)
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        else:
            raise McpError(-32601, f"unknown method: {method}")
        if modern:
            result["resultType"] = "complete"
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except McpError as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}
    except RequestCancelled:
        raise
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}


def main(request_handler: Callable[..., Any] = handle_request) -> int:
    configure_logging()
    diagnostic("server_started", timeout_seconds=TIMEOUT_SECONDS)
    # One CLI worker preserves ordering and backend health accounting. The reader
    # remains available for cancellation; modern metadata is never retained.
    work = queue.Queue(maxsize=64)
    pending: dict[Any, threading.Event] = {}
    lock = threading.Lock()

    def respond(response, trace, started):
        if response is not None:
            write_message(response)
            diagnostic("response_sent", trace=trace,
                       failed=("error" in response or response.get("result", {}).get("isError", False)),
                       elapsed_ms=round((time.monotonic() - started) * 1000))

    def worker():
        legacy_initialized = False
        while True:
            item = work.get()
            if item is None:
                return
            message, trace, started, cancelled = item
            request_id = message["id"]
            REQUEST_CONTEXT.cancelled = cancelled
            try:
                check_cancelled()
                if not legacy_initialized and message["method"] not in {"initialize", "ping"}:
                    params = message.get("params", {})
                    if not isinstance(params, dict) or not modern_request(message["method"], params):
                        raise McpError(-32602, "Provide modern request metadata or initialize a legacy connection")
                response = request_handler(message, trace=trace)
                if message["method"] == "initialize" and response is not None and "result" in response:
                    legacy_initialized = True
                with lock:
                    if not cancelled.is_set():
                        respond(response, trace, started)
            except McpError as exc:
                error = {"code": exc.code, "message": exc.message}
                if exc.data is not None:
                    error["data"] = exc.data
                with lock:
                    if not cancelled.is_set():
                        respond({"jsonrpc": "2.0", "id": request_id, "error": error}, trace, started)
            except RequestCancelled:
                diagnostic("request_cancelled", trace=trace)
            finally:
                with lock:
                    pending.pop(request_id, None)
                del REQUEST_CONTEXT.cancelled

    thread = threading.Thread(target=worker, name="obsidian-cli-worker", daemon=True)
    thread.start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        trace = uuid.uuid4().hex
        started = time.monotonic()
        diagnostic("request_received", trace=trace)
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            with lock:
                respond({"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "Invalid JSON"}}, trace, started)
            continue
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)
                or ("id" in message and type(message["id"]) not in (str, int))):
            with lock:
                respond({"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}, trace, started)
            continue
        if "id" not in message:
            params = message.get("params", {})
            if message["method"] == "notifications/cancelled" and isinstance(params, dict):
                target = params.get("requestId")
                if type(target) in (str, int):
                    with lock:
                        event = pending.get(target)
                        if event is not None:
                            event.set()
            diagnostic("notification_processed", trace=trace)
            continue
        with lock:
            request_id = message["id"]
            if request_id in pending or len(pending) >= 64:
                respond({"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": -32600, "message": "Duplicate ID or too many pending requests"}}, trace, started)
                continue
            event = threading.Event()
            pending[request_id] = event
            work.put_nowait((message, trace, started, event))
    work.put(None)
    thread.join()
    diagnostic("server_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
