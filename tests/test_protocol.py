"""Wire compatibility, cancellation and tool-boundary regression tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import obsidian_readonly_mcp as server


def request(method, **params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {
        "_meta": {server.META_PREFIX + "protocolVersion": server.PROTOCOL_VERSION,
                  server.META_PREFIX + "clientCapabilities": {}}, **params}}


class ProtocolTest(unittest.TestCase):
    def test_modern_tools_work_without_initialize(self):
        invoke = Mock(return_value=("synthetic note", False))
        reply = server.handle_request(request("tools/call", name="read", arguments={"path": "x.md"}), invoke=invoke)
        self.assertEqual(reply["result"], {"resultType": "complete", "content": [{"type": "text", "text": "synthetic note"}], "isError": False})
        invoke.assert_called_once_with("read", {"path": "x.md"}, trace=None)

    def test_discovery_and_list_include_cache_policy(self):
        for method in ("server/discover", "tools/list"):
            result = server.handle_request(request(method))["result"]
            self.assertEqual(result["resultType"], "complete")
            self.assertEqual(result["ttlMs"], 0)
            self.assertEqual(result["cacheScope"], "private")
        self.assertIn(server.PROTOCOL_VERSION, server.handle_request(request("server/discover"))["result"]["supportedVersions"])

    def test_legacy_negotiation_and_results_stay_compatible(self):
        for version in (*server.LEGACY_PROTOCOL_VERSIONS, "2099-01-01"):
            result = server.handle_request({"id": 1, "method": "initialize", "params": {"protocolVersion": version}})["result"]
            self.assertEqual(result["protocolVersion"], version if version in server.LEGACY_PROTOCOL_VERSIONS else server.LEGACY_PROTOCOL_VERSIONS[0])
            self.assertNotIn("resultType", result)
        self.assertNotIn("resultType", server.handle_request({"id": 1, "method": "tools/list"})["result"])
        self.assertIsNone(server.handle_request({"method": "notifications/initialized"}))

    def test_version_mismatch_never_invokes_cli(self):
        message = request("tools/call", name="read", arguments={"path": "x.md"})
        message["params"]["_meta"][server.META_PREFIX + "protocolVersion"] = "2099-01-01"
        with patch.object(server.subprocess, "Popen") as launch:
            error = server.handle_request(message)["error"]
            launch.assert_not_called()
        self.assertEqual(error["code"], -32022)
        self.assertEqual(error["data"], {"requested": "2099-01-01", "supported": list(server.SUPPORTED_VERSIONS)})

    def test_capabilities_are_per_request_and_not_authorization(self):
        message = request("tools/list")
        message["params"]["_meta"][server.META_PREFIX + "clientCapabilities"] = {"sampling": {"tools": {}}, "custom": True}
        self.assertIn("result", server.handle_request(message))
        del message["params"]["_meta"][server.META_PREFIX + "clientCapabilities"]
        self.assertEqual(server.handle_request(message)["error"]["code"], -32602)
        for caps in (None, [], {"sampling": True}, {"sampling": {"tools": True}}):
            message["params"]["_meta"][server.META_PREFIX + "clientCapabilities"] = caps
            self.assertEqual(server.handle_request(message)["error"]["code"], -32602)

    def test_invalid_metadata_and_params(self):
        for params in ([], None, {"_meta": []}, {"_meta": {}}, {"_meta": {server.META_PREFIX + "clientCapabilities": {}}}):
            self.assertEqual(server.handle_request({"id": 1, "method": "server/discover", "params": params})["error"]["code"], -32602)
        message = request("tools/list")
        message["params"]["_meta"][server.META_PREFIX + "clientInfo"] = {"name": "test"}
        self.assertEqual(server.handle_request(message)["error"]["code"], -32602)

    def test_tool_error_is_still_complete(self):
        result = server.handle_request(request("tools/call", name="read", arguments={}), invoke=lambda *a, **k: ("unavailable", True))["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["resultType"], "complete")

    def test_pinned_adapter_catalog_is_respected(self):
        invoke = Mock(return_value=("pinned", False))
        catalog = [next(t for t in server.TOOLS if t["name"] == "read")]
        self.assertEqual(server.handle_request(request("tools/list"), tools=catalog)["result"]["tools"], catalog)
        reply = server.handle_request(request("tools/call", name="vaults", arguments={}), tools=catalog, invoke=invoke)
        self.assertIn("error", reply)
        invoke.assert_not_called()

    def test_no_arbitrary_execution_or_falsy_arguments(self):
        with patch.object(server.subprocess, "Popen") as launch:
            for name in ("python", "bash", "eval", "command", "write"):
                self.assertIn("error", server.handle_request(request("tools/call", name=name, arguments={"code": "anything"})))
            for args in ([], None, "", False):
                self.assertEqual(server.handle_request(request("tools/call", name="read", arguments=args))["error"]["code"], -32602)
            launch.assert_not_called()

    def test_fresh_stdio_rejects_missing_metadata_and_recovers(self):
        messages = [request("server/discover"), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, request("tools/list")]
        messages[-1]["id"] = 3
        result = subprocess.run([sys.executable, server.__file__], input="\n".join(map(json.dumps, messages)) + "\n",
            text=True, capture_output=True, timeout=3, env={**os.environ, "OBSIDIAN_READONLY_LOG_DIR": "off"})
        replies = {reply["id"]: reply for reply in map(json.loads, result.stdout.splitlines())}
        self.assertEqual(result.returncode, 0)
        self.assertIn("result", replies[1])
        self.assertEqual(replies[2]["error"]["code"], -32602)
        self.assertEqual(replies[3]["result"]["resultType"], "complete")

    def test_schema_conformance_when_schema_is_supplied(self):
        # Optional independent validation against the published schema, no runtime dependency.
        location = os.environ.get("MCP_PROTOCOL_SCHEMA")
        if not location:
            self.skipTest("Set MCP_PROTOCOL_SCHEMA to the official 2026-07-28 schema.json")
        import jsonschema
        schema = json.loads(Path(location).read_text())
        for method, definition, params in (("server/discover", "Discover", {}), ("tools/list", "ListTools", {}), ("tools/call", "CallTool", {"name": "read", "arguments": {"path": "x.md"}})):
            message = request(method, **params)
            reply = server.handle_request(message, invoke=lambda *a, **k: ("synthetic", False))
            for obj, ref in ((message, definition + "Request"), (reply["result"], definition + "Result")):
                jsonschema.validate(obj, {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/" + ref})

    def test_stdio_cancellation_stops_work_and_keeps_server_usable(self):
        # Synthetic CLI fixture; the MCP interface never accepts executable code.
        with tempfile.TemporaryDirectory() as directory:
            script = "import obsidian_readonly_mcp as s, sys; s.build_argv=lambda *a: [sys.executable, '-c', 'import time; time.sleep(10)']; s.main()"
            process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, "OBSIDIAN_READONLY_LOG_DIR": directory})
            try:
                process.stdin.write(json.dumps(request("tools/call", name="read", arguments={"path": "synthetic.md"})) + "\n")
                process.stdin.flush()
                deadline = time.monotonic() + 3
                log = Path(directory) / "server.log"
                while not log.exists() or '"command_started"' not in log.read_text():
                    if time.monotonic() > deadline:
                        self.fail("CLI fixture did not start")
                    time.sleep(0.01)
                started = time.monotonic()
                followup = request("tools/list")
                followup["id"] = 2
                output, errors = process.communicate(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}) + "\n" + json.dumps(followup) + "\n", timeout=3)
                self.assertEqual(errors, "")
                self.assertEqual(process.returncode, 0)
                self.assertEqual([r["id"] for r in map(json.loads, output.splitlines())], [2])
                self.assertLess(time.monotonic() - started, 2)
                events = log.read_text()
                self.assertIn('"command_cancelled"', events)
                self.assertNotIn('"cooldown_opened"', events)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
