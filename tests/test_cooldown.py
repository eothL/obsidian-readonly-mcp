import json
import subprocess
import unittest
from unittest.mock import patch

import obsidian_readonly_mcp as server


class CooldownTest(unittest.TestCase):
    def setUp(self):
        server.FAILURES.clear()

    def tearDown(self):
        server.FAILURES.clear()

    def test_two_launch_failures_stop_spawning_and_other_vault_still_works(self):
        with patch.object(server.subprocess, 'Popen', side_effect=FileNotFoundError) as launch:
            for _ in range(2):
                server.run_command(['obsidian', 'vault=a', 'read'], tool='read')
            result, failed = server.run_command(['obsidian', 'vault=a', 'read'], tool='read')
            self.assertTrue(failed)
            self.assertEqual(json.loads(result)['status'], 'cooldown')
            self.assertGreater(json.loads(result)['retry_after_seconds'], 0)
            self.assertEqual(launch.call_count, 2)
            server.run_command(['obsidian', 'vault=b', 'read'], tool='read')
            self.assertEqual(launch.call_count, 3)

    def test_cooldown_expires_without_sleep(self):
        with patch.object(server.time, 'monotonic', return_value=100):
            server.record_failure('vault=a')
            server.record_failure('vault=a')
            self.assertEqual(server.cooldown_remaining('vault=a'), 60)
        with patch.object(server.time, 'monotonic', return_value=161):
            self.assertEqual(server.cooldown_remaining('vault=a'), 0)
            self.assertNotIn('vault=a', server.FAILURES)

    def test_health_bypasses_pause_and_success_clears_it(self):
        server.record_failure('vault=a')
        server.record_failure('vault=a')
        with patch.object(server.subprocess, 'Popen') as launch:
            launch.return_value.communicate.return_value = (b'a\n', b'')
            launch.return_value.returncode = 0
            text, failed = server.health({'vault': 'a'})
            self.assertFalse(failed)
            self.assertEqual(json.loads(text)['obsidian_vault'], 'responsive')
            self.assertNotIn('vault=a', server.FAILURES)
            launch.assert_called_once()

    def test_missing_note_does_not_trip_cooldown(self):
        with patch.object(server.subprocess, 'Popen') as launch:
            launch.return_value.communicate.return_value = (b'Error: File "missing" not found.\n', b'')
            launch.return_value.returncode = 0
            for _ in range(3):
                _, failed = server.run_obsidian('read', {'path': 'missing.md', 'vault': 'a'})
                self.assertTrue(failed)
            self.assertEqual(launch.call_count, 3)
            self.assertNotIn('vault=a', server.FAILURES)

    def test_success_breaks_failure_streak_and_disable_is_honored(self):
        server.record_failure('vault=a')
        with patch.object(server.subprocess, 'Popen') as launch:
            launch.return_value.communicate.return_value = (b'note', b'')
            launch.return_value.returncode = 0
            server.run_command(['obsidian', 'vault=a', 'read'], tool='read')
        self.assertNotIn('vault=a', server.FAILURES)
        with patch.object(server, 'COOLDOWN_SECONDS', 0):
            server.record_failure('vault=a')
            server.record_failure('vault=a')
            self.assertEqual(server.cooldown_remaining('vault=a'), 0)

    def test_batch_stops_launching_after_failures(self):
        calls = {'calls': [{'tool': 'read', 'arguments': {'path': 'x', 'vault': 'a'}}] * 8}
        with patch.object(server.subprocess, 'Popen', side_effect=FileNotFoundError) as launch:
            text, failed = server.call_tool('batch', calls)
        self.assertTrue(failed)
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(len(json.loads(text)), 8)
        self.assertEqual(json.loads(json.loads(text)[2]['text'])['status'], 'cooldown')

    def test_repeated_timeouts_trip_pause_and_failed_probe_does_not_clear_it(self):
        with patch.object(server.subprocess, 'Popen') as launch, patch.object(server.os, 'killpg'):
            process = launch.return_value
            process.pid = 123
            process.communicate.side_effect = [
                subprocess.TimeoutExpired('obsidian', 30), (b'', b''),
                subprocess.TimeoutExpired('obsidian', 30), (b'', b'')]
            process.poll.return_value = -9
            for _ in range(2):
                _, failed = server.run_command(['obsidian', 'vault=a', 'read'], tool='read')
                self.assertTrue(failed)
            self.assertGreater(server.cooldown_remaining('vault=a'), 0)
        with patch.object(server.subprocess, 'Popen', side_effect=FileNotFoundError):
            _, failed = server.health({'vault': 'a'})
        self.assertTrue(failed)
        self.assertGreater(server.cooldown_remaining('vault=a'), 0)

    def test_invalid_arguments_never_affect_backend_health(self):
        with patch.object(server.subprocess, 'Popen') as launch:
            for _ in range(3):
                with self.assertRaises(server.McpError):
                    server.run_obsidian('read', {'path': '../outside.md'})
            launch.assert_not_called()
        self.assertFalse(server.FAILURES)
