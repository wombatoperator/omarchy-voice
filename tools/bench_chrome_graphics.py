#!/usr/bin/env python3
"""Isolated Chrome graphics smoke test; local page, temporary profiles, no API."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from urllib.parse import quote


def clients():
    return json.loads(subprocess.check_output(['hyprctl', 'clients', '-j']))


def main(variants):
    prior = next((w['address'] for w in clients() if w.get('focusHistoryID') == 0), None)
    results = []
    try:
        options = {'baseline': [], 'opengl': ['--disable-features=Vulkan', '--use-angle=gl'],
                   'x11': ['--ozone-platform=x11']}
        for name in variants:
            flags = options[name]
            with tempfile.TemporaryDirectory(prefix='oma-chrome-graphics-') as folder:
                title = 'OMA graphics ' + name + ' ' + str(time.time_ns())
                page = ('<title>' + title + '</title><canvas id="c" width="800" height="600"></canvas><script>'
                        'let gl=c.getContext("webgl2")||c.getContext("webgl");'
                        'if(!gl){document.title+=" NO_WEBGL"}else{'
                        'let ext=gl.getExtension("WEBGL_debug_renderer_info");'
                        'document.title+=" READY "+(ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):"WebGL");'
                        'function draw(t){gl.clearColor((Math.sin(t/1000)+1)/2,.3,.4,1);'
                        'gl.clear(gl.COLOR_BUFFER_BIT);requestAnimationFrame(draw)}requestAnimationFrame(draw)}'
                        '</script>')
                log = Path(folder) / 'stderr.log'
                with log.open('wb') as err:
                    proc = subprocess.Popen(['/opt/google/chrome/chrome', '--user-data-dir=' + folder,
                        '--ozone-platform=wayland', '--no-first-run', '--no-default-browser-check',
                        '--disable-sync', '--enable-logging=stderr', *flags, 'data:text/html,' + quote(page)],
                        stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
                    started = time.monotonic()
                    ready_ms, window = None, None
                    try:
                        deadline = started + 10
                        while time.monotonic() < deadline:
                            window = next((w for w in clients() if title in w.get('title', '')), window)
                            if window and ' READY ' in window['title'] and ready_ms is None:
                                ready_ms = round((time.monotonic() - started) * 1000)
                            time.sleep(.2)
                    finally:
                        if window:
                            subprocess.run(['hyprctl', 'dispatch', 'hl.dsp.window.close({ window = '+json.dumps('address:'+window['address'])+' })'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if proc.poll() is None:
                            proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait()
                errors = [line for line in log.read_text(errors='replace').splitlines()
                          if any(s in line for s in ('FATAL', 'GPU process exited', 'not compatible with Vulkan',
                                                      'Check failed', 'ContextResult', 'Failed to create'))]
                row = {'variant': name, 'ready_ms': ready_ms,
                       'renderer': window['title'].split(' READY ', 1)[-1] if window else None,
                       'errors': errors[:12]}
                results.append(row)
                print(json.dumps(row), flush=True)
        destination = Path(__file__).resolve().parent.parent / 'benchmarks/chrome-graphics.json'
        destination.write_text(json.dumps(results, indent=2))
        destination.chmod(0o600)
    finally:
        if prior:
            subprocess.run(['hyprctl', 'dispatch', 'hl.dsp.focus({ window = '+json.dumps('address:'+prior)+' })'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variants', nargs='+', choices=['baseline', 'opengl', 'x11'], default=['x11'])
    main(parser.parse_args().variants)
