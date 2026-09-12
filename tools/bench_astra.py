#!/usr/bin/env python3
"""Budgeted Astra computer protocol test. Synthetic UI, real API, no desktop input."""
import argparse
import asyncio
import base64
import json
import os
import subprocess
import time
from pathlib import Path

from bench_desktop import Budget, ROOT, save
from omarchy_voice import browser, config
from omarchy_voice.tools import Result


def picture(opened):
    text = ('Article opened|Harbor trains resume on Monday|Verified fixture code: CEDAR-42' if opened else
            'OMA browser test|Click the blue Open article button|Open article')
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="500"><rect width="800" height="500" fill="white"/>'
    if not opened:
        svg += '<rect x="60" y="210" width="280" height="80" rx="10" fill="#a8d5ff"/>'
    for i, line in enumerate(text.split('|')):
        svg += f'<text x="80" y="{70 + i * 90}" font-family="DejaVu Sans" font-size="26" fill="black">{line}</text>'
    raw = subprocess.check_output(['magick', 'svg:-', 'png:-'], input=(svg + '</svg>').encode())
    return 'data:image/png;base64,' + base64.b64encode(raw).decode()


async def run(read_text=False):
    config.load_env_file()
    cfg = config.load(notify=False)
    cfg.live_browser_max_turns = 3
    cfg.live_browser_max_output_tokens = 512
    cfg.live_browser_timeout_seconds = 35
    budget = Budget()
    entry = budget.begin({'id': time.strftime('%Y%m%d-%H%M%S') + '-astra-protocol',
                          'model': browser.MODEL, 'case': 'article', 'source': 'synthetic', 'tier': 'default'})
    events, opened = [], [read_text]
    screens = [picture(False), picture(True)]
    class Surface:
        def prepare(self, *args): return screens[0]
        def capture(self): return screens[opened[0]]
        def helper(self, name, arguments):
            if name == 'read_browser_text' and arguments == {}:
                return Result(True, ('Harbor trains resume on Monday. Verified fixture code: CEDAR-42'
                                     if opened[0] else 'OMA browser test. Open article'))
            raise ValueError('Synthetic fixture only supports reading its own text')
        def perform(self, action):
            if action['type'] == 'click' and 60 <= action['x'] <= 340 and 210 <= action['y'] <= 290:
                opened[0] = True
            elif action['type'] not in ('screenshot', 'wait', 'move'):
                raise ValueError('The synthetic UI only supports its Open article button')
    started = time.monotonic()
    worker = browser.BrowserWorker(cfg, None, lambda event, **data: events.append({'event': event, **data}), lambda: True)
    worker.surface = Surface()
    result = None
    try:
        result = await worker.run('Read the open article and tell me its headline and verification code.' if read_text else
                                  'Click Open article, then tell me its headline and verification code from the resulting page.')
    finally:
        responses = [e for e in events if e['event'] == 'browser_response']
        charged = sum(e['usd_estimate'] for e in responses)
        unknown = not responses or any(e['event'] == 'browser_error' and 'API' in e.get('message', '') for e in events)
        metrics = {'passed': bool(result and result.ok and opened[0] and 'CEDAR-42' in result.output),
                   'read_text': read_text, 'helpers': [e['name'] for e in events if e['event'] == 'browser_helper'],
                   'output': result.output if result else '', 'responses': len(responses),
                   'elapsed_ms': round((time.monotonic() - started) * 1000, 1),
                   'charged_estimate': entry['reserve'] if unknown else round(charged, 6)}
        save(ROOT / 'benchmarks' / entry['id'] / 'detail.json', {'metrics': metrics, 'events': events})
        budget.finish(entry, metrics)
        budget.close()
        print(json.dumps(metrics))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--read-text', action='store_true')
    args = parser.parse_args()
    if not args.connect:
        parser.error('--connect required for paid API testing')
    asyncio.run(run(args.read_text))
