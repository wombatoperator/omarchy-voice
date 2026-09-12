#!/usr/bin/env python3
"""Summarize measured stages in one Live trace session; no API calls."""
import argparse
import collections
import json
import math
import statistics
from pathlib import Path


def distribution(values):
    values = sorted(v for v in values if isinstance(v, (float, int)))
    if not values:
        return {'count': 0}
    return {'count': len(values), 'median_ms': round(statistics.median(values), 1),
            'p95_ms': values[math.ceil(len(values) * .95) - 1], 'max_ms': values[-1]}


def summarize(rows, session_id=None):
    session_id = session_id or next(r['session_id'] for r in reversed(rows) if r.get('session_id'))
    rows = [r for r in rows if r.get('session_id') == session_id]
    stages = {}
    for event, field in [('backend_finished', 'duration_ms'), ('browser_response', 'duration_ms'),
                         ('batch_started', 'queue_ms'), ('first_output_audio', 'since_input_delta_ms'),
                         ('steering_forwarded', 'since_input_delta_ms')]:
        stages[event] = distribution(r.get(field) for r in rows if r['event'] == event)
    tools = collections.defaultdict(list)
    for r in rows:
        if r['event'] == 'tool_finished':
            tools[r['tool']].append(r['duration_ms'])
    playback = [r for r in rows if r['event'] == 'playback_stats']
    return {'session_id': session_id, 'events': len(rows), 'stages': stages,
            'tools': {name: distribution(values) for name, values in tools.items()},
            'parallel_tool_calls': sum(r['event'] == 'tool_started' and r.get('parallel', False) for r in rows),
            'browser_runs': [{k: r[k] for k in ('duration_ms', 'responses', 'actions', 'usd_estimate', 'usage_complete')}
                             for r in rows if r['event'] == 'browser_finished'],
            'browser_errors': [r['message'] for r in rows if r['event'] == 'browser_error'],
            'audio_underrun_gaps_ms': [r['gap_ms'] for r in rows if r['event'] == 'playback_underrun'],
            'audio_max_loop_lag_ms': max((r['max_loop_lag_ms'] for r in playback), default=0),
            'audio_max_arrival_gap_ms': max((r['max_arrival_gap_ms'] for r in playback), default=0),
            'measurement_note': 'Audio is packet arrival, not speaker onset. Input-relative metrics start at '
                                'the last received transcript fragment, not measured end of microphone speech. '
                                'Browser task start is routing, not the first browser action.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', nargs='?', type=Path,
                        default=Path.home() / '.local/state/omarchy-voice/live-trace.jsonl')
    parser.add_argument('--session')
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.trace.read_text().splitlines() if line.strip()]
    print(json.dumps(summarize(rows, args.session), indent=2))
