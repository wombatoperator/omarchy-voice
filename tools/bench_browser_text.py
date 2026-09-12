#!/usr/bin/env python3
"""Free native browser benchmark: owned local page, offscreen text, no API."""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from omarchy_voice.browser import BrowserSurface
from omarchy_voice.config import Config
from omarchy_voice.tools import Executor


def main():
    ex = Executor(Config(notify=False))
    initial = ex._query_json('clients')
    before = {w['address'] for w in initial}
    previous = next((w['address'] for w in initial if w.get('focusHistoryID') == 0), None)
    title = 'OMA text QA ' + str(time.time_ns())
    page = ('<title>' + title + '</title><h1>Harbor trains resume on Monday</h1>' +
            ''.join('<p style="margin:80px">Article paragraph ' + str(i) + '</p>' for i in range(40)) +
            '<p>Final verification code: CEDAR-42</p>')
    address = None
    process = None
    profile = tempfile.TemporaryDirectory(prefix='oma-browser-qa-')
    try:
        process = subprocess.Popen(['/opt/google/chrome/chrome', '--ozone-platform=wayland', '--user-data-dir=' + profile.name,
                                    '--no-first-run', '--disable-sync', '--disable-gpu', '--new-window',
                                    'data:text/html,' + quote(page)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and not address:
            time.sleep(.15)
            address = next((w['address'] for w in ex._query_json('clients')
                            if w['address'] not in before and title in w.get('title', '')), None)
        if not address:
            raise RuntimeError('Owned browser fixture did not open')
        surface = BrowserSurface(ex, lambda: True, lambda *a, **kw: None)
        surface.prepare('address:' + address)
        surface.capture()
        results = []
        for _ in range(3):
            started = time.monotonic()
            result = surface.helper('read_browser_text', {})
            results.append({'duration_ms': round((time.monotonic() - started) * 1000, 1),
                            'passed': result.ok and 'CEDAR-42' in result.output and
                                      'Harbor trains resume' in result.output,
                            'characters': len(result.output)})
        dest = Path(__file__).resolve().parent.parent / 'benchmarks' / 'browser-text-native.json'
        dest.write_text(json.dumps({'api_cost_usd': 0, 'results': results}, indent=2))
        dest.chmod(0o600)
        print(dest.read_text())
        if not all(r['passed'] for r in results):
            raise RuntimeError('Offscreen article extraction failed')
    finally:
        if address:
            ex.call('hypr_dispatch', {'lua': 'hl.dsp.window.close({ window = ' + json.dumps('address:' + address) + ' })'})
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        profile.cleanup()
        if previous:
            ex.call('hypr_dispatch', {'lua': 'hl.dsp.focus({ window = ' + json.dumps('address:' + previous) + ' })'})


if __name__ == '__main__':
    main()
