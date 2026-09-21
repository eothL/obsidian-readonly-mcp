#!/usr/bin/env python3
"""Summarize a codex exec --json trace without emitting note text or arguments."""
import argparse
from collections import Counter
import json
from pathlib import Path


def summarize(path, log_path=None):
    events = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    items = [event['item'] for event in events if event.get('type') == 'item.completed']
    calls = [item for item in items if item.get('type') == 'mcp_tool_call']
    operations = []
    for call in calls:
        if call['tool'] == 'batch':
            operations.extend(call.get('arguments', {}).get('calls', []))
        else:
            operations.append({'tool': call['tool'], 'arguments': call.get('arguments', {})})
    errors = 0
    budget_errors = 0
    for call in calls:
        response = call.get('result') or {}
        if call.get('error'):
            errors += 1
            continue
        contents = response.get('content', [])
        if call['tool'] != 'batch':
            errors += bool(response.get('isError') or any(
                content.get('text', '').lstrip().startswith('Error:') for content in contents))
            continue
        for content in contents:
            try:
                children = json.loads(content.get('text', ''))
            except (ValueError, TypeError):
                children = []
            if not isinstance(children, list):
                children = []
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_text = child.get('text', '')
                errors += bool(child.get('isError') or child_text.lstrip().startswith('Error:'))
                budget_errors += 'budget' in child_text.lower() and bool(child.get('isError'))
    result = {
        'result_errors': errors,
        'budget_errors': budget_errors,
        'completed': any(event.get('type') == 'turn.completed' for event in events),
        'mcp_calls': len(calls),
        'mcp_calls_by_tool': dict(Counter(call['tool'] for call in calls)),
        'requested_operations': len(operations),
        'operations_by_tool': dict(Counter(call['tool'] for call in operations)),
        'partial_reads': sum(call['tool'] == 'read' and any(key in call.get('arguments', {}) for key in ('heading', 'start_line', 'end_line')) for call in operations),
        'uncached_input_tokens': next((event['usage']['input_tokens'] - event['usage'].get('cached_input_tokens', 0) for event in reversed(events) if event.get('type') == 'turn.completed'), None),
        'tool_result_chars': sum(len(json.dumps(call.get('result'), ensure_ascii=False)) for call in calls),
        'shell_calls': sum(item.get('type') == 'command_execution' for item in items),
        'web_calls': sum(item.get('type') == 'web_search' for item in items),
        'usage': next((event['usage'] for event in reversed(events) if event.get('type') == 'turn.completed'), None),
    }
    if log_path:
        logs = [json.loads(line) for line in Path(log_path).read_text().splitlines() if line.strip()]
        commands = [event for event in logs if event.get('event') == 'command_finished']
        result['cli_completed'] = len(commands)
        result['cli_total_ms'] = sum(event['elapsed_ms'] for event in commands)
        result['cli_output_bytes'] = sum(event['stdout_bytes'] + event['stderr_bytes'] for event in commands)
        result['cli_timeouts'] = sum(event.get('event') == 'command_timeout' for event in logs)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace')
    parser.add_argument('--server-log')
    args = parser.parse_args()
    print(json.dumps(summarize(args.trace, args.server_log), indent=2))
