from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import obsidian_readonly_mcp as server


class DiagnosticsTest(unittest.TestCase):
    def setUp(self):
        server.FAILURES.clear()

    def test_success_and_nonzero_exit_keep_output(self):
        for code in (0, 2):
            argv = [sys.executable, '-c', f'import sys; print("note-secret"); print("error-secret", file=sys.stderr); sys.exit({code})']
            with patch.object(server, 'build_argv', return_value=argv), self.assertLogs(server.LOGGER, level='INFO') as logs:
                text, failed = server.run_obsidian('read', {'path': 'private-path'}, trace='abc')
            self.assertIn('note-secret', text)
            self.assertEqual(failed, code != 0)
            logged = '\n'.join(logs.output)
            for secret in ('note-secret', 'error-secret', 'private-path'):
                self.assertNotIn(secret, logged)
            self.assertIn('command_finished', logged)
            self.assertIn(f'"exit_code": {code}', logged)

    def test_missing_cli(self):
        with patch.object(server, 'build_argv', return_value=['/nonexistent/obsidian']):
            self.assertEqual(server.run_obsidian('read', {}), ('obsidian CLI not found on PATH', True))

    @unittest.skipUnless(os.name == 'posix', 'POSIX process groups')
    def test_timeout_kills_helpers_holding_pipes_and_next_call_works(self):
        # The parent exits, but its child retains stdout/stderr. This must not hang cleanup.
        code = 'import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])'
        started = time.monotonic()
        with patch.object(server, 'TIMEOUT_SECONDS', 0.15), patch.object(server, 'build_argv', return_value=[sys.executable, '-c', code]):
            text, failed = server.run_obsidian('read', {})
        self.assertTrue(failed)
        self.assertIn('timed out', text)
        self.assertLess(time.monotonic() - started, 2)
        with patch.object(server, 'build_argv', return_value=[sys.executable, '-c', 'print("recovered")']):
            self.assertEqual(server.run_obsidian('read', {}, probe=True), ('recovered\n', False))

    def test_health_checks_vault_and_reports_failures(self):
        for output, command_failed, expected_failed in [('local', False, False), ('Error: app unavailable', False, True), ('', False, True), ('timeout', True, True)]:
            with patch.object(server, 'run_obsidian', return_value=(output, command_failed)) as run:
                response = server.handle_request({'id': 1, 'method': 'tools/call', 'params': {'name': 'health', 'arguments': {'vault': 'local'}}})
            self.assertEqual(response['result']['isError'], expected_failed)
            run.assert_called_once_with('vault', {'vault': 'local', 'info': 'name'}, trace=None, timeout_seconds=None, probe=True)
        listed = server.handle_request({'id': 2, 'method': 'tools/list'})
        self.assertIn('health', {t['name'] for t in listed['result']['tools']})

    def test_rotation_privacy_and_protocol_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = list(server.LOGGER.handlers)
            with patch.dict(os.environ, {'OBSIDIAN_READONLY_LOG_DIR': directory}):
                server.configure_logging()
            handler = server.LOGGER.handlers[-1]
            try:
                handler.maxBytes = 500
                for _ in range(40):
                    server.diagnostic('test_event', trace='test')
                self.assertLessEqual(len(list(Path(directory).glob('server.log*'))), 3)
                self.assertTrue((Path(directory) / 'server.log.2').exists())
                self.assertEqual((Path(directory) / 'server.log').stat().st_mode & 0o777, 0o600)
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    server.write_message({'id': 1, 'result': {}})
                self.assertEqual(json.loads(stream.getvalue()), {'id': 1, 'result': {}})
            finally:
                handler.close()
                server.LOGGER.handlers = previous

    def test_stdio_protocol_and_correlated_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, []]
            result = subprocess.run([sys.executable, server.__file__], input='\n'.join(map(json.dumps, messages))+'\n', text=True, capture_output=True, timeout=3, env={**os.environ, 'OBSIDIAN_READONLY_LOG_DIR': directory})
            self.assertEqual(result.returncode, 0)
            responses = list(map(json.loads, result.stdout.splitlines()))
            self.assertEqual(len(responses), 3)
            self.assertEqual(next(r for r in responses if r['id'] is None)['error']['code'], -32600)
            events = [json.loads(line) for line in (Path(directory) / 'server.log').read_text().splitlines()]
            received = [e['trace'] for e in events if e['event'] == 'request_received']
            sent = [e['trace'] for e in events if e['event'] == 'response_sent']
            self.assertCountEqual(received, sent)

    def test_log_failure_does_not_break_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'not-a-directory'
            target.write_text('')
            with patch.dict(os.environ, {'OBSIDIAN_READONLY_LOG_DIR': str(target)}), contextlib.redirect_stderr(io.StringIO()) as errors:
                server.configure_logging()
            self.assertIn('diagnostic log unavailable', errors.getvalue())
            self.assertEqual(server.handle_request({'id': 1, 'method': 'ping'})['result'], {})


if __name__ == '__main__':
    unittest.main()
