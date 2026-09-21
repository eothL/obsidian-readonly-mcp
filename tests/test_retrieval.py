from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

import obsidian_readonly_mcp as server


NOTE = '''---
title: GPU
# Hidden YAML heading
---
# Memory
HBM → cache
## Reuse
Tiles reuse bytes.
```python
# Not a heading
```
# Execution
Warps hide latency.
'''


class RetrievalTest(unittest.TestCase):
    def test_heading_includes_children_and_excludes_next_peer(self):
        result = server.select_text(NOTE, {'heading': 'Memory'})
        self.assertIn('HBM → cache', result)
        self.assertIn('## Reuse', result)
        self.assertNotIn('# Execution', result)
        self.assertTrue(result.startswith('[lines 5-11 of 13]'))

    def test_fences_and_frontmatter_do_not_create_headings(self):
        for heading in ('Not a heading', 'Hidden YAML heading'):
            with self.assertRaisesRegex(server.McpError, 'missing or ambiguous'):
                server.select_text(NOTE, {'heading': heading})

    def test_duplicate_headings_require_disambiguating_range(self):
        with self.assertRaisesRegex(server.McpError, 'ambiguous'):
            server.select_text('# Same\none\n# Same\ntwo\n', {'heading': 'Same'})

    def test_inclusive_range_unicode_and_eof(self):
        self.assertEqual(server.select_text(NOTE, {'start_line': 6, 'end_line': 6}),
                         '[lines 6-6 of 13]\nHBM → cache\n')
        self.assertIn('[lines 12-13 of 13]', server.select_text(NOTE, {'start_line': 12, 'end_line': 999}))

    def test_invalid_selection_fails_before_cli_launch(self):
        bad = [{'start_line': 0}, {'start_line': True}, {'end_line': '5'},
               {'heading': 'Memory', 'start_line': 1}, {'heading': None},
               {'start_line': 5, 'end_line': 2}]
        with patch.object(server, 'run_command') as run:
            for args in bad:
                with self.assertRaises(server.McpError):
                    server.run_obsidian('read', {'path': 'gpu.md', **args})
            run.assert_not_called()

    def test_read_selection_does_not_slice_errors_or_truncated_sources(self):
        for text, failed in [('Error: File not found', True),
                             ('text\n[obsidian-readonly-mcp: output truncated]', False)]:
            with patch.object(server, 'run_command', return_value=(text, failed)):
                _, error = server.run_obsidian('read', {'path': 'gpu.md', 'heading': 'Memory'})
                self.assertTrue(error)

    def test_real_cli_exit_zero_error_is_reported(self):
        text, failed = server.run_command(
            [sys.executable, '-c', 'print("Error: File not found")'], tool='read')
        self.assertTrue(failed)
        self.assertIn('File not found', text)

    def test_batch_preserves_order_and_individual_errors(self):
        def invoke(name, args, **kwargs):
            if args.get('bad'):
                raise server.McpError(-32602, 'bad argument')
            return name, False
        text, failed = server.run_batch(
            {'calls': [{'tool': 'read', 'arguments': {'bad': True}}, {'tool': 'links', 'arguments': {}}]},
            invoke=invoke, allowed={'read', 'links'})
        results = json.loads(text)
        self.assertTrue(failed)
        self.assertTrue(results[0]['isError'])
        self.assertEqual(results[1], {'tool': 'links', 'text': 'links', 'isError': False})

    def test_invalid_or_nested_batch_never_executes(self):
        for args in ({'calls': []}, {'calls': [{'tool': 'batch'}]},
                     {'calls': [{'tool': []}]}, {'calls': [{'tool': 'delete'}]},
                     {'calls': [{'tool': 'read'}] * 9},
                     {'calls': [{'tool': 'read'}], 'vault': 'other'}):
            with patch.object(server, 'call_tool') as invoke:
                with self.assertRaises(server.McpError):
                    server.run_batch(args, invoke=invoke, allowed={'read', 'batch'})
                invoke.assert_not_called()

    def test_batch_deadline_skips_remaining_commands(self):
        calls = {'calls': [{'tool': 'read'}, {'tool': 'read'}]}
        with patch.object(server.time, 'monotonic', side_effect=[0, 1, 31]), patch.object(server, 'call_tool', return_value=('ok', False)) as invoke:
            text, failed = server.run_batch(calls, invoke=invoke, allowed={'read'})
        self.assertTrue(failed)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(invoke.call_args.kwargs['timeout_seconds'], 29)
        self.assertIn('time budget exhausted', json.loads(text)[1]['text'])

    def test_batch_output_budget_includes_envelope_and_stops_reads(self):
        with patch.object(server, 'MAX_OUTPUT_BYTES', 4096), patch.object(server, 'call_tool', return_value=('x' * 6000, False)) as invoke:
            text, failed = server.run_batch({'calls': [{'tool': 'read'}] * 8}, invoke=invoke, allowed={'read'})
        self.assertTrue(failed)
        self.assertEqual(invoke.call_count, 1)
        self.assertLess(len(text.encode()), 4096)
        self.assertEqual(len(json.loads(text)), 8)

    def test_tools_stay_readonly_and_modern_version_is_not_falsely_advertised(self):
        self.assertTrue(all(tool['annotations']['readOnlyHint'] for tool in server.TOOLS))
        response = server.handle_request({'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2026-07-28'}})
        self.assertEqual(response['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(server.handle_request({'id': 2, 'method': 'server/discover'})['error']['code'], -32601)


if __name__ == '__main__':
    unittest.main()
