#!/usr/bin/env python3
"""Free desktop integration test: verify X/YouTube launch and focus.

Run inside a transient service with OMA's PrivateTmp/ProtectSystem settings.
Only newly opened test windows are closed. No API calls or microphone use.
"""
import json
import argparse
from pathlib import Path
import sys
import time
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--installed', action='store_true')
args = parser.parse_args()
sys.path.insert(0, str(Path.home() / '.local/share/omarchy-voice/src' if args.installed
                       else Path(__file__).resolve().parents[1] / 'src'))
from omarchy_voice.config import Config
from omarchy_voice.tools import Executor

ex = Executor(Config())
initial_pids = {w['pid'] for w in ex._query_json('clients') if 'chrome' in w.get('class', '').lower()}
prior = next((w['address'] for w in ex._query_json('clients') if w.get('focusHistoryID') == 0), None)
results = []
for host in ('x.com', 'youtube.com'):
    started = time.monotonic()
    window = None
    try:
        window, why = ex._open_web_window('https://' + host + '/', host, timeout=10)
        opened_ms = round((time.monotonic() - started) * 1000, 1)
        if not window:
            raise RuntimeError(why)
        focus_started = time.monotonic()
        got = ex._tool_omarchy_cli('launch or focus webapp ' + host.split('.')[0] + ' https://' + host + '/')
        results.append({'site': host, 'opened': True, 'open_ms': opened_ms,
                        'focused': got.ok, 'focus_ms': round((time.monotonic() - focus_started) * 1000, 1),
                        'window_pid': window['pid'], 'initialTitle': window.get('initialTitle'),
                        'result': got.output})
    except Exception as exc:
        results.append({'site': host, 'error': str(exc)})
    finally:
        if window:
            ex._dispatch_lua('hl.dsp.window.close({ window = '+json.dumps('address:'+window['address'])+' })')
if prior:
    ex._dispatch_lua('hl.dsp.focus({ window = '+json.dumps('address:'+prior)+' })')
print(json.dumps({'installed': args.installed, 'initial_chrome_pids': sorted(initial_pids),
                  'results': results}, indent=2), flush=True)
if any(not r.get('opened') or not r.get('focused') for r in results):
    raise SystemExit(1)
