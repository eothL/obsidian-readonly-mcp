import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "trace_summary", Path(__file__).parents[1] / "scripts/summarize_trace.py")
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


class TraceSummaryTest(unittest.TestCase):
    def test_errors_without_content_and_private_arguments(self):
        calls = [
            {"tool": "read", "arguments": {"path": "private-note.md"},
             "result": None, "error": {"message": "private transport error"}},
            {"tool": "read", "arguments": {},
             "result": {"isError": True, "content": [{"text": "private response"}]}},
            {"tool": "batch", "arguments": {"calls": [
                {"tool": "read", "arguments": {"heading": "private-heading"}}]},
             "result": {"content": [{"text": json.dumps([
                 {"tool": "read", "text": "Batch output budget exhausted", "isError": True}])}]}},
        ]
        events = [{"type": "item.completed", "item": {"type": "mcp_tool_call", **call}}
                  for call in calls]
        events.append({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            path.write_text("\n".join(map(json.dumps, events)))
            result = summary.summarize(path)
        self.assertEqual(result["result_errors"], 3)
        self.assertEqual(result["budget_errors"], 1)
        self.assertEqual(result["partial_reads"], 1)
        self.assertEqual(result["uncached_input_tokens"], 60)
        self.assertNotIn("private", json.dumps(result))
